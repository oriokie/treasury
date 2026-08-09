"""The pledge-campaign screen has to be in the sidebar, and one link at a time.

/pledges/campaigns/ (``pledges.views.CampaignListView``) shipped complete — the
view, its template, the create/edit form beside it, and tests that all passed —
and then no template in the application ever linked to it. A treasurer's only
route in was to type the URL. Recommendation #126 called that failure mode — a
built screen with no door — its fourth instance, after #121, #122 and #125; this
is another, and what let every one of them through was a test that
``reverse()``d the name, requested the path and called the screen reachable.
That is the one step a real user cannot take. So these tests read the sidebar
out of a rendered page, and none of them treats ``reverse()`` as evidence.

Each assertion below was watched to fail with the template reverted, per the
standing rule from #130: a guard nobody has broken on purpose is not yet a guard.

The second half is the collision adding the link exposed. The sidebar decided
its active state with substring checks, and 'campaign' is a substring of
'pledge_campaign_list': opening a pledge campaign already highlighted the
unrelated giving Campaigns entry (a different model — the allocation fallback),
and with a pledge-campaign link present it would have highlighted both of them
and Pledges as well. Three lit links tell a treasurer nothing about where they
are.
"""
import re

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import AUDITOR, LEADER, TREASURER


def _user(username, role):
    u = User.objects.create_user(username, password="nav-camp-pass-1")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


def _people_links(body):
    """The People navgrp only — a link elsewhere on the page is not this menu."""
    return body.split('data-grp="people"')[1].split("</details>")[0]


def _link_class(body, href):
    """The class attribute of the sidebar link to `href`.

    A missing link raises rather than returning None: "no link at all" must
    never read as "link present but not highlighted", which is exactly how an
    assertNotIn would have scored it — a quiet pass on the disappearance this
    whole file exists to catch.
    """
    m = re.search(r'<a href="%s"[^>]*class="([^"]*)"' % re.escape(href), body)
    if m is None:
        raise AssertionError("no sidebar link to %s on this page" % href)
    return m.group(1)


class PledgeCampaignSidebarTests(TestCase):
    """A treasurer at the dashboard can see the way in."""

    def setUp(self):
        self.tr = _user("navcamp_tr", TREASURER)
        self.client = Client()
        self.client.force_login(self.tr)

    def _sidebar(self):
        return self.client.get(reverse("dashboard")).content.decode()

    def test_the_pledge_campaign_list_is_in_the_menu(self):
        self.assertIn(
            'href="%s"' % reverse("pledge_campaign_list"), self._sidebar(),
            "The pledge-campaign list is built, gated and tested but appears "
            "in no menu, so a treasurer cannot reach it without typing the URL.")

    def test_it_sits_in_People_beside_the_pledge_dashboard(self):
        """Its siblings are the screens it is about, not a group of its own."""
        people = _people_links(self._sidebar())
        self.assertIn('href="%s"' % reverse("pledge_dashboard"), people)
        self.assertIn('href="%s"' % reverse("pledge_campaign_list"), people)

    def test_the_link_opens_the_page_it_points_at(self):
        """A menu entry that 404s or bounces is no better than no entry."""
        r = self.client.get(reverse("pledge_campaign_list"))
        self.assertEqual(r.status_code, 200)
        self.assertTemplateUsed(r, "pledges/campaign_list.html")

    def test_an_auditor_sees_it_because_an_auditor_may_open_it(self):
        """ReadAccessMixin admits Treasurer/Assistant/Auditor/admin alike, so the
        menu must not be narrower than the gate — an auditor who cannot see the
        link is back to typing the URL."""
        client = Client()
        client.force_login(_user("navcamp_aud", AUDITOR))
        self.assertEqual(client.get(reverse("pledge_campaign_list")).status_code, 200)
        self.assertIn('href="%s"' % reverse("pledge_campaign_list"),
                      client.get(reverse("dashboard")).content.decode())

    def test_a_department_leader_neither_sees_it_nor_may_open_it(self):
        """ReadAccessMixin excludes leaders on purpose (they get the scoped
        leader views instead), so the menu must not offer them a link whose only
        destination is a redirect away."""
        client = Client()
        client.force_login(_user("navcamp_lead", LEADER))
        r = client.get(reverse("pledge_campaign_list"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, reverse("leader_dashboard"))
        self.assertNotIn('href="%s"' % reverse("pledge_campaign_list"),
                         client.get(reverse("dashboard")).content.decode())


class CampaignActiveStateTests(TestCase):
    """Two different models are called a campaign; two links, one lit at a time."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(_user("navcamp_tr2", TREASURER))

    def test_only_the_pledge_link_lights_up_on_a_pledge_campaign_page(self):
        body = self.client.get(reverse("pledge_campaign_list")).content.decode()
        self.assertIn("active", _link_class(body, reverse("pledge_campaign_list")),
                      "the page you are on is the one link that should be lit")
        self.assertNotIn(
            "active", _link_class(body, reverse("campaign_list")),
            "the giving Campaigns link is lit on a pledge-campaign page — "
            "'campaign' is a substring of 'pledge_campaign_list'")
        self.assertNotIn(
            "active", _link_class(body, reverse("pledge_dashboard")),
            "the Pledges link is lit on a pledge-campaign page — 'pledge' is a "
            "substring of 'pledge_campaign_list'")

    def test_the_giving_campaign_link_still_lights_up_on_its_own_page(self):
        """The narrowing must not cost the link that already worked."""
        body = self.client.get(reverse("campaign_list")).content.decode()
        self.assertIn("active", _link_class(body, reverse("campaign_list")),
                      "narrowing the test put out the link it was meant to keep")
        self.assertNotIn("active", _link_class(body, reverse("pledge_campaign_list")))

    def test_the_pledge_dashboard_link_still_lights_up_on_its_own_page(self):
        body = self.client.get(reverse("pledge_dashboard")).content.decode()
        self.assertIn("active", _link_class(body, reverse("pledge_dashboard")))
        self.assertNotIn("active", _link_class(body, reverse("pledge_campaign_list")))
