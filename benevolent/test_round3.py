"""Round 3 — reported issues, each fixed and guarded.

The headline find: the shared member-search widget **had never displayed a
single suggestion to anybody**. `query()` resolved to the endpoint's JSON
envelope `{results: [...]}` and handed that whole object to `renderResults()`,
which immediately tested `results.length` — `undefined` on an object — and hid
the box and returned. The endpoint was fine, the CSS was fine, the request was
even being made; the answer was thrown away one line before it could be
rendered, on every keystroke, in every form that used it. That failure lives
entirely in the browser, so it is guarded by a jsdom test
(`tests/js/member_search.test.js`) rather than here. What IS guarded here is
everything on the Python side of the same features.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import YearEndClose
from core.roles import ASSISTANT, TREASURER
from departments.models import Department
from members.models import Member, MemberPhone

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentEventType, BenevolentScheme, SchemeDependant,
                               SchemeMembership, SchemePolicy)
from benevolent.services import cases as case_svc
from benevolent.services import contributions as contrib_svc
from benevolent.services import registry as reg_svc
from benevolent.services import schemes as scheme_svc

TODAY = dt.date.today()


class Round3Fixture(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("t_r3", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.clerk = User.objects.create_user("c_r3", password="x")
        self.clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        self.client.force_login(self.treasurer)

    def _scheme(self, code, **policy_kw):
        fund = Department.objects.create(
            name=f"R3 {code}", slug=f"r3-{code.lower()}",
            fund_type=Department.FundType.LOCAL)
        scheme = BenevolentScheme.objects.create(
            name=f"R3 {code}", code=code, fund=fund, created_by=self.treasurer)
        event = BenevolentEventType.objects.create(
            scheme=scheme, name="Bereavement", code="BER")
        kw = dict(
            scheme=scheme, effective_from=TODAY - dt.timedelta(days=500),
            membership_required=True, waiting_period_days=0,
            contribution_mode=SchemePolicy.ContributionMode.FIXED_PERIODIC,
            contribution_amount=Decimal("100"),
            benefit_mode=SchemePolicy.BenefitMode.FIXED, benefit_amount=Decimal("10000"),
            arrears_treatment=SchemePolicy.ArrearsTreatment.IGNORE,
            created_by=self.treasurer)
        kw.update(policy_kw)
        policy = SchemePolicy.objects.create(**kw)
        scheme_svc.publish_policy(policy, user=self.treasurer)
        scheme_svc.activate_scheme(scheme, user=self.treasurer)
        return scheme, event, policy


# ===========================================================================
# 1. Alternate phone numbers are searchable everywhere
# ===========================================================================

class AlternatePhoneSearchTests(Round3Fixture):
    """MemberPhone has always recorded a member's other numbers, and the
    bank-statement matcher has always searched them. The SEARCH SCREENS did
    not — so a treasurer typing the very number that appears in the narration
    in front of them was told the member did not exist, and would be pushed
    into creating a duplicate for someone the system already knew."""

    def setUp(self):
        super().setUp()
        self.mary = Member.objects.create(name="Mary Altphone", phone="254700200001")
        MemberPhone.objects.create(member=self.mary, number="254799555444", label="M-Pesa")

    def test_the_shared_typeahead_finds_a_member_by_an_alternate_number(self):
        r = self.client.get(reverse("member_search"), {"q": "254799555444"})
        names = [x["name"] for x in r.json()["results"]]
        self.assertIn("MARY ALTPHONE", names)

    def test_the_benevolent_typeahead_finds_a_member_by_an_alternate_number(self):
        r = self.client.get(reverse("benevolent_member_search"), {"q": "254799555444"})
        names = [x["name"] for x in r.json()["results"]]
        self.assertIn("MARY ALTPHONE", names)

    def test_the_members_list_finds_a_member_by_an_alternate_number(self):
        r = self.client.get(reverse("member_list"), {"q": "254799555444"})
        self.assertContains(r, "MARY ALTPHONE")

    def test_a_member_is_not_listed_twice_when_several_of_their_numbers_match(self):
        MemberPhone.objects.create(member=self.mary, number="254799555999")
        r = self.client.get(reverse("benevolent_member_search"), {"q": "2547995"})
        names = [x["name"] for x in r.json()["results"]]
        self.assertEqual(names.count("MARY ALTPHONE"), 1)


# ===========================================================================
# 2. The search warns about a candidate who is already covered
# ===========================================================================

class AlreadyCoveredWarningTests(Round3Fixture):

    def test_an_already_enrolled_candidate_is_flagged(self):
        scheme, _e, _p = self._scheme("R3A")
        m = Member.objects.create(name="Already Enrolled R3", phone="254700200002")
        reg_svc.register(scheme, m, joined_on=TODAY, user=self.treasurer)
        r = self.client.get(reverse("benevolent_member_search"),
                            {"q": "Already Enrolled", "scheme": scheme.pk})
        row = r.json()["results"][0]
        self.assertTrue(row["blocked"])
        self.assertIn("Already enrolled here", row["warning"])

    def test_a_candidate_who_is_already_someone_elses_spouse_is_flagged(self):
        scheme, _e, _p = self._scheme("R3B")
        head = Member.objects.create(name="R3 Head", phone="254700200003")
        spouse = Member.objects.create(name="R3 Spouse", phone="254700200004")
        hh = reg_svc.register(scheme, head, joined_on=TODAY, user=self.treasurer,
                              registration_type="HOUSEHOLD")
        reg_svc.add_dependant(hh, relationship=SchemeDependant.Relationship.SPOUSE,
                              member=spouse, user=self.treasurer)
        r = self.client.get(reverse("benevolent_member_search"),
                            {"q": "R3 Spouse", "scheme": scheme.pk})
        row = r.json()["results"][0]
        self.assertIn("spouse of", row["warning"])
        self.assertIn("R3 HEAD", row["warning"])

    def test_registering_someone_already_a_spouse_here_is_REFUSED_not_just_warned(self):
        """The UI warning is a courtesy. The real protection is server-side:
        one person must not end up with two memberships in one scheme —
        counted twice on the roll, levied twice, able to claim twice."""
        scheme, _e, _p = self._scheme("R3C")
        head = Member.objects.create(name="R3C Head", phone="254700200005")
        spouse = Member.objects.create(name="R3C Spouse", phone="254700200006")
        hh = reg_svc.register(scheme, head, joined_on=TODAY, user=self.treasurer,
                              registration_type="HOUSEHOLD")
        reg_svc.add_dependant(hh, relationship=SchemeDependant.Relationship.SPOUSE,
                              member=spouse, user=self.treasurer)
        before = SchemeMembership.objects.filter(scheme=scheme).count()
        r = self.client.post(
            reverse("benevolent_register", args=[scheme.pk]),
            {"member": spouse.pk, "registration_type": "INDIVIDUAL",
             "joined_on": TODAY.isoformat(), "notes": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already covered under this scheme")
        self.assertEqual(SchemeMembership.objects.filter(scheme=scheme).count(), before)

    def test_a_clean_candidate_carries_no_warning(self):
        scheme, _e, _p = self._scheme("R3D")
        Member.objects.create(name="R3D Nobody", phone="254700200007")
        r = self.client.get(reverse("benevolent_member_search"),
                            {"q": "R3D Nobody", "scheme": scheme.pk})
        row = r.json()["results"][0]
        self.assertEqual(row["warning"], "")
        self.assertFalse(row["blocked"])


# ===========================================================================
# 3. A contribution can be attributed to a case (Edwin's question 11)
# ===========================================================================

class ContributionCaseAttributionTests(Round3Fixture):
    """How does the system know a contribution belongs to a case? Three ways:
    the case's own levy screen (which sets it explicitly), the bank-statement
    allocator (which reads the case number out of the narration), and — now —
    the general contribution form.

    That third one was a real gap. A levy recorded through the general form
    could not name a case at all, so `record_contribution()` saw `case=None`,
    inferred the kind as VOLUNTARY rather than LEVY, and filed it against
    nothing. The member stayed 'unpaid' on the case's levy roster, and under a
    POOLED policy — where the benefit IS whatever the levy collected — the
    payout itself came out short.
    """

    def setUp(self):
        super().setUp()
        self.scheme, self.event, self.policy = self._scheme(
            "R3E", contribution_mode=SchemePolicy.ContributionMode.PER_CASE_LEVY,
            levy_amount=Decimal("500"),
            benefit_mode=SchemePolicy.BenefitMode.POOLED)
        self.payer = reg_svc.register(
            self.scheme, Member.objects.create(name="R3 Payer", phone="254700200010"),
            joined_on=TODAY - dt.timedelta(days=200), user=self.treasurer)
        self.bereaved = reg_svc.register(
            self.scheme, Member.objects.create(name="R3 Bereaved", phone="254700200011"),
            joined_on=TODAY - dt.timedelta(days=200), user=self.treasurer)
        self.case = case_svc.create_case(
            self.scheme, event_type=self.event, membership=self.bereaved,
            event_date=TODAY, user=self.clerk)

    def test_the_contribution_form_offers_a_case(self):
        r = self.client.get(reverse("benevolent_contribute", args=[self.scheme.pk]))
        self.assertContains(r, "id_case")

    def test_a_levy_recorded_on_the_general_form_lands_on_the_cases_roster(self):
        r = self.client.post(
            reverse("benevolent_contribute", args=[self.scheme.pk]),
            {"membership": self.payer.pk, "case": self.case.pk,
             "date": TODAY.isoformat(), "amount": "500", "channel": "CASH",
             "period_label": "", "note": ""})
        self.assertEqual(r.status_code, 302)

        c = BenevolentContribution.objects.get(membership=self.payer)
        self.assertEqual(c.case_id, self.case.pk)
        # money attached to a case IS a levy — the service infers this, and it
        # matters: a levy must never settle the payer's own subscription
        self.assertEqual(c.kind, BenevolentContribution.Kind.LEVY)
        self.assertEqual(contrib_svc.levy_collected(self.case), Decimal("500"))

        summary = contrib_svc.levy_summary(self.case)
        paid = {r["membership"].pk for r in summary["rows"] if r["paid"] > 0}
        self.assertIn(self.payer.pk, paid)

    def test_without_a_case_the_same_payment_is_NOT_a_levy(self):
        """Confirms the bug this fixes was real: leaving the case blank still
        records the money, but as a voluntary gift attached to nothing — which
        is exactly what used to happen to EVERY levy entered on this form,
        because there was no way to say otherwise."""
        self.client.post(
            reverse("benevolent_contribute", args=[self.scheme.pk]),
            {"membership": self.payer.pk, "case": "",
             "date": TODAY.isoformat(), "amount": "500", "channel": "CASH",
             "period_label": "", "note": ""})
        c = BenevolentContribution.objects.get(membership=self.payer)
        self.assertIsNone(c.case_id)
        self.assertNotEqual(c.kind, BenevolentContribution.Kind.LEVY)
        self.assertEqual(contrib_svc.levy_collected(self.case), Decimal(0))

    def test_a_non_member_cannot_be_levied_for_a_case(self):
        stranger = Member.objects.create(name="R3 Stranger", phone="254700200012")
        r = self.client.post(
            reverse("benevolent_contribute", args=[self.scheme.pk]),
            {"member": stranger.pk, "case": self.case.pk,
             "date": TODAY.isoformat(), "amount": "500", "channel": "CASH",
             "period_label": "", "note": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "A levy is paid by an enrolled member")

    def test_a_draft_case_IS_offered(self):
        """A church starts the harambee the moment a death is known — long
        before the paperwork catches up. Money given then is still a levy for
        that case, and must be attributable."""
        from benevolent.forms import ContributionForm
        self.assertEqual(self.case.status, BenevolentCase.Status.DRAFT)
        form = ContributionForm(scheme=self.scheme)
        self.assertIn(self.case, form.fields["case"].queryset)

    def test_a_rejected_case_is_NOT_offered(self):
        case_svc.submit_case(self.case, user=self.clerk)
        case_svc.assess_case(self.case, user=self.treasurer)
        case_svc.reject_case(self.case, reason="Not covered.", user=self.treasurer)
        from benevolent.forms import ContributionForm
        form = ContributionForm(scheme=self.scheme)
        self.assertNotIn(self.case, form.fields["case"].queryset)


# ===========================================================================
# 4. The founding balance is a one-time figure, and is frozen once a year closes
# ===========================================================================

class FoundingBalanceTests(Round3Fixture):
    """`Department.opening_balance` is the FOUNDING brought-forward figure —
    what a fund held on the day the church started using this system. It is
    NOT year-scoped: every later year's opening is DERIVED from it (founding +
    all movement before that year), and year-end close never writes it.

    The budget page let a treasurer edit it while calling it "opening balance
    for <year>". Changing it in July did not set July's opening — it silently
    rewrote every fund balance in every year the church had ever recorded,
    backwards. That is the distortion reported.
    """

    def setUp(self):
        super().setUp()
        self.dept = Department.objects.create(
            name="R3 Budget Fund", slug="r3-budget-fund",
            fund_type=Department.FundType.LOCAL, opening_balance=Decimal("1000"))

    def test_before_any_year_close_it_is_editable_but_warned(self):
        r = self.client.get(reverse("budget"))
        self.assertContains(r, f'name="opening_{self.dept.pk}"')
        self.assertContains(r, "one-time figure, not a yearly one")

    def test_before_any_year_close_a_change_is_accepted(self):
        self.client.post(reverse("budget"),
                         {"year": TODAY.year, f"opening_{self.dept.pk}": "2500.00"})
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.opening_balance, Decimal("2500.00"))

    def test_once_a_year_is_closed_the_input_is_gone(self):
        YearEndClose.objects.create(year=TODAY.year - 1, closed_by=self.treasurer,
                                    total_carried=Decimal(0))
        r = self.client.get(reverse("budget"))
        self.assertNotContains(r, f'name="opening_{self.dept.pk}"')
        self.assertContains(r, "Founding balances are locked")

    def test_once_a_year_is_closed_a_posted_change_is_REFUSED(self):
        """The real protection: hiding the input is not enough — a crafted or
        stale POST must not be able to rewrite an audited history either."""
        YearEndClose.objects.create(year=TODAY.year - 1, closed_by=self.treasurer,
                                    total_carried=Decimal(0))
        self.client.post(reverse("budget"),
                         {"year": TODAY.year, f"opening_{self.dept.pk}": "999999.00"})
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.opening_balance, Decimal("1000"),
                         "a closed year's founding balance was overwritten")

    def test_the_page_shows_each_funds_DERIVED_opening_for_the_year(self):
        """What a treasurer actually came for: this year's opening, which is
        calculated, not typed."""
        r = self.client.get(reverse("budget"))
        self.assertContains(r, "derived")
        row = next(x for x in r.context["rows"] if x["d"].pk == self.dept.pk)
        self.assertIn("opening_derived", row)


# ===========================================================================
# 5. Date filters / defaults
# ===========================================================================

class DateFilterTests(Round3Fixture):

    def test_the_transfers_page_defaults_to_this_month_and_offers_filters(self):
        r = self.client.get(reverse("transfer_list"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["date_default_applied"])
        self.assertContains(r, 'name="fund"')
        self.assertContains(r, "this month")

    def test_the_transfers_page_honours_an_explicit_all_time(self):
        r = self.client.get(reverse("transfer_list"), {"start": "", "end": ""})
        self.assertFalse(r.context["date_default_applied"])

    def test_the_member_page_offers_a_date_filter(self):
        m = Member.objects.create(name="R3 Filter Member", phone="254700200020")
        r = self.client.get(reverse("member_detail", args=[m.pk]))
        self.assertContains(r, 'name="start"')

    def test_the_member_pages_lifetime_total_ignores_the_filter(self):
        """Narrowing the window must never make a member look like they have
        given less than they have — which is exactly why this page does NOT
        default to a month the way the unbounded LIST pages do."""
        from giving.models import Transaction
        m = Member.objects.create(name="R3 Lifetime", phone="254700200021")
        d = Department.objects.create(name="R3 Gifts", slug="r3-gifts",
                                      fund_type=Department.FundType.LOCAL)
        Transaction.objects.create(
            date=dt.date(2020, 5, 1), channel="CASH", direction="CREDIT",
            amount=Decimal("700"), department=d, member=m, confirmed=True,
            allocation_status="MANUAL")
        r = self.client.get(reverse("member_detail", args=[m.pk]))
        # default window is THIS YEAR, so the 2020 gift is outside it…
        self.assertEqual(r.context["period_given"], Decimal(0))
        # …but the lifetime total still shows it
        self.assertEqual(r.context["total_given"], Decimal("700"))
