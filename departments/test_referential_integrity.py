"""Database review: DepartmentStatusLog and FundCarryForward both used
on_delete=CASCADE on their department FK, despite being audit-trail records
by their own stated purpose. Changed to PROTECT — deleting a department
should never silently take a permanent audit/history record with it. Safe
change: Department deletion isn't exposed through any application view (only
Transaction/Expense's own PROTECT already made most deletions impossible);
this only closes the narrow remaining gap for departments with no financial
activity left but a status-log or year-end-close history."""
from django.db.models import ProtectedError
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department, DepartmentStatusLog


class DepartmentStatusLogProtectionTests(TestCase):
    def test_status_log_blocks_department_deletion(self):
        tr = User.objects.create_user("tr_dsl_protect", password="x")
        d = Department.objects.create(name="StatusLogProtectFund", fund_type="LOCAL",
            category="MINISTRY")
        DepartmentStatusLog.objects.create(department=d, to_status="ACTIVE", changed_by=tr)
        with self.assertRaises(ProtectedError):
            d.delete()

    def test_department_without_status_log_still_deletable(self):
        d = Department.objects.create(name="NoStatusLogFund", fund_type="LOCAL",
            category="MINISTRY")
        d.delete()   # must not raise
        self.assertFalse(Department.objects.filter(name="NoStatusLogFund").exists())


class FundCarryForwardProtectionTests(TestCase):
    def test_carry_forward_blocks_department_deletion(self):
        from core.models import FundCarryForward, YearEndClose
        tr = User.objects.create_user("tr_fcf_protect", password="x")
        d = Department.objects.create(name="CarryForwardProtectFund", fund_type="LOCAL",
            category="MINISTRY")
        ye = YearEndClose.objects.create(year=2025, closed_by=tr)
        FundCarryForward.objects.create(close=ye, department=d, closing_balance=1000)
        with self.assertRaises(ProtectedError):
            d.delete()

    def test_department_without_carry_forward_still_deletable(self):
        d = Department.objects.create(name="NoCarryForwardFund", fund_type="LOCAL",
            category="MINISTRY")
        d.delete()   # must not raise
        self.assertFalse(Department.objects.filter(name="NoCarryForwardFund").exists())
