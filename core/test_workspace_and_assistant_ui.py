"""The Treasurer workspace and the Assistant, after their UI rework.

The workspace bug worth a permanent guard: the page rendered only
`high_priority` — the top 12 insights — while the scorecard beside it
announced the full count (44 on the demo data). The other 32 were computed,
counted, and unreachable. `by_category` was even built in the view for this
and never used by the template. `by_severity` now covers every insight, and
the invariant below holds whatever the engine happens to find.

Also guarded: the severity→pill mapping. The old template read

    {% if r.severity == 'critical' or r.severity == 'warning' %}{% endif %}

— an empty branch, so a *critical* recommendation got no pill class at all
while merely informational ones got `pill-grey`. The urgent items were the
only ones with no colour.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER


def _treasurer(username="ws_tr"):
    u = User.objects.create_user(username, password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class WorkspaceInsightCoverageTests(TestCase):
    def setUp(self):
        self.client.force_login(_treasurer())

    def test_page_renders(self):
        r = self.client.get(reverse("treasurer_workspace"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Treasurer workspace")

    def test_every_insight_is_reachable_in_some_severity_band(self):
        """The regression this file exists for: bands must account for the
        whole insight list, not a top-N slice of it."""
        r = self.client.get(reverse("treasurer_workspace"))
        insights = r.context["insights"]
        banded = sum(b["count"] for b in r.context["by_severity"])
        self.assertEqual(banded, len(insights))

    def test_band_counts_match_the_items_they_carry(self):
        r = self.client.get(reverse("treasurer_workspace"))
        for band in r.context["by_severity"]:
            self.assertEqual(band["count"], len(band["items"]), band["label"])

    def test_bands_are_ordered_most_serious_first(self):
        r = self.client.get(reverse("treasurer_workspace"))
        order = [b["key"] for b in r.context["by_severity"]]
        expected = [k for k in ("critical", "warning", "notice", "info")
                    if k in order]
        self.assertEqual(order, expected)

    def test_items_within_a_band_are_highest_priority_first(self):
        r = self.client.get(reverse("treasurer_workspace"))
        for band in r.context["by_severity"]:
            prios = [i.priority for i in band["items"]]
            self.assertEqual(prios, sorted(prios, reverse=True), band["label"])

    def test_only_non_empty_bands_are_offered(self):
        r = self.client.get(reverse("treasurer_workspace"))
        for band in r.context["by_severity"]:
            self.assertTrue(band["items"], f"{band['label']} band is empty")

    def test_risk_band_is_derived_from_the_risk_score(self):
        r = self.client.get(reverse("treasurer_workspace"))
        score, band = r.context["risk_score"], r.context["risk_band"]
        expected = "High" if score >= 60 else "Moderate" if score >= 30 else "Low"
        self.assertEqual(band, expected)


class WorkspaceMarkupTests(TestCase):
    def setUp(self):
        self.client.force_login(_treasurer("ws_tr2"))

    def test_no_undefined_css_classes_remain(self):
        """`u-sm` and `btn-link` are both on the KNOWN_UNDEFINED ratchet in
        core.test_css_contract — nothing defines them, so the acknowledge /
        resolve / dismiss controls rendered as bare browser buttons. The
        rework replaced them with styled classes defined on the page."""
        body = self.client.get(reverse("treasurer_workspace")).content.decode()
        self.assertNotIn('class="u-sm"', body)
        self.assertNotIn('btn-link', body)

    def test_insight_actions_are_present_and_styled(self):
        body = self.client.get(reverse("treasurer_workspace")).content.decode()
        self.assertIn("ins-act", body)
        for label in ("Acknowledge", "Resolve", "Dismiss"):
            self.assertIn(label, body)

    def test_a_critical_recommendation_gets_a_visible_pill_not_a_blank_class(self):
        """Directly renders the mapping the old empty {% if %} broke."""
        from django.template import Context, Template
        tpl = Template(
            "{% if r.severity == 'critical' %}pill-red"
            "{% elif r.severity == 'warning' %}pill-amber"
            "{% else %}pill-grey{% endif %}")
        for sev, want in (("critical", "pill-red"), ("warning", "pill-amber"),
                          ("info", "pill-grey"), ("notice", "pill-grey")):
            got = tpl.render(Context({"r": type("R", (), {"severity": sev})()}))
            self.assertEqual(got, want, sev)


class WorkspaceAccessTests(TestCase):
    def test_anonymous_is_redirected(self):
        r = self.client.get(reverse("treasurer_workspace"))
        self.assertEqual(r.status_code, 302)

    def test_a_portal_member_may_not_read_it(self):
        from core.roles import MEMBER
        u = User.objects.create_user("ws_member", password="x")
        u.groups.add(Group.objects.get_or_create(name=MEMBER)[0])
        self.client.force_login(u)
        r = self.client.get(reverse("treasurer_workspace"))
        self.assertNotEqual(r.status_code, 200)


class AssistantPageTests(TestCase):
    def setUp(self):
        self.u = _treasurer("as_tr")
        self.client.force_login(self.u)

    def test_page_renders_with_suggestion_groups(self):
        r = self.client.get(reverse("assistant"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Treasury assistant")
        self.assertTrue(r.context["suggestion_groups"])
        self.assertContains(r, "asst-group")

    def test_badge_reports_ai_assist_off(self):
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.llm_enabled = False; cfg.save()
        body = self.client.get(reverse("assistant")).content.decode()
        self.assertIn("Rules only", body)
        self.assertNotIn("AI assist on", body)

    def test_badge_reports_ai_assist_on(self):
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.llm_enabled = True; cfg.save()
        body = self.client.get(reverse("assistant")).content.decode()
        self.assertIn("AI assist on", body)

    def test_the_empty_state_is_a_flex_block_so_clear_can_restore_it(self):
        """Clear sets `empty.style.display = ""`, which falls back to the
        stylesheet. The rule has to exist, or clearing the conversation would
        leave the panel blank."""
        body = self.client.get(reverse("assistant")).content.decode()
        self.assertIn(".asst-empty{display:flex", body)

    def test_report_context_banner_when_opened_from_a_report(self):
        r = self.client.get(reverse("assistant"), {
            "report_key": "income_statement", "start": "2026-01-01",
            "end": "2026-01-31"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "income_statement")
        self.assertContains(r, "ctxBanner")

    def test_ask_endpoint_answers_from_live_data(self):
        import json
        r = self.client.post(reverse("assistant_ask"),
                             data=json.dumps({"q": "total collections this month"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text", r.json())
