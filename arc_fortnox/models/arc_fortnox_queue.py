import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
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

    def action_cancel(self):
        """Cancel pending entries. Cancel is immediate, on purpose."""
        pending = self.filtered(lambda entry: entry.state == "pending")
        if len(pending) != len(self):
            raise UserError(
                _("Only pending queue entries can be cancelled.")
            )
        pending.write({"state": "cancelled"})

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
            crashed = False
            if entry.state == "pending":
                # Committed before any API call: if the process dies during
                # the request, the sending state tells the next run to look
                # for an already created draft before posting again.
                entry.write({"state": "sending"})
                if commit:
                    self.env.cr.commit()
            else:
                # Found in sending: a previous run may have died after the
                # POST, so the push must adopt before it duplicates.
                crashed = True
            try:
                entry._arc_fortnox_process_one(recover=crashed)
            except FortnoxError as error:
                entry._arc_fortnox_register_failure(error)
            except Exception:
                _logger.exception(
                    "Fortnox queue entry %s failed unexpectedly", entry.id
                )
                entry._arc_fortnox_register_failure(
                    FortnoxError(
                        _("Unexpected error while pushing to Fortnox."),
                        code="unexpected_error",
                    )
                )
            if commit:
                self.env.cr.commit()
        return True

    def _arc_fortnox_process_one(self, recover=False):
        """Push one entry. Assumes a sending state.

        With recover set, the entry was found in sending from a previous
        run: Fortnox is searched by YourOrderNumber first and an existing
        draft is adopted instead of duplicated.
        """
        self.ensure_one()
        order = self.sale_order_id
        client = self.company_id.arc_fortnox_client()
        if recover:
            found = client.get("invoices", params={"yourordernumber": order.name})
            invoices = (found or {}).get("Invoices") or []
            if invoices:
                number = str(invoices[0].get("DocumentNumber") or "")
                if number:
                    return self._arc_fortnox_mark_done(number)
        number = order._arc_fortnox_push_draft(client)
        return self._arc_fortnox_mark_done(number)
        number = order._arc_fortnox_push_draft(client)
        return self._arc_fortnox_mark_done(number)

    def _arc_fortnox_mark_done(self, number):
        """Adopt the Fortnox draft number and close the entry."""
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
        order._arc_fortnox_invoice_created()
        order.message_post(
            body=_("Fortnox invoice draft %s created.", number)
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
