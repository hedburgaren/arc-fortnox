import base64
import csv
import io

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from ..models.arc_fortnox_record import RECORD_TYPES

# Column names in Fortnox's own exports, English and Swedish, per field.
CUSTOMER_COLUMNS = {
    "fortnox_number": ("customer_number", "Kundnummer", "Kundnr"),
    "name": ("name", "Namn", "Kundnamn"),
    "organisation_number": ("organisation_number", "Organisationsnummer", "Org.nr"),
    "email": ("email", "E-post", "Email"),
    "city": ("invoice_city", "Ort", "Fakturaort"),
    "active": ("active", "Aktiv"),
    "customer_type": ("type", "Typ"),
}
ARTICLE_COLUMNS = {
    "fortnox_number": ("Artikelnummer", "article_number", "ArticleNumber"),
    "name": ("Benämning", "description", "Description"),
    "active": ("Aktiv", "active", "Active"),
}
TRUE_VALUES = {"1", "ja", "yes", "true", "sant"}


class ArcFortnoxRecordImport(models.TransientModel):
    """Load Fortnox customers or articles from a Fortnox export file.

    For installations without API access yet, or for a first look before
    the connection is authorized. The file is parsed in memory and never
    stored.
    """

    _name = "arc.fortnox.record.import"
    _description = "Fortnox: import records from an export"

    record_type = fields.Selection(RECORD_TYPES, string="Type", required=True, default="customer")
    file = fields.Binary(string="Fortnox Export (CSV)", required=True, attachment=False)
    file_name = fields.Char(string="File Name")

    def _arc_fortnox_rows(self):
        self.ensure_one()
        text = base64.b64decode(self.file).decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        columns = CUSTOMER_COLUMNS if self.record_type == "customer" else ARTICLE_COLUMNS
        header = reader.fieldnames or []
        found = {}
        for field, aliases in columns.items():
            for alias in aliases:
                if alias in header:
                    found[field] = alias
                    break
        if "fortnox_number" not in found:
            raise UserError(
                _(
                    "The file has no column for the Fortnox number. Expected one of: %s.",
                    ", ".join(columns["fortnox_number"]),
                )
            )
        rows = []
        for line in reader:
            row = {field: (line.get(column) or "").strip() for field, column in found.items()}
            if "active" in row:
                row["fortnox_active"] = row.pop("active").casefold() in TRUE_VALUES
            rows.append(row)
        return rows

    def action_import(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators may load records from Fortnox."))
        records = self.env["arc.fortnox.record"]._arc_fortnox_upsert(
            self.env.company, self.record_type, self._arc_fortnox_rows()
        )
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "arc_fortnox.action_arc_fortnox_record_customer"
            if self.record_type == "customer"
            else "arc_fortnox.action_arc_fortnox_record_article"
        )
        action["name"] = _("%s Fortnox records loaded", len(records))
        return action
