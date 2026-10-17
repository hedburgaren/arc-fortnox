from odoo.modules.neutralize import get_neutralization_queries

from .common import ArcFortnoxF1Common


class TestNeutralize(ArcFortnoxF1Common):
    def test_neutralization_drops_tokens_secret_and_trigger(self):
        self.company.write(
            {
                "arc_fortnox_client_id": "keep-id",
                "arc_fortnox_client_secret": "live-secret",
                "arc_fortnox_trigger_mode": "domain",
                "arc_fortnox_trigger_domain": "[('state', '=', 'sale')]",
            }
        )
        parameters = self.env["ir.config_parameter"].sudo()
        parameters.set_param("arc_fortnox.client_secret", "live-param-secret")
        credential = self.env["arc.fortnox.credential"].create(
            {
                "company_id": self.company.id,
                "state": "authorized",
                "access_token": "live-access",
                "refresh_token": "live-refresh",
                "state_nonce": "nonce",
            }
        )
        self.env.flush_all()

        queries = list(get_neutralization_queries(["arc_fortnox"]))
        self.assertEqual(len(queries), 1, "arc_fortnox ships one neutralize.sql")
        for query in queries:
            self.env.cr.execute(query)
        self.env.invalidate_all()

        self.assertFalse(credential.access_token)
        self.assertFalse(credential.refresh_token)
        self.assertFalse(credential.state_nonce)
        self.assertEqual(credential.state, "not_authorized")
        self.assertFalse(self.company.arc_fortnox_client_secret)
        self.assertEqual(self.company.arc_fortnox_client_id, "keep-id")
        self.assertEqual(self.company.arc_fortnox_trigger_mode, "manual")
        self.assertFalse(parameters.get_param("arc_fortnox.client_secret"))
