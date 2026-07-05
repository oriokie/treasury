"""Production incident: /ledger/rebuild/ failed with
IntegrityError: (1062, "Duplicate entry '5105' for key 'code'").

Root cause: ensure_chart() assigned each Expense.Category's account code from
its POSITION in Expense.Category.choices (EXPENSE_BASE + enumerate index).
A prior release inserted two new categories (SALARIES, LEASE) in the middle
of that list, which shifted the positional index — and therefore the
computed code — of every category listed after them. On any database that
already had its chart of accounts built before that release (i.e. any real
deployment), UTILITIES already held code 5105 on disk; after the insertion,
SALARIES's newly-computed code was ALSO 5105 (its new position), so creating
it collided with UTILITIES's existing row.

Fixed by assigning each new category's code as "one past the highest code
already on record" instead of from list position — immune to future
reordering, and an existing category's code, once assigned, is never
recomputed or reused."""
from django.db import IntegrityError
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from ledger.models import Account
from ledger.services.posting import ensure_chart, rebuild, EXPENSE_BASE
from cashbook.models import Expense


def _tr():
    u = User.objects.create_user("tr_chart_stability", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _simulate_pre_release_state():
    """Reconstruct exactly the production state that triggered the bug: the
    chart was built before SALARIES/LEASE existed, so categories after them
    in the current choices list hold the OLD, lower, index-based codes."""
    Account.objects.filter(system_key__in=["EXP_SALARIES", "EXP_LEASE"]).delete()
    old_codes = {"EXP_UTILITIES": "5105", "EXP_MAINTENANCE": "5106",
                 "EXP_CONSTRUCTION": "5107", "EXP_EVANGELISM": "5108",
                 "EXP_BENEVOLENCE": "5109", "EXP_BANK_CHARGE": "5110",
                 "EXP_REMITTANCE": "5111", "EXP_OTHER": "5112"}
    for key, code in old_codes.items():
        Account.objects.filter(system_key=key).update(code=code)


class ChartCodeStabilityTests(TestCase):
    def setUp(self):
        ensure_chart()

    def test_reproduces_and_resolves_the_exact_incident(self):
        _simulate_pre_release_state()
        utilities_code_before = Account.objects.get(system_key="EXP_UTILITIES").code
        self.assertEqual(utilities_code_before, "5105")

        try:
            ensure_chart()
        except IntegrityError:
            self.fail("ensure_chart() raised IntegrityError — the production incident recurred")

        # the pre-existing category's code must be completely untouched
        self.assertEqual(Account.objects.get(system_key="EXP_UTILITIES").code, "5105")
        # the new categories must have gotten a genuinely free code, not one
        # that collides with anything already on disk
        salaries_code = Account.objects.get(system_key="EXP_SALARIES").code
        lease_code = Account.objects.get(system_key="EXP_LEASE").code
        self.assertNotEqual(salaries_code, "5105")
        codes = list(Account.objects.values_list("code", flat=True))
        self.assertEqual(len(codes), len(set(codes)), "duplicate account codes exist")

    def test_rebuild_succeeds_from_the_pre_release_state(self):
        _simulate_pre_release_state()
        n = rebuild()   # must not raise
        self.assertGreaterEqual(n, 0)
        self.assertTrue(
            Account.objects.filter(system_key="EXP_SALARIES").exists())
        self.assertTrue(
            Account.objects.filter(system_key="EXP_LEASE").exists())

    def test_rebuild_endpoint_succeeds_from_the_pre_release_state(self):
        _simulate_pre_release_state()
        tr = _tr()
        c = Client(); c.force_login(tr)
        r = c.post("/ledger/rebuild/", follow=True)
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("rebuilt from source documents", b)
        self.assertNotIn("Server Error", b)

    def test_every_category_gets_an_account_with_no_duplicates(self):
        ensure_chart()
        for val, _ in Expense.Category.choices:
            self.assertTrue(Account.objects.filter(system_key=f"EXP_{val}").exists())
        codes = list(Account.objects.filter(code__gte=str(EXPENSE_BASE))
                     .values_list("code", flat=True))
        self.assertEqual(len(codes), len(set(codes)))

    def test_running_ensure_chart_twice_is_stable(self):
        ensure_chart()
        codes_first = dict(Account.objects.values_list("system_key", "code"))
        ensure_chart()
        codes_second = dict(Account.objects.values_list("system_key", "code"))
        self.assertEqual(codes_first, codes_second)

    def test_new_category_added_later_gets_a_free_code_not_a_positional_one(self):
        # simulate adding a brand-new category that doesn't exist yet at all
        Account.objects.filter(system_key="EXP_OTHER").delete()
        max_code_before = max(int(c) for c in
            Account.objects.values_list("code", flat=True) if c.isdigit())
        from cashbook.models import Expense as _E
        Account.objects.get_or_create(system_key="EXP_OTHER",
            defaults={"code": "9999", "name": "placeholder", "type": "EXPENSE"})
        Account.objects.filter(system_key="EXP_OTHER").delete()
        ensure_chart()
        new_code = int(Account.objects.get(system_key="EXP_OTHER").code)
        self.assertGreater(new_code, max_code_before)
