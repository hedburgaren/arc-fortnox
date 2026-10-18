from odoo.exceptions import UserError, ValidationError

from .common import ArcFortnoxF1Common


class TestCompanySettings(ArcFortnoxF1Common):
    def test_trigger_domain_validation(self):
        self.company.write({"arc_fortnox_trigger_domain": ""})
        self.company.write(
            {"arc_fortnox_trigger_domain": "[('state', '=', 'sale')]"}
        )
        with self.assertRaises(ValidationError):
            self.company.write(
                {"arc_fortnox_trigger_domain": "[('no_such_field', '=', 'x')]"}
            )
        with self.assertRaises(ValidationError):
            self.company.write(
                {"arc_fortnox_trigger_domain": "state = sale"}
            )
        with self.assertRaises(ValidationError):
            self.company.write(
                {"arc_fortnox_trigger_domain": "('state', '=', 'sale')"}
            )

    def test_backoff_schedule_validation(self):
        self.company.write({"arc_fortnox_backoff_minutes": "5, 15, 60"})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_backoff_minutes": "5,abc"})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_backoff_minutes": "0,5"})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_backoff_minutes": ""})

    def test_backoff_delays_helper(self):
        self.company.write({"arc_fortnox_backoff_minutes": "5,15,60,240,1440"})
        self.assertEqual(
            self.company.arc_fortnox_backoff_delays(), [5, 15, 60, 240, 1440]
        )

    def test_positive_settings_validation(self):
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_throttle_calls": 0})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_timeout_connect": 0})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_max_attempts": 0})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_max_attempts": 11})
        with self.assertRaises(ValidationError):
            self.company.write({"arc_fortnox_max_description_length": -1})


class TestCredentialResolution(ArcFortnoxF1Common):
    def setUp(self):
        super().setUp()
        self.parameters = self.env["ir.config_parameter"].sudo()
        self.parameters.set_param("arc_fortnox.client_id", "param-client-id")
        self.parameters.set_param(
            "arc_fortnox.client_secret", "param-client-secret"
        )

    def test_company_settings_win_over_parameters(self):
        self.company.write(
            {
                "arc_fortnox_client_id": "company-client-id",
                "arc_fortnox_client_secret": "company-client-secret",
            }
        )
        client_id, client_secret = (
            self.company.arc_fortnox_client_credentials()
        )
        self.assertEqual(client_id, "company-client-id")
        self.assertEqual(client_secret, "company-client-secret")

    def test_parameter_fallback_used_when_settings_empty(self):
        self.company.write(
            {"arc_fortnox_client_id": False, "arc_fortnox_client_secret": False}
        )
        client_id, client_secret = (
            self.company.arc_fortnox_client_credentials()
        )
        self.assertEqual(client_id, "param-client-id")
        self.assertEqual(client_secret, "param-client-secret")

    def test_underscore_parameter_names_accepted(self):
        self.parameters.set_param("arc_fortnox.client_id", False)
        self.parameters.set_param(
            "arc_fortnox_client_id", "underscore-client-id"
        )
        client_id, _client_secret = (
            self.company.arc_fortnox_client_credentials()
        )
        self.assertEqual(client_id, "underscore-client-id")

    def test_missing_credentials_raise_readable_error(self):
        self.parameters.set_param("arc_fortnox.client_id", False)
        self.parameters.set_param("arc_fortnox.client_secret", False)
        self.company.write(
            {"arc_fortnox_client_id": False, "arc_fortnox_client_secret": False}
        )
        with self.assertRaises(UserError) as context:
            self.company.arc_fortnox_client_credentials()
        self.assertIn(self.company.name, str(context.exception))

    def test_settings_page_links_the_vat_mappings(self):
        arch = self.env["res.config.settings"].get_view()["arch"]
        action = self.env.ref("arc_fortnox.action_arc_fortnox_vat_mapping")
        self.assertIn('name="%d"' % action.id, arch)
