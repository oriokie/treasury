"""#3 dev-offering right, #4 assignable rights, #5 petty charge flag,
#6 assistant, #7 balanced dev-group builder."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department, DevelopmentGroup
from members.models import Member
from giving.models import Transaction
from cashbook.models import StaffAdvance, PettyCashTopUp
from accounts.models import Profile


def _leader(username, rights=None):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name="Leader")[0])
    if rights:
        p = Profile.objects.create(name=username + "-prof",
                                   rights=rights + ["view_reports"])
        u.profiles.add(p)
    return u


class RightsTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr", password="x", is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])

    def test_dev_offering_right_grants_access(self):
        c = Client(); c.force_login(_leader("devlead", ["allocate_dev_offering"]))
        self.assertEqual(c.get("/reports/dev-groups/unassigned/").status_code, 200)

    def test_without_right_blocked(self):
        c = Client(); c.force_login(_leader("plain"))
        self.assertEqual(c.get("/reports/dev-groups/unassigned/").status_code, 302)

    def test_advances_right_grants_access(self):
        c = Client(); c.force_login(_leader("advonly", ["manage_advances"]))
        self.assertEqual(c.get("/advances/new/").status_code, 200)

    def test_treasurer_has_all_rights(self):
        from core.roles import (can_allocate_dev_offering, can_manage_advances,
                                 can_build_dev_groups)
        self.assertTrue(can_allocate_dev_offering(self.tr))
        self.assertTrue(can_manage_advances(self.tr))
        self.assertTrue(can_build_dev_groups(self.tr))

    def test_builder_right_required(self):
        c = Client(); c.force_login(_leader("nobuild"))
        self.assertEqual(c.get("/reports/dev-groups/build/").status_code, 302)


class PettyChargeTests(TestCase):
    def test_petty_advance_charge_is_petty_flagged(self):
        tr = User.objects.create_user("trp", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        dept = Department.objects.create(name="Y", fund_type="LOCAL",
            category="MINISTRY", show_in_expenses=True)
        PettyCashTopUp.objects.create(date=dt.date(2026, 6, 1),
            amount=Decimal("30000"), recorded_by=tr)
        c = Client(); c.force_login(tr)
        c.post("/advances/new/", {"staff_name": "P", "department": dept.id,
            "amount": "5000", "date_issued": "2026-06-05", "method": "CASH",
            "bank_charge": "50", "from_petty_cash": "1", "purpose": "x"})
        adv = StaffAdvance.objects.get(staff_name="P")
        self.assertTrue(adv.charge_expense.paid_from_petty_cash)


class DevGroupBuilderTests(TestCase):
    def test_builds_balanced_groups(self):
        from core.models import SiteConfig
        tr = User.objects.create_user("trb", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        cfg = SiteConfig.get(); cfg.dev_group_builder_apply = True; cfg.save()
        devfund = Department.objects.create(name="Development", fund_type="LOCAL",
            category="DEVELOPMENT")
        for i in range(12):
            m = Member.objects.create(name=f"M{i}", active=True)
            Transaction.objects.create(date=dt.date(2026, 5, 1),
                amount=Decimal(str((i + 1) * 100)), department=devfund,
                direction="CREDIT", confirmed=True, member=m, channel="BANK",
                allocation_status="MANUAL")
        c = Client(); c.force_login(tr)
        self.assertEqual(c.get("/reports/dev-groups/build/?n=4").status_code, 200)
        c.post("/reports/dev-groups/build/", {"n": "4", "prefix": "Team"})
        self.assertEqual(DevelopmentGroup.objects.filter(
            name__startswith="Team", active=True).count(), 4)
        self.assertEqual(Member.objects.filter(
            dev_group__name__startswith="Team").count(), 12)
        from django.db.models import Sum
        caps = [Transaction.objects.filter(member__dev_group=g).aggregate(
            t=Sum("amount"))["t"] or Decimal(0)
            for g in DevelopmentGroup.objects.filter(name__startswith="Team")]
        total = sum(caps, Decimal(0))
        self.assertEqual(total, Decimal("7800"))
        self.assertLess(max(caps) - min(caps), total / 4)

    def test_apply_off_by_default_downloads_only(self):
        # with the live action disabled (default), POST changes nothing but the
        # Excel/CSV download still works and includes phones
        tr = User.objects.create_user("trd", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        devfund = Department.objects.create(name="Development", fund_type="LOCAL",
            category="DEVELOPMENT")
        m = Member.objects.create(name="Phoned", phone="254712345678", active=True)
        Transaction.objects.create(date=dt.date(2026, 5, 1), amount=Decimal("500"),
            department=devfund, direction="CREDIT", confirmed=True, member=m,
            channel="BANK", allocation_status="MANUAL")
        c = Client(); c.force_login(tr)
        c.post("/reports/dev-groups/build/", {"n": "2", "prefix": "Z"})
        self.assertEqual(DevelopmentGroup.objects.filter(name__startswith="Z").count(), 0)
        r = c.get("/reports/dev-groups/build/?n=2&export=csv")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Phone", body)
        self.assertIn("254712345678", body)
        self.assertEqual(c.get(
            "/reports/dev-groups/build/?n=2&export=xlsx").status_code, 200)

    def test_builder_rejects_bad_n(self):
        tr = User.objects.create_user("trb2", password="x", is_superuser=True)
        tr.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        c = Client(); c.force_login(tr)
        c.post("/reports/dev-groups/build/", {"n": "1"})
        self.assertEqual(DevelopmentGroup.objects.count(), 0)


class AssistantTests(TestCase):
    def test_advances_and_petty_intents(self):
        from core.services.assistant import answer
        self.assertIn("advances outstanding",
                      answer("staff advances outstanding")["text"].lower())
        self.assertIn("petty cash", answer("petty cash balance")["text"].lower())
