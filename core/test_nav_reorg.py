"""Nav follow-up: split People/Funds, moved Trust remittance to Banking,
folded Allocation rules + Dev-group patterns into Funds & setup."""
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group


def _treasurer():
    u = User.objects.create_user("tr_nav2", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class NavReorgTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.c = Client(); self.c.force_login(self.tr)

    def test_people_and_funds_split(self):
        body = self.c.get("/").content.decode()
        self.assertIn('data-grp="people"', body)
        self.assertIn('data-grp="funds"', body)
        self.assertIn("People <span", body)
        self.assertIn("Funds &amp; setup", body)

    def test_remittance_moved_to_banking(self):
        body = self.c.get("/").content.decode()
        banking = body.split('data-grp="banking"')[1].split("</details>")[0]
        reports = body.split('data-grp="reports"')[1].split("</details>")[0]
        self.assertIn("Trust remittance", banking)
        self.assertNotIn("Trust remittance", reports)

    def test_allocation_rules_in_funds_setup(self):
        body = self.c.get("/").content.decode()
        funds = body.split('data-grp="funds"')[1].split("</details>")[0]
        self.assertIn("Allocation rules", funds)

    def test_development_group_patterns_moved_off_the_sidebar(self):
        """It is reached from the Allocation rules page now — where its two
        siblings (the rules themselves, and Allocation & categories) also live.
        A sidebar entry for every configuration page a treasurer visits twice a
        year is how a sidebar stops being navigable."""
        body = self.c.get("/").content.decode()
        self.assertNotIn("Development-group patterns", body)
        rules = self.c.get("/rules/").content.decode()
        self.assertIn("/dev-patterns/", rules)
        self.assertIn("/allocation-settings/", rules)

    def test_all_moved_links_resolve(self):
        for path in ["/", "/reports/remittance-dashboard/".replace("-dashboard", "")
                      if False else "/"]:
            pass
        from django.urls import reverse
        for name in ["remittance_dashboard", "rule_list", "dev_patterns",
                     "member_list", "pledge_dashboard", "campaign_list",
                     "department_list", "transfer_list", "budget", "asset_list"]:
            r = self.c.get(reverse(name))
            self.assertIn(r.status_code, (200, 301, 302), name)

    def test_breadcrumbs_reflect_new_sections(self):
        b = self.c.get("/members/").content.decode()
        self.assertIn('<span class="bc-section">People</span>', b)
        b2 = self.c.get("/departments/").content.decode()
        self.assertIn("Funds &amp; setup", b2)
