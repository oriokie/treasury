"""Per-user Appearance & Preferences: model, persistence, live updates, apply,
reset, landing redirect, dashboard widgets, pagination."""
import json
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from core.models import UserPreference


class PreferenceModelTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("pm", password="x")

    def test_get_for_creates_with_defaults(self):
        pref = UserPreference.get_for(self.u)
        self.assertEqual(pref.theme, "SYSTEM")
        self.assertEqual(pref.accent, "forest")
        self.assertTrue(pref.dashboard_widgets)   # seeded
        self.assertEqual(pref.rows_per_page, 25)

    def test_accent_hex(self):
        pref = UserPreference.get_for(self.u)
        self.assertEqual(pref.accent_hex, "#1f5f4f")
        pref.accent = "plum"
        self.assertEqual(pref.accent_hex, "#6b3b6e")
        pref.accent = "custom"; pref.accent_custom = "#123456"
        self.assertEqual(pref.accent_hex, "#123456")

    def test_merged_widgets_reconciles(self):
        pref = UserPreference.get_for(self.u)
        # an obsolete key is dropped; a missing default is appended
        pref.dashboard_widgets = [{"key": "ghost", "visible": True},
                                  {"key": "kpis", "visible": False}]
        pref.save()
        keys = [w["key"] for w in pref.merged_widgets()]
        self.assertNotIn("ghost", keys)
        self.assertIn("kpis", keys)
        self.assertIn("recent", keys)
        vis = dict((w["key"], w["visible"]) for w in pref.merged_widgets())
        self.assertFalse(vis["kpis"])

    def test_reset_to_defaults(self):
        pref = UserPreference.get_for(self.u)
        pref.theme = "DARK"; pref.accent = "rust"; pref.high_contrast = True
        pref.rows_per_page = 100; pref.save()
        pref.reset_to_defaults()
        self.assertEqual(pref.theme, "SYSTEM")
        self.assertEqual(pref.accent, "forest")
        self.assertFalse(pref.high_contrast)
        self.assertEqual(pref.rows_per_page, 25)


class PreferenceViewTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("pv", password="x", is_superuser=True)
        self.u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)

    def test_page_renders(self):
        b = self.c.get("/preferences/").content.decode()
        self.assertIn("Appearance &amp; Preferences", b)
        self.assertIn("preferences.js", b)
        self.assertIn("accent-picker", b)

    def test_live_update_persists_and_validates(self):
        r = self.c.post("/preferences/update/", {"key": "theme", "value": "DARK"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(UserPreference.get_for(self.u).theme, "DARK")
        # bool coercion
        self.c.post("/preferences/update/", {"key": "high_contrast", "value": "1"})
        self.assertTrue(UserPreference.get_for(self.u).high_contrast)
        # int clamping
        self.c.post("/preferences/update/", {"key": "rows_per_page", "value": "9999"})
        self.assertEqual(UserPreference.get_for(self.u).rows_per_page, 200)
        # unknown key rejected
        self.assertEqual(self.c.post("/preferences/update/",
                         {"key": "evil", "value": "x"}).status_code, 400)

    def test_widget_order_persists(self):
        data = [{"key": "recent", "visible": True}, {"key": "kpis", "visible": False}]
        r = self.c.post("/preferences/update/",
                        {"key": "dashboard_widgets", "value": json.dumps(data)})
        self.assertTrue(r.json()["ok"])
        pref = UserPreference.get_for(self.u)
        self.assertEqual(pref.dashboard_widgets[0]["key"], "recent")
        self.assertFalse(pref.dashboard_widgets[1]["visible"])

    def test_html_reflects_prefs(self):
        pref = UserPreference.get_for(self.u)
        pref.theme = "DARK"; pref.accent = "blue"; pref.high_contrast = True
        pref.layout_width = "FULL"; pref.save()
        b = self.c.get("/").content.decode()
        self.assertIn('data-theme="dark"', b)
        self.assertIn('data-accent="blue"', b)
        self.assertIn('data-contrast="high"', b)
        self.assertIn('data-width="full"', b)

    def test_reset_button(self):
        pref = UserPreference.get_for(self.u); pref.theme = "DARK"; pref.save()
        self.c.post("/preferences/", {"reset": "1"})
        self.assertEqual(UserPreference.get_for(self.u).theme, "SYSTEM")

    def test_landing_redirect(self):
        pref = UserPreference.get_for(self.u)
        pref.landing_page = "expense_list"; pref.save()
        r = self.c.get("/after-login/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("expense", r.url)

    def test_dashboard_widget_hide_and_order(self):
        pref = UserPreference.get_for(self.u)
        pref.dashboard_widgets = [
            {"key": "recent", "visible": True}, {"key": "kpis", "visible": False},
            {"key": "attention", "visible": True}, {"key": "sabbath", "visible": True},
            {"key": "charts", "visible": True}, {"key": "funds", "visible": True},
            {"key": "trend", "visible": True}]
        pref.save()
        b = self.c.get("/").content.decode()
        self.assertNotIn('data-w="kpis"', b)               # hidden
        self.assertIn('data-w="recent" style="order:0"', b)  # reordered first

    def test_toasts_config_exposed(self):
        b = self.c.get("/").content.decode()
        self.assertIn("window.__prefs", b)
        self.assertIn("toast-host", b)


class PaginationPrefTests(TestCase):
    def test_rows_per_page_applied(self):
        from django.test import RequestFactory
        from giving.views import TransactionListView
        u = User.objects.create_user("pp", password="x", is_superuser=True)
        pref = UserPreference.get_for(u); pref.rows_per_page = 15; pref.save()
        req = RequestFactory().get("/transactions/"); req.user = u
        v = TransactionListView(); v.request = req
        self.assertEqual(v.get_paginate_by(None), 15)
