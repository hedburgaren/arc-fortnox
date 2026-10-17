import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.translate import _

from .fortnox_client import FortnoxError

_logger = logging.getLogger(__name__)

ACTIVE_STATES = ("pending", "sending", "error")


class ArcFortnoxQueue(models.Model):
    """Queue of sales orders waiting to become invoice drafts in Fortnox.

    At most one active entry (pending, sending or error) may exist per order,
    enforced by a partial unique index created in init(). Done and cancelled
    entries stay as history and do not block a new entry.
    """

    _name = "arc.fortnox.queue"
    _description = "Fortnox Invoice Queue"
    _order = "next_attempt, id"

    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        index=True,
    )
    sale_order_id = fields.Many2one(
        comodel_name="sale.order",
        string="Sales Order",
        required=True,
        index=True,
        ondelete="cascade",
    )
    state = fields.Selection(
        selection=[
            ("pending", "Pending"),
            ("sending", "Sending"),
            ("done", "Done"),
            ("error", "Error"),
            ("cancelled", "Cancelled"),
        ],
        string="State",
        default="pending",
        required=True,
        index=True,
        copy=False,
    )
    attempts = fields.Integer(string="Attempts", default=0, copy=False)
    next_attempt = fields.Datetime(
        string="Next Attempt",
        default=fields.Datetime.now,
        required=True,
    )
    fortnox_invoice_number = fields.Char(
        string="Fortnox Invoice Number", copy=False, readonly=True
    )
    error_code = fields.Char(string="Error Code", copy=False, readonly=True)
    error_text = fields.Text(string="Error Text", copy=False, readonly=True)

    @api.depends("sale_order_id.display_name", "state")
    def _compute_display_name(self):
        state_labels = dict(self._fields["state"]._description_selection(self.env))
        for entry in self:
            order_name = entry.sale_order_id.display_name or ""
            entry.display_name = f"{order_name} ({state_labels.get(entry.state, '')})"

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS arc_fortnox_queue_active_uniq
            ON arc_fortnox_queue (sale_order_id)
            WHERE state IN ('pending', 'sending', 'error')
            """
        )

    @api.model
    def _arc_fortnox_enqueue(self, orders):
        """Create pending entries for the given orders, skipping orders that
        already have an active entry. Returns the entries created now.
        """
        entries = self.browse()
        for order in orders:
            existing = self.sudo().search(
                [
                    ("sale_order_id", "=", order.id),
                    ("state", "in", list(ACTIVE_STATES)),
                ],
                limit=1,
            )
            if existing:
                continue
            entries |= self.sudo().create(
                {
                    "company_id": order.company_id.id,
                    "sale_order_id": order.id,
                }
            )
        return entries

    def _arc_fortnox_check_invoicer(self):
        if not (
            self.env.su
            or self.env.user.has_group("arc_fortnox.group_invoicer")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError(
                _("Only Fortnox invoicers may change the Fortnox invoice queue.")
            )

    def action_cancel(self):
        """Cancel pending or failed entries. Cancel is immediate, on purpose.

        Invoicers read the queue but cannot write to it; state changes go
        through this action and action_retry only.
        """
        self._arc_fortnox_check_invoicer()
        cancellable = self.filtered(lambda entry: entry.state in ("pending", "error"))
        if len(cancellable) != len(self):
            raise UserError(
                _("Only pending or failed queue entries can be cancelled.")
            )
        cancellable.sudo().write({"state": "cancelled"})
        for entry in cancellable:
            entry.sale_order_id.sudo().message_post(
                body=_("Fortnox queue entry cancelled by %s.", self.env.user.name)
            )
        return True

    def action_retry(self):
        """Send failed entries again, with a fresh attempt count.

        Safe by construction: every attempt searches Fortnox for a draft
        with the order's reference before it posts.
        """
        self._arc_fortnox_check_invoicer()
        failed = self.filtered(lambda entry: entry.state == "error")
        if len(failed) != len(self):
            raise UserError(_("Only failed queue entries can be retried."))
        failed.sudo().write(
            {
                "state": "pending",
                "attempts": 0,
                "next_attempt": fields.Datetime.now(),
            }
        )
        for entry in failed:
            entry.sale_order_id.sudo().message_post(
                body=_("Fortnox queue entry retried by %s.", self.env.user.name)
            )
        return True

    # -------------------------------------------------------------
    # Processing
    # -------------------------------------------------------------

    @api.model
    def _arc_fortnox_due_entries(self):
        """Entries that should be processed now, oldest attempt first."""
        return self.sudo().search(
            [
                ("state", "in", ["pending", "sending"]),
                ("next_attempt", "<=", fields.Datetime.now()),
            ],
            order="next_attempt, id",
        )

    @api.model
    def _arc_fortnox_cron_process(self, commit=True):
        """Process due entries: one commit per entry.

        The commit parameter exists for the test suite: tests run on a
        cursor that forbids commits, so they pass False. The scheduled cron
        uses the default and commits after every entry so one failure or a
        crash never loses the state of the entries already handled.
        """
        for entry in self._arc_fortnox_due_entries():
            if entry.state == "pending":
                # Committed before any API call. Every attempt searches
                # Fortnox before it posts, so a crash or a lost answer at
                # any point is recovered by the next attempt.
                entry.write({"state": "sending"})
                if commit:
                    self.env.cr.commit()
            # One savepoint per entry. A FortnoxError is caught inside it,
            # so what the steps kept (a created customer's number) stays;
            # anything else, a database error included, rolls the entry's
            # work back and leaves the cursor usable for the next one.
            failure = None
            try:
                with self.env.cr.savepoint():
                    try:
                        entry._arc_fortnox_process_one()
                    except FortnoxError as error:
                        failure = error
            except Exception:
                _logger.exception(
                    "Fortnox queue entry %s failed unexpectedly", entry.id
                )
                failure = FortnoxError(
                    _("Unexpected error while sending to Fortnox."),
                    code="unexpected_error",
                )
            if failure:
                entry._arc_fortnox_register_failure(failure)
            if commit:
                self.env.cr.commit()
        return True

    def _arc_fortnox_process_one(self):
        """Send one entry. Assumes a sending state.

        The order is checked first: an order that is no longer confirmed,
        or already carries a Fortnox number, is not sent again. Then Fortnox
        is searched for a live draft with this order's reference, on every
        attempt, so a timeout after Fortnox accepted the draft, a crash, or
        a failed write back can never produce a second draft. Only when
        nothing is found is a new draft posted.
        """
        self.ensure_one()
        order = self.sale_order_id
        if order.state != "sale":
            return self._arc_fortnox_cancel_entry(
                _("Order %s is no longer confirmed; nothing was sent.", order.name)
            )
        if order.arc_fortnox_invoice_number:
            return self._arc_fortnox_cancel_entry(
                _(
                    "Order %s already has Fortnox invoice %s; nothing was sent.",
                    order.name,
                    order.arc_fortnox_invoice_number,
                )
            )
        client = self.company_id.arc_fortnox_client()
        with self.env.cr.savepoint():
            existing = order._arc_fortnox_find_existing_draft(client)
        if existing:
            return self._arc_fortnox_mark_done(existing, adopted=True)
        number = order._arc_fortnox_push_draft(client)
        return self._arc_fortnox_mark_done(number)

    def _arc_fortnox_cancel_entry(self, reason):
        self.ensure_one()
        self.write({"state": "cancelled", "error_code": False, "error_text": reason})
        self.sale_order_id.message_post(body=reason)
        return True

    def _arc_fortnox_mark_done(self, number, adopted=False):
        """Adopt the Fortnox draft number and close the entry.

        The number and the done state are what keeps the draft from being
        sent twice, so they are written first. The companion hook and the
        chatter note follow in a savepoint of their own: a failure there is
        logged and raised as an activity, and never makes the entry retry.
        """
        self.ensure_one()
        order = self.sale_order_id
        self.write(
            {
                "state": "done",
                "fortnox_invoice_number": number,
                "error_code": False,
                "error_text": False,
            }
        )
        order.write({"arc_fortnox_invoice_number": number})
        try:
            with self.env.cr.savepoint():
                order._arc_fortnox_invoice_created()
                if adopted:
                    body = _("Existing Fortnox invoice draft %s adopted.", number)
                else:
                    body = _("Fortnox invoice draft %s created.", number)
                order.message_post(body=body)
        except Exception:
            _logger.exception(
                "Fortnox queue entry %s: follow up after draft %s failed",
                self.id,
                number,
            )
            with self.env.cr.savepoint():
                order.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=_("Fortnox follow up failed"),
                    note=_(
                        "Fortnox invoice draft %s exists and is recorded on "
                        "the order, but the follow up steps after it failed. "
                        "See the server log for queue entry %s.",
                        number,
                        self.id,
                    ),
                )
        return True

    def _arc_fortnox_register_failure(self, error):
        """Store the failure, apply backoff or go to a hard error."""
        self.ensure_one()
        company = self.company_id.sudo()
        attempts = self.attempts + 1
        max_attempts = company.arc_fortnox_max_attempts
        values = {
            "attempts": attempts,
            "error_code": error.error_code,
            "error_text": error.error_text,
        }
        if attempts >= max_attempts:
            values["state"] = "error"
            self.write(values)
            self._arc_fortnox_alert_failure()
        else:
            delays = company.arc_fortnox_backoff_delays()
            delay = delays[min(attempts - 1, len(delays) - 1)]
            values["state"] = "pending"
            values["next_attempt"] = fields.Datetime.now() + timedelta(
                minutes=delay
            )
            self.write(values)
        return True

    def _arc_fortnox_alert_failure(self):
        """Hard error: activity on the order and a chatter note."""
        entry = self.sudo()
        order = entry.sale_order_id
        order.activity_schedule(
            "mail.mail_activity_data_todo",
            summary=_("Fortnox invoicing failed"),
            note=_(
                "Queue entry %s gave up after %s attempts. Last error: %s (%s).",
                entry.id,
                entry.attempts,
                entry.error_text or "unknown",
                entry.error_code or "no code",
            ),
        )
        order.message_post(
            body=_(
                "Fortnox invoicing failed after %s attempts: %s (%s).",
                entry.attempts,
                entry.error_text or "unknown",
                entry.error_code or "no code",
            )
        )
