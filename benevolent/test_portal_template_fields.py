"""Member-facing portal templates asked the code for fields it does not have.

They all failed the quiet way. Django resolves a name it cannot find to the
empty string in a plain variable, and to false in an ``{% if %}`` — so a
template that asks for an attribute nobody ever wrote does not raise, does not
log, and does not show up in a status-code check. It just stops saying something
the member came to the page to read:

  * ``portal/case_detail.html`` dated each timeline row from ``e.at``. CaseEvent
    has ``on`` and ``created_at``; it has never had ``at``. Every line of every
    member's own case history — raised, submitted, assessed, approved, paid,
    closed — rendered with a blank date. The identical typo in the same view's
    queryset (``.order_by("at")``) hit the ORM, raised FieldError and was fixed
    the same day, which is the whole lesson: one side of the view was loud and
    the other was silent, so only one of the two got noticed.
  * ``portal/standing.html`` guarded its explanation on ``row.standing.reasons``.
    ``standing.assess()`` returns a StandingResult carrying ``reason`` (one
    sentence) and ``workings`` (the supporting lines). There is no ``reasons``.
    The block therefore never rendered for anybody in any standing: a member in
    arrears saw the red pill and the figure and was never told, in words, what
    it meant — while the office screen next door, reading ``reason``, had been
    printing exactly that sentence the whole time.
  * ``portal/case_detail.html`` labelled each payment with
    ``p.get_method_display``. BenevolentPayout has no ``method`` — the payment
    method is on the cashbook Expense the payout indexes, and a historical
    payout has no Expense at all — so the accessor does not exist, and every
    paid case has shown the member a date followed by a dangling " · " and
    nothing after it. This one is not from the same audit as the two above: it
    is older, it was found by re-reading the template rather than by anything
    failing, and that is the point. Once again the office's own case screen had
    been printing the right three facts (date, payee, amount) the whole time.

The tests below extend ``PortalPagesWithRealDataTests`` rather than the empty
fixture, and that is the point rather than a detail. None of these bugs is
reachable without real data: the timeline loop needs a case with events in it,
the payments panel is inside ``{% if payouts %}`` and so does not exist at all
until somebody has actually been paid, and a member with nothing owing has no
explanation to be denied. This is the lesson of #121/#122/#125/#130 arriving for
a fourth time — an empty record renders every page, satisfies every permission
check, and executes none of the loops where these faults live.
"""
import datetime as dt
import re
from dataclasses import fields as dataclass_fields
from decimal import Decimal
from pathlib import Path

from django.template.defaultfilters import date as date_filter
from django.template.loader import get_template
from django.urls import reverse
from django.utils.html import escape

from cashbook.models import Expense

from . import test_portal_pages
from .models import BenevolentCase, BenevolentPayout, CaseEvent, Standing
from .services import standing as standing_svc
from .services.standing import StandingResult

# Imported as a module, not `from ... import PortalPagesWithRealDataTests`: a
# TestCase bound as a name in this module is discovered here too, and the whole
# of that class would then be collected and run a second time under this file.


def _template_source(name):
    """The template as written, not as rendered.

    Several of the checks here are about what the template *asks for*, which is
    a question about its source. Reading it through the loader rather than by
    path means the test follows the same resolution the renderer does, so it
    cannot end up cheerfully inspecting a file Django would never load.
    """
    return Path(get_template(name).origin.name).read_text(encoding="utf-8")


_COMMENT_BLOCK = re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S)
_ANY_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def _attributes_asked_of(source, var):
    """Every attribute a template's source reads off ``var``.

    Comments come out first, and that is load-bearing rather than tidy. Each fix
    in this family documents itself by naming the dead attribute it replaced —
    ``e.at``, ``standing.reasons``, ``p.get_method_display`` — so a scan of the
    raw file would read the explanation as a fresh offence and fail on the very
    note that records why the guard exists.

    What is left is searched inside ``{{ }}`` and ``{% %}`` alike, because a
    name that does not resolve is wrong in each of them and wrong differently:
    blank in a variable, false in an ``{% if %}``, and — the one case that is
    not silent — a 500 when it is a filter *argument*, which Django resolves
    eagerly even when the default is never used.

    ``(?<![\\w.])`` is what makes the search safe rather than approximate. The
    first version of this guard anchored on ``{{\\s*e\\.`` to stay out of
    trouble; matching ``e.`` loosely instead would hit the ``e.`` inside
    ``case.event_type``, and the test would spend its life reporting fields that
    were never asked for. The lookbehind keeps that precision while letting the
    scan reach into tags and filter arguments, where the anchored form could not.
    """
    body = _COMMENT_BLOCK.sub("", source)
    wanted = re.compile(rf"(?<![\w.]){re.escape(var)}\.(\w+)")
    found = set()
    for tag in _ANY_TAG.findall(body):
        found.update(wanted.findall(tag))
    return found


class PortalTemplatesReadRealFieldsTests(
        test_portal_pages.PortalPagesWithRealDataTests):
    """The member's own case and the member's own standing, on real records."""

    def setUp(self):
        super().setUp()
        # A case that has actually been through the mill. The dates are spread
        # out and all distinct, because a timeline dated from the wrong field
        # would still pass if every event shared one date with the case itself.
        self.case = BenevolentCase.objects.create(
            scheme=self.scheme, event_type=self.bereavement,
            membership=self.membership, event_date=dt.date(2025, 11, 2),
            status=BenevolentCase.Status.PAID,
            approved_amount=Decimal("5000"), raised_by=self.treasurer)
        self.event_dates = {
            CaseEvent.Kind.RAISED: dt.date(2025, 11, 3),
            CaseEvent.Kind.SUBMITTED: dt.date(2025, 11, 7),
            CaseEvent.Kind.DOCUMENT_ADDED: dt.date(2025, 11, 12),
            CaseEvent.Kind.ASSESSED: dt.date(2025, 11, 19),
            CaseEvent.Kind.APPROVED: dt.date(2025, 12, 1),
            CaseEvent.Kind.PAYOUT_PAID: dt.date(2025, 12, 9),
            CaseEvent.Kind.CLOSED: dt.date(2026, 1, 15),
        }
        for kind, on in self.event_dates.items():
            CaseEvent.objects.create(
                case=self.case, kind=kind, on=on,
                summary=f"{CaseEvent.Kind(kind).label} — recorded")

        # And money actually paid on it, in all three shapes the panel can be
        # handed — which is why the fault lived so long. `{% if payouts %}`
        # means an unpaid case renders no panel at all, so every existing portal
        # test, right up to the real-data ones, walked straight past this block.
        #
        # 3,500 + 1,500 is the full 5,000 approved, so `refresh_status` (which
        # the expense signals fire on delete, below) leaves the case PAID rather
        # than quietly demoting the fixture to PARTLY_PAID under us.
        voucher = Expense.objects.create(
            date=dt.date(2025, 12, 10), department=self.fund,
            description=f"{self.scheme.name} benefit {self.case.number}",
            amount=Decimal("3500"), category=Expense.Category.BENEVOLENCE,
            method=Expense.Method.MPESA, status=Expense.Status.PAID,
            recorded_by=self.treasurer)
        self.live_payout = BenevolentPayout.objects.create(
            case=self.case, expense=voucher, payee_name="Ruth Momanyi",
            created_by=self.treasurer)
        # A benefit split with a third party, imported from the pre-system
        # records: no Expense at all, so it is the row that would be left
        # methodless by sourcing the method from `p.expense` instead.
        self.historical_payout = BenevolentPayout.objects.create(
            case=self.case, is_historical=True,
            historical_amount=Decimal("1500"),
            historical_date=dt.date(2026, 2, 3),
            payee_name="Kisii Funeral Home", created_by=self.treasurer)
        # A payout whose voucher was deleted. Built by actually deleting it,
        # not by writing expense=None, because that is the only way to prove the
        # state is reachable: the FK is SET_NULL and `benevolent.signals` says in
        # as many words that the payout is meant to survive its voucher. Its
        # `date` and `payee_name` are then both empty, which is the row that made
        # the date default necessary.
        doomed = Expense.objects.create(
            date=dt.date(2026, 3, 4), department=self.fund,
            description=f"{self.scheme.name} benefit {self.case.number} (void)",
            amount=Decimal("800"), category=Expense.Category.BENEVOLENCE,
            status=Expense.Status.PENDING, recorded_by=self.treasurer)
        self.orphaned_payout = BenevolentPayout.objects.create(
            case=self.case, expense=doomed, payee_name="",
            created_by=self.treasurer)
        doomed.delete()
        self.orphaned_payout.refresh_from_db()
        self.case.refresh_from_db()
        self.assertIsNone(self.orphaned_payout.expense,
                          "Deleting the voucher was supposed to leave the payout "
                          "orphaned; if it now cascades, this fixture is testing "
                          "a state that no longer exists.")

    def _case_body(self):
        response = self.client.get(
            reverse("portal_case_detail", args=[self.case.pk]))
        self.assertEqual(response.status_code, 200,
                         "The member could not open their own case.")
        return response.content.decode()

    def _standing_body(self):
        response = self.client.get(reverse("portal_standing"))
        self.assertEqual(response.status_code, 200,
                         "The standing page failed for a member in arrears.")
        return response.content.decode()

    def _assert_only_asks_for_real_fields(self, *, template, var, owner, real,
                                          if_none_found):
        """The one shape all three source guards share.

        Written once and parameterised rather than copied a third time, because
        the copying is what let the payout row survive: `e.at` and
        `standing.reasons` each got a bespoke check aimed at the name that had
        just been found, and the payments panel two loops further down the same
        file — asking a payout for a field of a different model entirely — was
        never in scope of either. A guard against a *class* of fault has to be
        cheap enough to point at the next loop the day it is written.
        """
        referenced = _attributes_asked_of(_template_source(template), var)
        self.assertTrue(referenced, if_none_found)
        for name in sorted(referenced):
            self.assertIn(
                name, real,
                f"{template} reads `{var}.{name}`, which {owner} does not have. "
                f"Django resolves that to nothing: it renders blank, logs "
                f"nothing and returns 200, so no other test in this repo can "
                f"see it.")

    # -- the timeline's dates ------------------------------------------------

    def test_every_line_of_the_case_timeline_carries_the_date_it_happened(self):
        """Dated from `e.at`, which does not exist, every row came out blank.

        Asserted per event rather than once, because the failure was total: the
        member could see that they had been paid and not when, seven times over.
        """
        body = self._case_body()
        for kind, on in self.event_dates.items():
            with self.subTest(kind=kind):
                self.assertIn(
                    date_filter(on, "d M Y"), body,
                    f"The timeline showed the {CaseEvent.Kind(kind).label} event "
                    f"with no date against it.")

    def test_no_timeline_row_renders_an_empty_date_cell(self):
        """The symptom itself, pinned. An empty `<span class="k">` is what a
        member actually saw, and it is what any future rename of `on` would put
        back — this catches that shape whatever name causes it."""
        self.assertNotIn(
            '<span class="k"></span>', self._case_body(),
            "A timeline row rendered its date cell empty, which is exactly how "
            "the missing-attribute fault presents.")

    def test_the_timeline_only_asks_caseevent_for_fields_it_has(self):
        """Guards the class of mistake, not this instance of it.

        `at` was wrong in a way no renderer would ever report, so the guard has
        to be the template's own text checked against the model. A field renamed
        in `models_case.py` now fails here instead of quietly blanking a column.
        """
        self._assert_only_asks_for_real_fields(
            template="benevolent/portal/case_detail.html", var="e",
            owner="CaseEvent",
            real={f.name for f in CaseEvent._meta.get_fields()} | set(dir(CaseEvent)),
            if_none_found="Found no `e.` references at all — has the timeline "
                          "loop been renamed? Then so must this test be.")

    # -- the standing explanation --------------------------------------------

    def test_a_member_in_arrears_is_told_in_words_why(self):
        """The pill said ARREARS; the sentence explaining it was never printed.

        `assess()` computes that sentence for exactly this purpose, and the
        office screen has always shown it. The portal asked for `reasons`, which
        is not a field, so the whole block was skipped in silence.
        """
        result = standing_svc.assess(self.membership, self.scheme.current_policy)
        self.assertEqual(
            result.standing, Standing.ARREARS,
            "This fixture no longer puts the member in arrears, so it no longer "
            "exercises the explanation. Fix the fixture, not the assertion.")
        self.assertIn(
            escape(result.reason), self._standing_body(),
            "The standing engine explained the member's position and the page "
            "did not pass it on.")

    def test_the_standing_panel_only_asks_standingresult_for_fields_it_has(self):
        """The same guard on the other page, for the same reason: a name that
        does not resolve makes a template `if` false rather than noisy, so
        nothing but a check on the source itself can catch it."""
        self._assert_only_asks_for_real_fields(
            template="benevolent/portal/standing.html", var="row.standing",
            owner="StandingResult",
            real=({f.name for f in dataclass_fields(StandingResult)}
                  | set(dir(StandingResult))),
            if_none_found="The standing panel no longer reads the assessment at "
                          "all, which is the bug this test exists to prevent.")

    # -- the payments panel ---------------------------------------------------

    def _payment_lines(self):
        """The left-hand cell of each row under "Payments made".

        Asserting on the extracted cells rather than on substrings of the whole
        page is the difference between checking that a date appears somewhere
        and checking that it appears *on the payment row* — and the timeline
        above is full of dates, so "somewhere" would pass on a panel that had
        gone completely blank.
        """
        body = self._case_body()
        self.assertIn("Payments made", body,
                      "The payments panel did not render for a case with three "
                      "payouts on it, so none of these assertions mean anything.")
        panel = body.split("Payments made", 1)[1]
        return re.findall(r'<span class="k">(.*?)</span>', panel, re.S)

    def test_every_payment_row_says_when_the_money_moved_and_who_got_it(self):
        """What the member came for, and what `get_method_display` displaced.

        The payee matters here beyond tidiness: a bereavement benefit is
        routinely split between the family and the funeral home, so "who was
        paid" is the difference between two rows a member can account for and
        two figures they cannot.
        """
        lines = self._payment_lines()
        self.assertEqual(len(lines), 3,
                         f"Expected a row per payout, got {lines!r}.")
        for payout, expected_payee in [(self.live_payout, "Ruth Momanyi"),
                                       (self.historical_payout,
                                        "Kisii Funeral Home")]:
            with self.subTest(payout=expected_payee):
                wanted = f"{date_filter(payout.date, 'd M Y')} · {expected_payee}"
                self.assertIn(
                    wanted, lines,
                    f"No payment row read {wanted!r}. The panel showed {lines!r}.")

    def test_no_payment_row_trails_off_after_the_separator(self):
        """The symptom exactly as a member met it: `09 Dec 2025 · ` and nothing.

        Pinned on the rendered shape rather than on the absence of the word
        "method", so that any future attribute that fails to resolve in this
        cell — not just this one — is caught by the same assertion.
        """
        for line in self._payment_lines():
            self.assertFalse(
                re.search(r"·\s*$", line.strip()) or not line.strip(),
                f"A payment row rendered as {line.strip()!r} — a date, a "
                f"separator and nothing after it, which is what an attribute "
                f"the model does not have looks like on the page.")

    def test_a_payout_whose_voucher_was_deleted_still_reads_as_a_payment(self):
        """The row that forced the date to be defaulted.

        `date` is a property, not a column, and returns None once the Expense
        behind it is gone — a state `benevolent.signals` deliberately supports.
        Undefaulted, the cell opened with a bare separator; the payee fell back
        to the beneficiary, which is the same reading `record_payout` encodes
        when a caller names no third party.
        """
        self.assertIsNone(self.orphaned_payout.date,
                          "The orphaned payout now has a date, so this test no "
                          "longer covers the case the default exists for.")
        # Escaped, because the beneficiary is whatever the church roll says and
        # an apostrophe in a name would otherwise turn this into a false alarm.
        self.assertIn(
            f"— · {escape(self.case.beneficiary_display)}", self._payment_lines(),
            "A payout with no voucher behind it rendered without a date or a "
            "payee. Both are defaulted precisely so this row still says "
            "something; if the separator has changed, change it here too.")

    def test_the_payments_panel_only_asks_benevolentpayout_for_fields_it_has(self):
        """The third instance, and the reason the guard was generalised.

        `p.get_method_display` looks exactly like a real Django accessor, which
        is why it survived review: it is real on cashbook.Expense, and a payout
        merely indexes one. Nothing but the template's text checked against
        BenevolentPayout can tell those two apart.
        """
        self._assert_only_asks_for_real_fields(
            template="benevolent/portal/case_detail.html", var="p",
            owner="BenevolentPayout",
            real=({f.name for f in BenevolentPayout._meta.get_fields()}
                  | set(dir(BenevolentPayout))),
            if_none_found="Found no `p.` references — has the payments panel "
                          "been renamed or removed? Then so must this test be.")
