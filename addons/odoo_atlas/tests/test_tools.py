"""Tests for the tools themselves.

Two things are being defended. That each tool answers the question it claims to
answer, and that none of them can be talked into doing something else — writing,
escalating, or reading past the acting user's rights.
"""

import datetime
import json
import pathlib
import re

from odoo.addons.odoo_atlas.services import tools
from odoo.addons.odoo_atlas.services.tools import catalog
from odoo.addons.odoo_atlas.services.tools.filters import FilterError
from odoo.addons.odoo_atlas.tests.common import AtlasCase

TOOLS_ROOT = pathlib.Path(tools.__file__).resolve().parent

#: Every way to change something. A tool is read-only in 1.0 (ADR-0006), and
#: write operations are a post-1.0 milestone with explicit human confirmation —
#: not something to arrive behind a helpful-looking argument.
WRITE_CALLS = (
    ("create", re.compile(r"\.create\s*\(")),
    ("write", re.compile(r"\.write\s*\(")),
    ("unlink", re.compile(r"\.unlink\s*\(")),
    ("copy", re.compile(r"\.copy\s*\(")),
    ("sudo", re.compile(r"\.sudo\s*\(")),
    ("execute", re.compile(r"\.execute\s*\(")),
)


class TestToolsAreReadOnly(AtlasCase):
    """The rule, enforced by scanning rather than by review."""

    def sources(self):
        return sorted(TOOLS_ROOT.rglob("*.py"))

    def test_the_scan_reads_the_tools(self):
        names = {path.name for path in self.sources()}

        self.assertIn("handlers.py", names)
        self.assertIn("filters.py", names)

    def test_no_tool_writes_anything(self):
        offences = []
        for path in self.sources():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for label, pattern in WRITE_CALLS:
                    if pattern.search(line):
                        offences.append(f"{path.name}:{number} calls {label}")

        self.assertFalse(
            offences,
            "Atlas tools are read-only in 1.0 (ADR-0006). Found:\n  " + "\n  ".join(offences),
        )

    def test_the_scan_would_notice(self):
        samples = (
            "self.env['res.partner'].create({})",
            "records.write({'name': 'x'})",
            "records.unlink()",
            "self.env['res.partner'].sudo().search([])",
        )
        for sample in samples:
            self.assertTrue(
                any(pattern.search(sample) for _label, pattern in WRITE_CALLS),
                f"the scan missed {sample!r}",
            )


class TestCatalog(AtlasCase):
    def test_the_closed_set_is_registered(self):
        self.assertEqual(
            tools.names(),
            ("aggregate", "customer_360", "find_records", "overdue_invoices", "stock_levels"),
        )

    def test_every_tool_has_a_strict_schema(self):
        """Strict schemas are what stop a model inventing an extra argument."""
        for name in tools.names():
            schema = tools.get(name).parameters
            self.assertEqual(schema["type"], "object", name)
            self.assertIs(schema["additionalProperties"], False, name)

    def test_every_description_says_when_to_use_the_tool(self):
        """A description that only states a capability under-triggers the tool."""
        for name in tools.names():
            description = tools.get(name).description
            self.assertGreater(len(description), 80, name)
            self.assertIn("use this", description.lower(), name)

    def test_the_catalogue_leaves_out_what_this_database_cannot_serve(self):
        # No `sale`, `stock` or `account` on a base-only database, so the tools
        # that need them are not offered at all.
        offered = {entry["name"] for entry in tools.catalog_for(self.env)}

        self.assertIn("find_records", offered)
        self.assertIn("customer_360", offered)
        self.assertNotIn("stock_levels", offered)
        self.assertNotIn("overdue_invoices", offered)

    def test_registering_a_duplicate_name_is_refused(self):
        with self.assertRaises(ValueError):
            tools.register(tools.get("find_records"))


class TestFindRecords(AtlasCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.acme = cls.env["res.partner"].create(
            {"name": "Acme Industries", "city": "Brussels", "is_company": True}
        )
        cls.other = cls.env["res.partner"].create({"name": "Beta Supplies", "city": "Paris"})

    def run_tool(self, name, arguments, user=None):
        return tools.get(name).run(self.env(user=user or self.alice), arguments)

    def test_it_reads_matching_records(self):
        result = self.run_tool(
            "find_records",
            {
                "model": "res.partner",
                "filters": [{"field": "city", "operator": "=", "value": "Brussels"}],
                "fields": ["display_name", "city"],
            },
        )

        names = [row["display_name"] for row in result["rows"]]
        self.assertIn("Acme Industries", names)
        self.assertNotIn("Beta Supplies", names)

    def test_it_reports_how_many_matched_beyond_what_it_returned(self):
        """Otherwise a model presents the first page as the whole answer."""
        self.env["res.partner"].create(
            [{"name": f"Bulk {index}", "city": "Brussels"} for index in range(5)]
        )

        result = self.run_tool(
            "find_records",
            {
                "model": "res.partner",
                "filters": [{"field": "city", "operator": "=", "value": "Brussels"}],
                "limit": 2,
            },
        )

        self.assertEqual(result["returned"], 2)
        self.assertGreater(result["matched"], 2)
        self.assertTrue(result["truncated"])

    def test_a_model_outside_the_allow_list_is_refused(self):
        with self.assertRaises(FilterError) as caught:
            self.run_tool("find_records", {"model": "res.users"})

        self.assertIn("Allowed models", str(caught.exception))

    def test_a_model_the_user_cannot_read_is_refused(self):
        """What a tool can see is what the person who asked could see."""
        self.assertFalse(self.env["ir.config_parameter"].with_user(self.alice).has_access("read"))

        with self.assertRaises(FilterError):
            self.run_tool("find_records", {"model": "ir.config_parameter"})

    def test_the_row_cap_cannot_be_argued_past(self):
        result = self.run_tool("find_records", {"model": "res.partner", "limit": 100_000})

        self.assertLessEqual(result["returned"], catalog.MAX_ROWS)


class TestAggregate(AtlasCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.partner"].create(
            [
                {"name": "One", "city": "Brussels", "is_company": True},
                {"name": "Two", "city": "Brussels", "is_company": True},
                {"name": "Three", "city": "Paris", "is_company": False},
            ]
        )

    def run_tool(self, arguments):
        return tools.get("aggregate").run(self.env(user=self.alice), arguments)

    def test_it_groups_and_counts(self):
        result = self.run_tool({"model": "res.partner", "group_by": ["is_company"]})

        self.assertEqual(result["group_by"], ["is_company"])
        self.assertTrue(all("__count" in row for row in result["rows"]))

    def test_a_relation_group_comes_back_as_a_name(self):
        """A recordset does not survive JSON and means nothing to a model."""
        result = self.run_tool({"model": "res.partner", "group_by": ["country_id"]})

        for row in result["rows"]:
            self.assertIsInstance(row["country_id"], (str, bool, type(None)))

    def test_grouping_by_something_not_groupable_is_refused(self):
        """Grouping by free text is a way to page a table one group at a time."""
        with self.assertRaises(FilterError):
            self.run_tool({"model": "res.partner", "group_by": ["email"]})

    def test_totalling_a_non_measure_is_refused(self):
        with self.assertRaises(FilterError):
            self.run_tool(
                {"model": "res.partner", "group_by": ["is_company"], "measures": ["email"]}
            )

    def test_no_group_by_is_refused(self):
        with self.assertRaises(FilterError):
            self.run_tool({"model": "res.partner", "group_by": []})

    def test_too_many_grouping_levels_are_refused(self):
        with self.assertRaises(FilterError):
            self.run_tool(
                {
                    "model": "res.partner",
                    "group_by": ["is_company", "country_id", "user_id", "company_id"],
                }
            )


class TestCustomer360(AtlasCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.partner"].create({"name": "Unique Customer", "city": "Ghent"})
        cls.env["res.partner"].create([{"name": "Twin Customer"}, {"name": "Twin Customer Two"}])

    def run_tool(self, arguments):
        return tools.get("customer_360").run(self.env(user=self.alice), arguments)

    def test_it_summarises_one_customer(self):
        result = self.run_tool({"partner": "Unique Customer"})

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["partner"]["display_name"], "Unique Customer")

    def test_an_unknown_customer_is_reported_as_no_match(self):
        result = self.run_tool({"partner": "Nobody At All"})

        self.assertEqual(result["matched"], 0)

    def test_an_ambiguous_name_is_reported_rather_than_guessed(self):
        """Picking the first match is how a summary about the wrong company
        gets presented with confidence."""
        result = self.run_tool({"partner": "Twin Customer"})

        self.assertTrue(result["ambiguous"])
        self.assertGreater(len(result["candidates"]), 1)

    def test_a_missing_name_is_refused(self):
        with self.assertRaises(FilterError):
            self.run_tool({})


class TestToolsRespectRecordRules(AtlasCase):
    """The property that matters most: a tool cannot outrun its user.

    ``atlas.conversation`` is not in the tool allow-list, so this uses the
    record rules it does have to prove the general point — the tools run in the
    acting user's environment and the ORM does the rest.
    """

    def test_a_tool_runs_in_the_acting_users_environment(self):
        result = tools.get("find_records").run(
            self.env(user=self.alice), {"model": "res.partner", "limit": 1}
        )

        self.assertEqual(result["model"], "res.partner")

    def test_two_users_can_get_different_answers(self):
        restricted = self.env["res.partner"].create({"name": "Rule Test Partner"})
        rule = self.env["ir.rule"].create(
            {
                "name": "hide one partner from alice",
                "model_id": self.env.ref("base.model_res_partner").id,
                "groups": [(4, self.env.ref("odoo_atlas.group_atlas_user").id)],
                "domain_force": f"[('id', '!=', {restricted.id})]",
            }
        )
        self.addCleanup(rule.unlink)
        self.env.registry.clear_cache()

        result = tools.get("find_records").run(
            self.env(user=self.alice),
            {
                "model": "res.partner",
                "filters": [{"field": "id", "operator": "=", "value": restricted.id}],
            },
        )

        self.assertEqual(result["matched"], 0)


class TestToolResultsSurviveJson(AtlasCase):
    """Whatever a tool returns has to reach the model unchanged.

    The ORM hands back `date` objects and `(id, name)` tuples, which the HTTP
    layer converts on the way out. A tool called in process therefore used to
    return a different shape from the same tool called over the wire, and a test
    reading the in-process result was reading something that never shipped.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.partner"].create(
            {"name": "Json Test Partner", "city": "Ghent", "country_id": cls.env.ref("base.be").id}
        )

    def run_tool(self, name, arguments):
        return tools.get(name).run(self.env(user=self.alice), arguments)

    def test_a_result_serialises_without_a_fallback_encoder(self):
        """`default=` would paper over exactly the types being checked here."""
        result = self.run_tool(
            "find_records",
            {"model": "res.partner", "fields": ["display_name", "country_id", "create_date"]},
        )

        json.dumps(result)

    def test_dates_come_back_as_strings(self):
        result = self.run_tool(
            "find_records", {"model": "res.partner", "fields": ["create_date"], "limit": 1}
        )

        created = result["rows"][0]["create_date"]
        self.assertIsInstance(created, str)
        self.assertEqual(datetime.datetime.fromisoformat(created).year >= 2020, True)

    def test_a_relation_keeps_both_its_id_and_its_name(self):
        """The name is what the model reads; the id is what a follow-up filters on."""
        result = self.run_tool(
            "find_records",
            {
                "model": "res.partner",
                "filters": [{"field": "name", "operator": "=", "value": "Json Test Partner"}],
                "fields": ["country_id"],
            },
        )

        country = result["rows"][0]["country_id"]
        self.assertEqual(country[0], self.env.ref("base.be").id)
        self.assertIsInstance(country[1], str)

    def test_a_customer_summary_serialises_too(self):
        result = self.run_tool("customer_360", {"partner": "Json Test Partner"})

        json.dumps(result)
