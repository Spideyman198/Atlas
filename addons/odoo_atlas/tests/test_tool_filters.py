"""Tests for the filter compiler.

A language model chooses these arguments. The interesting cases are therefore
not the ones a developer would write — they are the ones a model produces when
it has half-remembered a field name, guessed at a type, or decided that a dotted
path would be convenient.

The last class in this file is the roadmap's property test: no input, however
constructed, compiles to a domain that reaches outside the allow-list.
"""

import itertools

from odoo.addons.odoo_atlas.services.tools import catalog, filters
from odoo.addons.odoo_atlas.services.tools.filters import FilterError
from odoo.tests import TransactionCase


class FilterCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.spec = catalog.PARTNER
        cls.model = cls.env["res.partner"]

    def compile(self, *entries):
        return filters.compile_filters(self.model, list(entries), self.spec)


class TestFilterCompilation(FilterCase):
    def test_no_filters_compiles_to_an_empty_domain(self):
        self.assertEqual(filters.compile_filters(self.model, None, self.spec), [])
        self.assertEqual(self.compile(), [])

    def test_a_simple_equality_compiles(self):
        domain = self.compile({"field": "city", "operator": "=", "value": "Brussels"})

        self.assertEqual(domain, [("city", "=", "Brussels")])

    def test_several_filters_are_anded(self):
        domain = self.compile(
            {"field": "city", "operator": "=", "value": "Brussels"},
            {"field": "is_company", "operator": "=", "value": True},
        )

        self.assertEqual(len(domain), 2)

    def test_a_list_operator_takes_a_list(self):
        domain = self.compile({"field": "id", "operator": "in", "value": [1, 2, 3]})

        self.assertEqual(domain, [("id", "in", [1, 2, 3])])

    def test_the_compiled_domain_actually_searches(self):
        """A domain that compiles but does not execute is not much of a win."""
        domain = self.compile({"field": "is_company", "operator": "=", "value": True})

        self.assertIsInstance(self.model.search_count(domain), int)


class TestFilterRejection(FilterCase):
    def test_a_field_outside_the_allow_list_is_refused(self):
        with self.assertRaises(FilterError) as caught:
            self.compile({"field": "password", "operator": "=", "value": "x"})

        # The message lists what *is* allowed: models correct themselves from it.
        self.assertIn("Allowed", str(caught.exception))

    def test_a_dotted_path_is_refused(self):
        """The whole reason the compiler exists.

        `partner_id.user_id.login` is a perfectly valid Odoo domain that walks
        two relations to a field nobody allow-listed.
        """
        with self.assertRaises(FilterError) as caught:
            self.compile({"field": "user_id.login", "operator": "=", "value": "admin"})

        self.assertIn("traverses a relation", str(caught.exception))

    def test_an_unknown_operator_is_refused(self):
        with self.assertRaises(FilterError):
            self.compile({"field": "city", "operator": "=like", "value": "Br%"})

    def test_child_of_is_refused(self):
        """Its meaning depends on a hierarchy the model cannot see."""
        with self.assertRaises(FilterError):
            self.compile({"field": "parent_id", "operator": "child_of", "value": 1})

    def test_a_number_against_a_text_field_is_refused(self):
        """Odoo would coerce and match nothing, which reads as 'no results'."""
        with self.assertRaises(FilterError):
            self.compile({"field": "city", "operator": "=", "value": 42})

    def test_a_boolean_against_a_numeric_field_is_refused(self):
        """`True` is an int in Python and would silently mean 1."""
        with self.assertRaises(FilterError):
            self.compile({"field": "customer_rank", "operator": "=", "value": True})

    def test_a_list_for_a_scalar_operator_is_refused(self):
        with self.assertRaises(FilterError):
            self.compile({"field": "city", "operator": "=", "value": ["Brussels", "Paris"]})

    def test_a_scalar_for_a_list_operator_is_refused(self):
        with self.assertRaises(FilterError):
            self.compile({"field": "id", "operator": "in", "value": 1})

    def test_an_empty_list_is_refused(self):
        with self.assertRaises(FilterError):
            self.compile({"field": "id", "operator": "in", "value": []})

    def test_an_over_long_list_is_refused(self):
        oversized = list(range(catalog.MAX_LIST_VALUES + 1))

        with self.assertRaises(FilterError):
            self.compile({"field": "id", "operator": "in", "value": oversized})

    def test_an_over_long_string_is_refused(self):
        with self.assertRaises(FilterError):
            self.compile(
                {"field": "city", "operator": "ilike", "value": "x" * (catalog.MAX_TEXT_LENGTH + 1)}
            )

    def test_too_many_filters_are_refused(self):
        entries = [{"field": "city", "operator": "=", "value": "Brussels"}] * (
            catalog.MAX_FILTERS + 1
        )

        with self.assertRaises(FilterError):
            filters.compile_filters(self.model, entries, self.spec)

    def test_a_nested_structure_is_refused(self):
        with self.assertRaises(FilterError):
            self.compile({"field": "id", "operator": "in", "value": [[1, 2], 3]})

    def test_a_filter_that_is_not_an_object_is_refused(self):
        with self.assertRaises(FilterError):
            filters.compile_filters(self.model, ["city = Brussels"], self.spec)

    def test_a_raw_domain_is_refused(self):
        """A model that has seen Odoo before will try this."""
        with self.assertRaises(FilterError):
            filters.compile_filters(self.model, [["city", "=", "Brussels"]], self.spec)


class TestFieldSelection(FilterCase):
    def test_omitting_fields_gives_a_useful_default(self):
        fields = filters.check_fields(None, self.spec)

        self.assertIn("id", fields)
        self.assertTrue(set(fields) <= set(self.spec.fields))

    def test_id_is_always_included(self):
        """Without it a caller cannot follow up on anything it was told about."""
        self.assertEqual(filters.check_fields(["city"], self.spec)[0], "id")

    def test_an_unknown_field_is_refused_rather_than_dropped(self):
        """Silently omitting one makes a model conclude the record has no value."""
        with self.assertRaises(FilterError):
            filters.check_fields(["city", "password"], self.spec)

    def test_the_row_limit_is_clamped_not_refused(self):
        self.assertEqual(filters.check_limit(10_000), catalog.MAX_ROWS)
        self.assertEqual(filters.check_limit(0), 1)
        self.assertEqual(filters.check_limit(None), catalog.MAX_ROWS)
        self.assertEqual(filters.check_limit("nonsense"), catalog.MAX_ROWS)


class TestNoInputEscapesTheAllowList(FilterCase):
    """The roadmap's property test.

    Rather than hand-picking hostile inputs, this enumerates the cross product
    of plausible fields, operators and values — including the ones a model would
    produce by mistake — and asserts the invariant over all of them: every
    compiled clause names an allow-listed field with an allow-listed operator,
    or nothing is compiled at all.

    Enumeration rather than a fuzzing library: the interesting space here is
    small and known, and adding `hypothesis` to run it would be more dependency
    than insight.
    """

    FIELDS = (
        "city",  # allowed, text
        "customer_rank",  # allowed, numeric
        "id",  # allowed, integer
        "is_company",  # allowed, boolean
        "password",  # never allowed
        "user_id.login",  # relation traversal
        "",  # empty
        "__class__",  # a Python attribute, not a field
        "city; DROP TABLE",  # an attempt at something else entirely
    )
    OPERATORS = ("=", "!=", ">", "in", "not in", "ilike", "=like", "child_of", "", "any")
    VALUES = (
        "Brussels",
        42,
        0,
        True,
        False,
        None,
        [1, 2],
        [],
        {"a": 1},
        "x" * (catalog.MAX_TEXT_LENGTH + 1),
    )

    def test_every_compiled_clause_stays_inside_the_allow_list(self):
        compiled = 0
        for field, operator, value in itertools.product(self.FIELDS, self.OPERATORS, self.VALUES):
            entry = {"field": field, "operator": operator, "value": value}
            try:
                domain = filters.compile_filters(self.model, [entry], self.spec)
            except FilterError:
                continue

            compiled += 1
            self.assertEqual(len(domain), 1, entry)
            name, compiled_operator, _compiled_value = domain[0]
            self.assertIn(name, self.spec.fields, entry)
            self.assertIn(compiled_operator, catalog.ALLOWED_OPERATORS, entry)
            self.assertNotIn(".", name, entry)

        # Guards the guard: an invariant that holds because nothing ever
        # compiles is not an invariant worth having.
        self.assertGreater(compiled, 10)

    def test_everything_that_compiles_also_executes(self):
        """A domain the ORM rejects is a 500 where a rejection was wanted."""
        executed = 0
        for field, operator, value in itertools.product(self.FIELDS, self.OPERATORS, self.VALUES):
            entry = {"field": field, "operator": operator, "value": value}
            try:
                domain = filters.compile_filters(self.model, [entry], self.spec)
            except FilterError:
                continue
            self.model.search_count(domain)
            executed += 1

        self.assertGreater(executed, 10)
