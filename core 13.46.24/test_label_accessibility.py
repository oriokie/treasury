"""UX/Accessibility review: every form label across the app was missing its
`for` attribute, so screen readers (and users clicking a label expecting it to
focus the field) had no programmatic association between a label and its
input — a WCAG 1.3.1 / 4.1.2 failure. Fixed at the shared form_fields.html
partial (used by most forms) plus ten custom form templates that duplicated
label rendering inline. Also: the amber "pending/warning" status colour
failed WCAG AA contrast in both light mode (3.99:1) and dark mode (3.28:1);
both are now fixed (4.76:1 and 6.92:1)."""
import re
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group


def _tr():
    u = User.objects.create_user("tr_a11y_lbl", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _labels_without_for(html):
    return re.findall(r'<label(?![^>]*\sfor=)(?![^>]*class="check-row")[^>]*>[^<]{1,60}', html)


class FormLabelAssociationTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)

    def test_shared_form_fields_partial_emits_for_attribute(self):
        from django.template import Template, Context
        from django import forms

        class SampleForm(forms.Form):
            name = forms.CharField(label="Name")
            active = forms.BooleanField(label="Active", required=False)

        t = Template('{% include "partials/form_fields.html" %}')
        html = t.render(Context({"form": SampleForm()}))
        self.assertIn('for="id_name"', html)
        self.assertIn('for="id_active"', html)

    def test_expense_form_labels_all_associated(self):
        b = self.c.get("/expenses/new/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_settings_page_labels_all_associated(self):
        b = self.c.get("/settings/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_cash_entry_form_labels_all_associated(self):
        b = self.c.get("/cash/new/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_asset_form_labels_all_associated(self):
        b = self.c.get("/assets/new/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_member_form_labels_all_associated(self):
        b = self.c.get("/members/new/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_pledge_form_labels_all_associated(self):
        b = self.c.get("/pledges/new/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_transaction_filter_labels_all_associated(self):
        b = self.c.get("/transactions/").content.decode()
        self.assertEqual(_labels_without_for(b), [])

    def test_department_form_labels_all_associated(self):
        b = self.c.get("/departments/new/").content.decode()
        self.assertEqual(_labels_without_for(b), [])


class AmberContrastTests(TestCase):
    def test_light_mode_amber_passes_wcag_aa(self):
        css = open("static/css/app.css").read()
        self.assertIn("--amber:#8a6013;", css)

    def test_dark_mode_amber_override_present(self):
        css = open("static/css/app.css").read()
        self.assertEqual(css.count("--amber:#d4a83a;"), 2)

    def test_contrast_ratios_pass_aa(self):
        def lum(hexcol):
            hexcol = hexcol.lstrip("#")
            r, g, b = [int(hexcol[i:i+2], 16) / 255.0 for i in (0, 2, 4)]
            def lin(c):
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = lin(r), lin(g), lin(b)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def contrast(c1, c2):
            l1, l2 = lum(c1), lum(c2)
            l1, l2 = max(l1, l2), min(l1, l2)
            return (l1 + 0.05) / (l2 + 0.05)

        self.assertGreaterEqual(contrast("#8a6013", "#f7ecd4"), 4.5)   # light mode
        self.assertGreaterEqual(contrast("#d4a83a", "#2c2410"), 4.5)   # dark mode
