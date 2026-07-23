"""Display preferences (v2.96) + leader UI consistency — contracts.

Preferences gained typography (heading typeface, figure typeface) and table
presentation (stripes, gridlines, sticky headers) controls, all flowing through
the same data-attribute mechanism as every existing preference. The leader
pages dropped their one-off green ld-hero banner for the app's statement
masthead. These tests pin both.
"""
import pathlib
import re

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

CSS = (pathlib.Path(__file__).resolve().parent.parent / "static" / "css"
       / "app.css").read_text()


class DisplayPrefContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from core.roles import TREASURER
        cls.user = User.objects.create_user("pref_t", password="x")
        cls.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])

    def setUp(self):
        self.c = Client()
        self.c.force_login(self.user)

    def test_defaults_emit_no_off_attributes(self):
        h = self.c.get("/").content.decode()
        self.assertIn('data-headings="serif"', h)
        self.assertIn('data-figures="mono"', h)
        self.assertIn('data-grid="rows"', h)
        self.assertNotIn("data-stripes", h)      # stripes on by default
        self.assertNotIn("data-stickyhead", h)   # sticky on by default

    def test_live_endpoint_persists_and_emits(self):
        for k, v in [("heading_font", "SANS"), ("figure_font", "BODY"),
                     ("table_grid", "GRID"), ("table_stripes", "false"),
                     ("sticky_headers", "false")]:
            r = self.c.post("/preferences/update/", {"key": k, "value": v})
            self.assertEqual(r.status_code, 200, k)
        h = self.c.get("/").content.decode()
        self.assertIn('data-headings="sans"', h)
        self.assertIn('data-figures="body"', h)
        self.assertIn('data-grid="grid"', h)
        self.assertIn('data-stripes="off"', h)
        self.assertIn('data-stickyhead="off"', h)

    def test_css_rules_exist_for_every_attribute(self):
        for sel in ('html[data-headings="sans"]',
                    'html[data-figures="body"]',
                    'html[data-stripes="off"]',
                    'html[data-grid="grid"]',
                    'html[data-stickyhead="off"]'):
            self.assertIn(sel, CSS, f"no CSS backs {sel} — the preference "
                                    "would save but change nothing")

    def test_form_is_exclude_based_and_complete(self):
        """Anti-frozen-allowlist (rec #114): the preferences form must expose
        every model field except the known non-form ones, automatically."""
        from core.forms import UserPreferenceForm
        from core.models import UserPreference
        non_form = {"id", "user", "dashboard_widgets", "table_state",
                    "updated_at"}
        model_fields = {f.name for f in UserPreference._meta.concrete_fields}
        form_fields = set(UserPreferenceForm().fields)
        self.assertEqual(model_fields - non_form, form_fields,
                         "a preference field is missing from the form")

    def test_prefs_page_renders_all_new_controls(self):
        h = self.c.get("/preferences/").content.decode()
        for key in ("heading_font", "figure_font", "table_grid",
                    "table_stripes", "sticky_headers"):
            self.assertIn(f'data-pref="{key}"', h, key)

    def test_landing_choices_are_real_url_names(self):
        from django.urls import reverse
        from core.models import UserPreference
        for name, _label in UserPreference.LANDING_CHOICES:
            reverse(name)   # raises NoReverseMatch on a bad entry


class LeaderUiConsistencyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from core.roles import LEADER
        from departments.models import Department, DepartmentLeadership
        cls.user = User.objects.create_user("ld_ui", password="x")
        cls.user.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        cls.dept = Department.objects.create(
            name="UI Test Dept", slug="ui-test-dept", category="MINISTRY")
        DepartmentLeadership.objects.create(user=cls.user, department=cls.dept)

    def setUp(self):
        self.c = Client()
        self.c.force_login(self.user)

    def test_leader_pages_carry_the_statement_design(self):
        pages = [f"/leader/department/{self.dept.pk}/",
                 f"/leader/department/{self.dept.pk}/collections/",
                 f"/leader/department/{self.dept.pk}/expenses/",
                 "/leader/advances/", "/leader/loans/",
                 "/leader/?stay=1"]
        for page in pages:
            r = self.c.get(page)
            self.assertEqual(r.status_code, 200, page)
            h = r.content.decode()
            self.assertTrue("rpt-mast" in h or 'class="eyebrow"' in h,
                            f"{page} lacks the statement design")
            self.assertNotIn("ld-hero", h,
                             f"{page} still uses the retired green banner")

    def test_no_ld_hero_anywhere(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = [str(p.relative_to(root))
                     for p in (root / "templates").rglob("*.html")
                     if "ld-hero" in p.read_text()]
        self.assertEqual(offenders, [])
        self.assertNotIn(".ld-hero", CSS)
