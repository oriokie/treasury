"""Who contributed to a case, who did not, and who was never asked.

Two reports and two rules, all turning on one distinction: being levied and not
paying is a debt; never having been levied is not. Confusing them either invents
arrears for people who owe nothing or loses the arrears of people who do.

The rules:

* **Somebody who joins while a case is running is not levied for it.** They were
  not in the scheme when the family's need arose. The standing engine already
  believed this — `missed_case_levies` has always counted only cases from a
  member's own cover date — but the levy roster did not, so a member who joined
  last week was put on the list for a case from last year and chased for it,
  while the same system declined to record it as a miss when they did not pay.

* **"Months since the last contribution" only means something if money was
  asked for.** A per-case levy scheme collects when somebody is bereaved. A
  quiet year with no cases would otherwise turn every faithful member inactive
  on the same day, for not paying a levy nobody raised.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department
from members.models import Member

from .models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                     SchemeDependant, SchemeMembership, SchemePolicy)
from .services import contributions as contrib_svc
from .services import registry as reg_svc
from .services import schemes as scheme_svc


class LevySchemeFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tess-case", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Benevolent Fund", slug="ben-case",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.scheme = BenevolentScheme.objects.create(
            name="Case Scheme", code="CSE", fund=self.fund,
            created_by=self.user, status=BenevolentScheme.Status.ACTIVE)
        self.event_type = BenevolentEventType.objects.create(
            scheme=self.scheme, name="Bereavement", code="BRV",
            covers_dependants=True)
        policy = SchemePolicy.objects.create(
            scheme=self.scheme,
            effective_from=dt.date.today() - dt.timedelta(days=800),
            contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"), inactivity_months=12,
            inactivity_missed_cases=3)
        scheme_svc.publish_policy(policy, user=self.user)
        self.policy = policy

        self.long_standing = self._member("Early Joiner", "254700000001",
                                          days_ago=400)
        self.payer = self._member("Faithful Payer", "254700000002", days_ago=400)
        self.event_date = dt.date.today() - dt.timedelta(days=30)
        self.case = BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.event_type,
            membership=self.long_standing, event_date=self.event_date,
            status=BenevolentCase.Status.APPROVED)
        self.client = Client()
        self.client.force_login(self.user)

    def _member(self, name, phone, days_ago):
        person = Member.objects.create(name=name, phone=phone)
        m = reg_svc.register(self.scheme, person,
                             joined_on=dt.date.today() - dt.timedelta(days=days_ago))
        if m.status == SchemeMembership.Status.PENDING:
            m = reg_svc.admit(m, user=self.user)
        m.refresh_from_db()
        return m


class JoiningDuringACaseTests(LevySchemeFixture):

    def test_a_member_who_joined_before_the_event_is_levied(self):
        roster = contrib_svc.raise_case_levy(self.case)
        levied = {r["membership"].pk for r in roster["rows"]}
        self.assertIn(self.payer.pk, levied)

    def test_a_member_who_joined_after_the_event_is_not_levied(self):
        late = self._member("Late Joiner", "254700000003", days_ago=0)
        roster = contrib_svc.raise_case_levy(self.case)
        levied = {r["membership"].pk for r in roster["rows"]}
        self.assertNotIn(
            late.pk, levied,
            "A member who joined after the event was put on the levy roster and "
            "would be chased for a death that happened before they joined.")

    def test_they_are_reported_as_not_yet_covered_rather_than_dropped(self):
        """Silence would look like an omission; the reason belongs on the list."""
        late = self._member("Late Joiner", "254700000003", days_ago=0)
        roster = contrib_svc.raise_case_levy(self.case)
        self.assertIn(late.pk, {m.pk for m in roster["not_yet_covered"]})

    def test_the_expected_total_excludes_them(self):
        before = contrib_svc.raise_case_levy(self.case)["expected"]
        self._member("Late Joiner", "254700000003", days_ago=0)
        after = contrib_svc.raise_case_levy(self.case)["expected"]
        self.assertEqual(
            before, after,
            "Adding a member who joined after the event changed what the case "
            "was expected to collect.")

    def test_the_roster_and_the_standing_engine_agree(self):
        """The inconsistency that made this a bug rather than a preference."""
        from .services.standing import missed_case_levies
        late = self._member("Late Joiner", "254700000003", days_ago=0)
        roster = contrib_svc.raise_case_levy(self.case)
        self.assertNotIn(late.pk, {r["membership"].pk for r in roster["rows"]})
        self.assertEqual(missed_case_levies(late, self.policy, dt.date.today()), 0)


class IdleMonthsNeedSomethingToHaveBeenAskedForTests(LevySchemeFixture):

    def test_a_quiet_year_does_not_make_a_member_inactive(self):
        """No cases means no levies means nothing not to have paid."""
        from .services.standing import facts_for
        BenevolentCase.objects.all().delete()
        facts = facts_for(self.payer, as_of=dt.date.today())
        self.assertFalse(
            facts.had_something_to_pay,
            "The scheme raised no levy, so there was nothing this member could "
            "have contributed to.")

    def test_a_year_with_a_case_still_counts(self):
        from .services.standing import facts_for
        facts = facts_for(self.payer, as_of=dt.date.today())
        self.assertTrue(facts.had_something_to_pay)

    def test_a_members_own_case_does_not_count_as_something_they_owed(self):
        from .services.standing import facts_for
        facts = facts_for(self.long_standing, as_of=dt.date.today())
        self.assertFalse(
            facts.had_something_to_pay,
            "The only case was this member's own bereavement; they were never "
            "levied for it.")

    def test_a_dues_scheme_is_unaffected(self):
        """Dues are owed whether or not anybody has died."""
        from .services.standing import _had_something_to_pay
        self.policy.contribution_mode = SchemePolicy.ContributionMode.FIXED_PERIODIC
        self.assertTrue(
            _had_something_to_pay(self.payer, self.policy, dt.date.today()))


class CaseContributionStatusReportTests(LevySchemeFixture):

    def setUp(self):
        super().setUp()
        contrib_svc.record_contribution(
            self.scheme, membership=self.payer, amount=Decimal("500"),
            date=dt.date.today(), kind="LEVY", case=self.case, user=self.user)

    def _csv(self, key, **params):
        import csv
        import io
        params.setdefault("scheme", "CSE")
        params["export"] = "csv"
        response = self.client.get(reverse("engine_report", args=[key]), params)
        self.assertEqual(response.status_code, 200)
        return list(csv.reader(io.StringIO(response.content.decode())))

    def _rows(self):
        rows = self._csv("benevolent_case_contribution_status",
                         case=self.case.number)
        header = next(r for r in rows if r and r[0] == "Member")
        start = rows.index(header) + 1
        return header, [r for r in rows[start:] if r and r[0]]

    def test_a_contributor_is_marked_as_having_contributed(self):
        header, rows = self._rows()
        row = next(r for r in rows if r[0] == self.payer.member.name)
        self.assertEqual(row[header.index("Status")], "Contributed")
        self.assertEqual(row[header.index("Contributed")], "500")

    def test_a_non_contributor_is_marked_and_shows_the_debt(self):
        other = self._member("Owes Money", "254700000004", days_ago=400)
        header, rows = self._rows()
        row = next(r for r in rows if r[0] == other.member.name)
        self.assertEqual(row[header.index("Status")], "Not contributed")
        self.assertEqual(row[header.index("Outstanding")], "500.00")

    def test_a_late_joiner_is_shown_as_not_levied_with_the_reason(self):
        """Not as a defaulter — they owe nothing."""
        late = self._member("Late Joiner", "254700000005", days_ago=0)
        header, rows = self._rows()
        row = next(r for r in rows if r[0] == late.member.name)
        self.assertIn("Not levied", row[header.index("Status")])
        self.assertIn("joined after", row[header.index("Status")])

    def test_the_spouse_is_named(self):
        SchemeDependant.objects.create(
            membership=self.payer, name="Grace Payer",
            relationship=SchemeDependant.Relationship.SPOUSE, active=True)
        header, rows = self._rows()
        row = next(r for r in rows if r[0] == self.payer.member.name)
        self.assertEqual(row[header.index("Spouse")], "Grace Payer")

    def test_the_report_renders_on_screen(self):
        response = self.client.get(
            reverse("engine_report", args=["benevolent_case_contribution_status"]),
            {"scheme": "CSE", "case": self.case.number})
        self.assertEqual(response.status_code, 200)

    def test_the_case_page_links_to_it_for_this_case(self):
        """A report a treasurer has to go and find is one they work around.

        The question "who has paid towards this" is asked while looking at the
        case, so the link belongs there and must carry the case with it.
        """
        body = self.client.get(
            reverse("benevolent_case_detail", args=[self.case.pk])).content.decode()
        self.assertIn("benevolent_case_contribution_status", body)
        self.assertIn(self.case.number, body)

    def test_both_reports_are_listed_in_the_report_library(self):
        """Otherwise they exist only for somebody who knows the URL."""
        body = self.client.get(reverse("report_library")).content.decode()
        self.assertIn("Who Has Contributed to a Case", body)
        self.assertIn("Member Contributions Across Cases", body)


class MemberCaseMatrixReportTests(LevySchemeFixture):

    def setUp(self):
        super().setUp()
        contrib_svc.record_contribution(
            self.scheme, membership=self.payer, amount=Decimal("500"),
            date=dt.date.today(), kind="LEVY", case=self.case, user=self.user)
        self.defaulter = self._member("Owes Money", "254700000004", days_ago=400)
        self.late = self._member("Late Joiner", "254700000005", days_ago=0)

    def _matrix(self):
        import csv
        import io
        response = self.client.get(
            reverse("engine_report", args=["benevolent_member_case_matrix"]),
            {"scheme": "CSE", "export": "csv",
             "start": (dt.date.today() - dt.timedelta(days=90)).isoformat(),
             "end": dt.date.today().isoformat()})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        header = next(r for r in rows if r and r[0] == "Member")
        start = rows.index(header) + 1
        return header, [r for r in rows[start:] if r and r[0]]

    def _cell(self, member_name):
        header, rows = self._matrix()
        column = next(i for i, h in enumerate(header) if self.case.number in h)
        row = next(r for r in rows if r[0] == member_name)
        return row[column]

    def test_a_contribution_shows_the_amount(self):
        self.assertEqual(self._cell(self.payer.member.name), "500")

    def test_being_levied_and_unpaid_shows_zero(self):
        """A debt, and it must be visible as one."""
        self.assertEqual(self._cell(self.defaulter.member.name), "0")

    def test_never_being_levied_shows_blank(self):
        """Not a nought — they owe nothing, and a nought would say they do."""
        self.assertEqual(self._cell(self.late.member.name), "")

    def test_a_members_own_case_is_blank_for_them(self):
        self.assertEqual(self._cell(self.long_standing.member.name), "")

    def test_the_columns_are_the_cases_in_the_period(self):
        header, _ = self._matrix()
        self.assertTrue(any(self.case.number in h for h in header))

    def test_the_member_details_are_carried(self):
        header, _ = self._matrix()
        for column in ("Member", "Membership no.", "Spouse", "Total given"):
            self.assertIn(column, header)

    def test_the_report_renders_on_screen(self):
        response = self.client.get(
            reverse("engine_report", args=["benevolent_member_case_matrix"]),
            {"scheme": "CSE",
             "start": (dt.date.today() - dt.timedelta(days=90)).isoformat(),
             "end": dt.date.today().isoformat()})
        self.assertEqual(response.status_code, 200)
