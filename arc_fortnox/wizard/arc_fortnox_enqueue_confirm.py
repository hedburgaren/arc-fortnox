from odoo import _, fields, models


class ArcFortnoxEnqueueConfirm(models.TransientModel):
    """Shows the warnings of `_arc_fortnox_enqueue_warnings` before queueing."""

    _name = "arc.fortnox.enqueue.confirm"
    _description = "Fortnox: confirm queueing despite warnings"

    sale_order_ids = fields.Many2many("sale.order", string="Orders", readonly=True)
    warning_text = fields.Text(string="Warnings", readonly=True)

    def _arc_fortnox_open(self, orders, warnings):
        wizard = self.create(
            {
                "sale_order_ids": [(6, 0, orders.ids)],
                "warning_text": "\n".join(warnings),
            }
        )
        return {
            "type": "ir.actions.act_window",
            "name": _("Queue for Fortnox?"),
            "res_model": self._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_confirm(self):
        self.ensure_one()
        self.sale_order_ids.with_context(
            arc_fortnox_warnings_confirmed=True
        ).action_arc_fortnox_enqueue()
        return {"type": "ir.actions.act_window_close"}
