from odoo.exceptions import AccessError

from .common import ArcFortnoxF1Common


class TestCredentialSecurity(ArcFortnoxF1Common):
    def test_credential_model_admin_only(self):
        """A user outside base.group_system cannot read or write the
        credential model at all. Module code uses sudo() internally."""
        credential = self.env["arc.fortnox.credential"].create(
            {"company_id": self.company.id}
        )
        admin = self.env.ref("base.user_admin")
        plain = self._create_plain_user()

        # The admin (base.group_system) has full access.
        self.assertTrue(
            credential.with_user(admin).read(["state"]),
            "An administrator must be able to read the credential.",
        )

        # A plain internal user gets an access error on any operation.
        for operation in (lambda: credential.with_user(plain).read(["state"]), lambda: credential.with_user(plain).write({"state": "authorized"})):
            with self.assertRaises(AccessError):
                operation()

        # The invoicer group deliberately has no access either: it may queue
        # orders but never sees tokens.
        invoicer = self._create_invoicer_user()
        with self.assertRaises(AccessError):
            self.env["arc.fortnox.credential"].with_user(invoicer).search([])

    def test_token_fields_hidden_from_non_admins(self):
        """Field level groups make the tokens unreadable for users outside
        base.group_system: reading them raises an access error."""
        credential = self.env["arc.fortnox.credential"].create(
            {
                "company_id": self.company.id,
                "access_token": "secret-access-token",
                "refresh_token": "secret-refresh-token",
            }
        )
        plain = self._create_plain_user()
        hidden = credential.with_user(plain)
        with self.assertRaises(AccessError):
            hidden.access_token
        with self.assertRaises(AccessError):
            hidden.refresh_token
        visible = credential.sudo()
        self.assertEqual(visible.access_token, "secret-access-token")
        self.assertEqual(visible.refresh_token, "secret-refresh-token")

    def test_one_credential_per_company(self):
        model = self.env["arc.fortnox.credential"]
        model.create({"company_id": self.company.id})
        self.env.flush_all()
        from psycopg2.errors import UniqueViolation

        with self.assertRaises(UniqueViolation):
            with self.env.cr.savepoint():
                model.create({"company_id": self.company.id})
