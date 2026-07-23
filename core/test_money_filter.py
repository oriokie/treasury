"""Central money filter (v2.97).

`money` replaced the `{{ x|floatformat:N|intcomma }}` pattern that lived in ~167
templates. These tests pin two things:

1. **Parity** — `money` is byte-identical to floatformat+intcomma over a wide
   battery of values, so the mass migration changed nothing a user sees. If a
   future edit to the filter breaks parity, the display of every figure in the
   app would shift; this test fails first.
2. **No regression to the old pattern** — the scattered pattern must not creep
   back into templates, and every template using the filter must load it.

Plus `money_acct` (accounting parentheses) behaviour and the currency-symbol
consolidation (one config-driven symbol, not a hardcoded KES/KSh split).
"""
import pathlib
import re
from decimal import Decimal

from django.contrib.humanize.templatetags.humanize import intcomma
from django.template.defaultfilters import floatformat
from django.test import SimpleTestCase, TestCase

from core.templatetags.treasury_extras import money, money_acct

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"


class MoneyParityTests(SimpleTestCase):
    def _ref(self, v, p):
        want = intcomma(floatformat(v, p))
        return want.lstrip("-") if want in ("-0", "-0.00") else want

    def test_identical_to_floatformat_intcomma(self):
        import random
        vals = [0, 1, -1, 1234.5, -1234.5, 1234.567, -1234.564, 999999.995,
                -999999.995, 0.004, -0.004, 0.005, -0.005, 1_000_000, 12.345,
                50, 2500.5, 123456789.99, -50.5, Decimal("1234567.891"),
                0.1, 0.15, 0.25, 2.675, Decimal("0.00"), Decimal("-0.00")]
        random.seed(7)
        for _ in range(500):
            vals.append(round(random.uniform(-5e6, 5e6), random.randint(0, 4)))
        mism = [(v, p, money(v, p), self._ref(v, p))
                for v in vals for p in (0, 2)
                if money(v, p) != self._ref(v, p)]
        self.assertEqual(mism, [], f"{len(mism)} parity mismatches, e.g. {mism[:5]}")

    def test_blank_and_nonnumeric(self):
        for v in (None, "", "abc", [], {}):
            self.assertEqual(money(v), "—")

    def test_rounds_to_zero_has_no_sign(self):
        self.assertEqual(money(Decimal("-0.004")), "0.00")
        self.assertEqual(money_acct(Decimal("-0.004")), "0.00")

    def test_places_argument(self):
        self.assertEqual(money(1234.5, 0), "1,235")
        self.assertEqual(money(1234.5), "1,234.50")

    def test_accounting_parentheses(self):
        self.assertEqual(money_acct(-1234.5), "(1,234.50)")
        self.assertEqual(money_acct(1234.5), "1,234.50")
        self.assertEqual(money_acct(-1234.5, 0), "(1,235)")
        self.assertEqual(money_acct(None), "—")


class MoneyMigrationGuardTests(SimpleTestCase):
    def test_old_pattern_gone_from_templates(self):
        offenders = [str(p.relative_to(TEMPLATES.parent))
                     for p in TEMPLATES.rglob("*.html")
                     if re.search(r"floatformat:[02]\|intcomma", p.read_text())]
        self.assertEqual(offenders, [],
                         "the floatformat|intcomma money pattern is back — use "
                         "the money filter instead: %s" % offenders)

    def test_money_users_load_the_tag(self):
        missing = []
        for p in TEMPLATES.rglob("*.html"):
            s = p.read_text()
            if re.search(r"\|money(:0)?\b|\|money_acct\b", s):
                if not re.search(r"{%\s*load\s+[^%]*\btreasury_extras\b", s):
                    missing.append(str(p.relative_to(TEMPLATES.parent)))
        self.assertEqual(missing, [], "templates use money without loading it: %s" % missing)


class CurrencyConsolidationTests(TestCase):
    def test_no_hardcoded_symbol_beside_a_figure(self):
        """A currency symbol sitting directly before a `{{ figure }}` must come
        from CURRENCY, not a hardcoded KES/KSh (which ignored the site config
        and split 116/61 across the app)."""
        offenders = []
        for p in TEMPLATES.rglob("*.html"):
            for m in re.finditer(r"\b(KES|KSh|Ksh)\s\{\{", p.read_text()):
                offenders.append(f"{p.name}: {m.group(0)}")
        self.assertEqual(offenders, [],
                         "hardcoded currency symbol before a figure: %s" % offenders)

    def test_currency_renders_from_config(self):
        from django.test import Client
        from django.contrib.auth.models import Group, User
        from core.roles import TREASURER
        u = User.objects.create_user("cur_t", password="x")
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        c = Client(); c.force_login(u)
        h = c.get("/departments/").content.decode()
        self.assertNotIn(">KES<", h)  # no stray hardcoded ISO code in markup
