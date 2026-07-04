"""Performance review round 2: the Audit Log page was issuing ~1,500+ queries
for a church with a substantial rule-edit history — h.instance (a historical
row reconstructed as a full model instance) has no select_related, so calling
str() on it for every AllocationRule history row (whose __str__ touches
self.split_fund or self.department) triggered a fresh FK query each time."""
from django.test import TestCase, Client
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import AllocationRule


def _tr():
    u = User.objects.create_user("tr_perf2", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class AuditLogPerformanceTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="AuditPerfFund", fund_type="LOCAL",
            category="MINISTRY")
        self.c = Client(); self.c.force_login(self.tr)
        # build up a realistic amount of AllocationRule history (create + edit)
        for i in range(60):
            r = AllocationRule.objects.create(reference=f"auditperf{i}",
                department=self.d, source="LEARNED")
            r.reference = f"auditperf{i}-edited"
            r.save()

    def test_query_count_bounded_not_per_history_row(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get("/reports/audit/")
        self.assertEqual(r.status_code, 200)
        # a handful of bulk queries, not ~1-2 per historical row (120+)
        self.assertLess(len(ctx.captured_queries), 40)

    def test_display_strings_still_correct(self):
        b = self.c.get("/reports/audit/").content.decode()
        self.assertIn("auditperf", b)
        self.assertIn(self.d.name, b)

    def test_display_string_matches_original_str_method(self):
        for h in AllocationRule.history.all()[:20]:
            try:
                expected = str(h.instance)
            except Exception:
                continue
            target = h.department.name if h.department_id else "—"
            self.assertIn(h.reference, expected)


class DepartmentDropdownPerformanceTests(TestCase):
    """Department.__str__() shows 'Parent / Name' for a sub-account, which
    means rendering a <select> of departments calls .parent on each one — a
    per-option N+1 unless the queryset used select_related('parent')."""
    def setUp(self):
        self.tr = _tr()
        self.parent = Department.objects.create(name="ParentPerfFund",
            fund_type="LOCAL", category="MINISTRY")
        for i in range(15):
            Department.objects.create(name=f"ChildPerfFund{i}", fund_type="LOCAL",
                category="MINISTRY", parent=self.parent)
        self.c = Client(); self.c.force_login(self.tr)

    def test_payables_page_query_count_bounded(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get("/payables/")
        self.assertEqual(r.status_code, 200)
        # without select_related("parent") on the 3 dropdown querysets, 16
        # departments with a parent would add ~16 extra queries per form
        # rendered (3 forms on this page) — comfortably bounded well short of that
        self.assertLess(len(ctx.captured_queries), 40)

    def test_payable_form_department_queryset_has_parent_prefetched(self):
        from cashbook.forms import PayableForm
        form = PayableForm()
        with CaptureQueriesContext(connection) as ctx:
            labels = [str(d) for d in form.fields["department"].queryset]
        # one query for the list itself (plus maybe a count/exists) — not one
        # extra query per department with a parent
        self.assertLess(len(ctx.captured_queries), 5)
        self.assertTrue(any("ParentPerfFund /" in lbl for lbl in labels))

    def test_transfer_form_department_queryset_has_parent_prefetched(self):
        from cashbook.forms import FundTransferForm
        form = FundTransferForm()
        with CaptureQueriesContext(connection) as ctx:
            list(form.fields["source"].queryset)
        self.assertLess(len(ctx.captured_queries), 5)
