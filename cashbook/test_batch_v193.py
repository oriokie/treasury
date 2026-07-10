"""v1.93 batch: fund sub-account opening column, payables tabs, settings tab
persistence, advance top-up double-entry + reversal."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import SiteConfig
from departments.models import Department
from cashbook.models import StaffAdvance, AdvanceTopUp, PettyCashTopUp
from cashbook.views import _petty_balance_asof
from ledger.services.posting import ensure_chart


def _tr(name="tr_193"):
    u = User.objects.create_user(name, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class FundOpeningColumnTests(TestCase):
    def setUp(self):
        self.c = Client(); self.c.force_login(_tr())
        self.parent = Department.objects.create(name="Parent F", fund_type="LOCAL",
            category="OFFERING", opening_balance=Decimal("1000"))
        self.sub = Department.objects.create(name="Sub F", fund_type="LOCAL",
            category="OFFERING", opening_balance=Decimal("500"), parent=self.parent)

    def test_subaccount_opening_shown(self):
        body = self.c.get(f"/reports/fund/{self.parent.id}/").content.decode()
        self.assertIn(">Opening</th>", body)

    def test_subgroup_export_has_opening(self):
        r = self.c.get(f"/reports/fund/{self.parent.id}/?export=subgroups-csv")
        self.assertIn("Opening", r.content.decode())


class PayablesTabsTests(TestCase):
    def setUp(self):
        self.c = Client(); self.c.force_login(_tr())

    def test_tabs_present(self):
        body = self.c.get("/payables/").content.decode()
        self.assertIn('class="tabbar', body)
        self.assertEqual(body.count('class="tab-btn'), 3)
        self.assertEqual(body.count('class="tab-panel"'), 3)
        self.assertIn('data-panel="accruals" hidden', body)


class SettingsPersistenceTests(TestCase):
    def setUp(self):
        self.c = Client(); self.c.force_login(_tr())

    def test_tab_param_sets_hidden_input(self):
        body = self.c.get("/settings/?tab=features").content.decode()
        self.assertIn('id="activeTab"', body)
        self.assertIn('value="features"', body)

    def test_save_preserves_tab(self):
        cfg = SiteConfig.get()
        from core.forms import SiteConfigForm
        form = SiteConfigForm(instance=cfg)
        data = {}
        for name in form.fields:
            val = form[name].value()
            if val is None:
                val = ""
            if isinstance(val, bool):
                if val:
                    data[name] = "on"
            else:
                data[name] = val
        data["active_tab"] = "approvals"
        r = self.c.post("/settings/", data)
        self.assertEqual(r.status_code, 302)
        self.assertIn("tab=approvals", r["Location"])

    def test_sidebar_scroll_js_present(self):
        body = self.c.get("/").content.decode()
        self.assertIn("sidebar-scroll", body)


class AdvanceTopUpTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr(); self.c = Client(); self.c.force_login(self.tr)
        self.d = Department.objects.create(name="Fund T", fund_type="LOCAL",
            category="OFFERING", show_in_expenses=True)
        PettyCashTopUp.objects.create(date=dt.date(2026, 5, 1),
            amount=Decimal("50000"), recorded_by=self.tr)
        self.adv = StaffAdvance.objects.create(staff_name="Top", department=self.d,
            amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1), purpose="fuel",
            method="CASH", from_petty_cash=True, issued_by=self.tr)

    def test_topup_reduces_petty(self):
        before = _petty_balance_asof(dt.date(2026, 6, 30))
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"amount": "2000", "date": "2026-06-10", "note": "more"})
        self.adv.refresh_from_db()
        after = _petty_balance_asof(dt.date(2026, 6, 30))
        self.assertEqual(self.adv.amount, Decimal("5000"))
        self.assertEqual(before - after, Decimal("2000"))

    def test_topup_in_petty_register(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"amount": "2000", "date": "2026-06-10"})
        reg = self.c.get("/petty-cash/?start=2026-06-01&end=2026-06-30").content.decode()
        self.assertIn("Advance top-up", reg)

    def test_reverse_topup_restores_source(self):
        before = _petty_balance_asof(dt.date(2026, 6, 30))
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"amount": "2000", "date": "2026-06-10"})
        tu = AdvanceTopUp.objects.filter(advance=self.adv).first()
        self.c.post(f"/advances/{self.adv.id}/topup/{tu.id}/reverse/")
        self.adv.refresh_from_db()
        self.assertEqual(self.adv.amount, Decimal("3000"))
        self.assertEqual(_petty_balance_asof(dt.date(2026, 6, 30)), before)
        self.assertFalse(AdvanceTopUp.objects.filter(id=tu.id).exists())

    def test_reverse_requires_treasurer(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"amount": "2000", "date": "2026-06-10"})
        tu = AdvanceTopUp.objects.filter(advance=self.adv).first()
        asst = User.objects.create_user("asst_t", password="x")
        asst.groups.add(Group.objects.get_or_create(name="Assistant")[0])
        c2 = Client(); c2.force_login(asst)
        r = c2.post(f"/advances/{self.adv.id}/topup/{tu.id}/reverse/")
        self.assertIn(r.status_code, (302, 403))
        self.assertTrue(AdvanceTopUp.objects.filter(id=tu.id).exists())  # not reversed
