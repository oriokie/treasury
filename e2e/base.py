"""Shared harness for the end-to-end business workflow suite.

WHY THIS SUITE EXISTS
---------------------
This application has, five separate times, shipped a feature whose every part
was implemented and individually tested and which a real user still could not
use: a public form that redirected to login (#121), an invitation that
dead-ended on itself (#122), a page that rendered only on an empty record
(#125), a screen linked from no menu (#126), comment markup rendered as visible
text (#130). The lesson was written down after the second one:

    A workflow assembled from individually-tested parts still needs one test
    that starts where the user starts.

Every suite in this package is that test. They differ from the app's other
tests in four deliberate ways:

* **They go through HTTP.** A test that calls the service layer proves the
  service works; it cannot prove a treasurer can reach it. These POST to the
  real URL as a logged-in user with a real role, so a missing decorator, an
  unwired route or a permission that turns the actor away is a failure here.
* **They start from nothing and build state through the app itself.** No
  fixture shortcut writes a row the workflow is supposed to create, because the
  step that creates it is exactly what is under test.
* **They assert on MONEY, not on status codes.** `assertEqual(r.status_code,
  302)` is satisfied by a form that rejected everything (see `submit` below).
  The assertions that matter here are balances, and whether two reports that
  must agree do.
* **They check the invariants at the end.** A workflow that produces the right
  fund balance while unbalancing the ledger has not worked.

WRITING ONE
-----------
Subclass `BusinessWorkflowTest`. Walk the process in numbered steps with a
comment per step saying what the human is doing. Use `submit()` for every
write. Finish with `assert_books_balance()`.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department

#: A fixed "today" so a workflow reads the same in January as in December, and
#: so a period-end assertion cannot start failing because the suite was run on
#: the 1st. Individual tests may use their own dates; this is the default the
#: helpers reach for.
TODAY = dt.date(2026, 7, 15)
PERIOD_START = dt.date(2026, 7, 1)
PERIOD_END = dt.date(2026, 7, 31)

#: Long enough for the app's own validators (minimum length 10).
PASSWORD = "workflow-test-password-1"


class WorkflowError(AssertionError):
    """Raised when a step of a business process did not do what it claimed."""


class BusinessWorkflowTest(TestCase):
    """A church, its officers, and its funds — the state every workflow starts
    from, and nothing more. Anything a workflow is meant to CREATE is created by
    the workflow, through the app.
    """

    #: Set False in a subclass that wants to build its own chart of accounts.
    ensure_chart_of_accounts = True

    def setUp(self):
        super().setUp()
        self.treasurer = self.make_user("wf_treasurer", roles.TREASURER)
        self.assistant = self.make_user("wf_assistant", roles.ASSISTANT)
        self.auditor = self.make_user("wf_auditor", roles.AUDITOR)

        # Two funds, one of each kind, because almost every figure in this
        # application is split by whether money is the church's to spend (LOCAL)
        # or is being held on behalf of the field (TRUST), and a workflow tested
        # against only one of them proves half the rule.
        self.local_fund = Department.objects.create(
            name="Church Building", slug="wf-building",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.DEVELOPMENT)
        self.trust_fund = Department.objects.create(
            name="Tithe", slug="wf-tithe",
            fund_type=Department.FundType.TRUST,
            category=Department.Category.TRUST)

        if self.ensure_chart_of_accounts:
            from ledger.services import posting
            posting.ensure_chart()

    # -- actors ---------------------------------------------------------------

    def make_user(self, username, role=None, **extra):
        user = User.objects.create_user(username, password=PASSWORD, **extra)
        if role:
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        return user

    def acting_as(self, user):
        """A signed-in client for `user`.

        Uses a real login rather than `force_login` so the app's own auth chain
        runs — the lock, two-factor and forced-password-change middlewares all
        sit between the login page and the first real request, and a workflow
        that only ever force_logins would never discover that one of them turns
        this actor away.
        """
        client = Client()
        signed_in = client.login(username=user.username, password=PASSWORD)
        if not signed_in:
            raise WorkflowError(
                f"{user.username} could not sign in. The workflow cannot start.")
        return client

    # -- the write helper, which is the point of this harness ------------------

    def submit(self, client, url_name, data=None, args=None, follow=True,
               allow_form_errors=False):
        """POST to a named URL and REFUSE to accept a silent no-op.

        This exists because the most common false green in a workflow test is a
        POST that returns 200 having changed nothing: the form rejected the
        input, Django re-rendered it with the errors attached, and the test —
        which only looked at the status code — recorded a success. The workflow
        then "passes" while doing nothing at all.

        So: a 5xx fails, and a response that came back carrying a bound form
        with errors fails and PRINTS THE ERRORS, which is almost always the
        whole diagnosis. Pass `allow_form_errors=True` for the tests whose
        subject IS the rejection.
        """
        url = reverse(url_name, args=args or [])
        response = client.post(url, data or {}, follow=follow)

        if response.status_code >= 500:
            raise WorkflowError(
                f"POST {url} ({url_name}) returned {response.status_code}.")

        if not allow_form_errors:
            for problem in self._form_errors(response):
                raise WorkflowError(
                    f"POST {url} ({url_name}) was REJECTED and changed nothing.\n"
                    f"  {problem}\n"
                    f"A workflow step that silently does nothing is the failure "
                    f"this suite exists to catch — not a passing status code.")
        return response

    def visit(self, client, url_name, args=None, query=""):
        """GET a page and require it to actually open.

        A workflow that ends at a page nobody can load has not finished. A
        redirect to the login page is the specific shape of #121 and is called
        out separately, because it reads like working security.
        """
        url = reverse(url_name, args=args or []) + query
        response = client.get(url, follow=True)
        if response.status_code >= 400:
            raise WorkflowError(
                f"GET {url} ({url_name}) returned {response.status_code}.")
        if any("login" in url_part for url_part, _ in response.redirect_chain):
            raise WorkflowError(
                f"GET {url} ({url_name}) was redirected to the login page while "
                f"signed in — the actor cannot reach this step.")
        return response

    @staticmethod
    def _form_errors(response):
        """Every bound-form error on a rendered response, as readable strings."""
        context = getattr(response, "context", None) or {}
        try:
            candidates = list(context.keys())
        except Exception:                      # a plain dict-less context
            return
        seen = set()
        for key in candidates:
            if "form" not in key:
                continue
            form = context.get(key)
            for bound in (form if isinstance(form, (list, tuple)) else [form]):
                errors = getattr(bound, "errors", None)
                if not errors:
                    continue
                text = str(errors)
                if text in seen:
                    continue
                seen.add(text)
                yield f"{key}: {text}"

    # -- money assertions ------------------------------------------------------

    def assert_books_balance(self, msg=""):
        """Assets = Liabilities + Funds, entity-wide.

        The one assertion every workflow ends with. A process can produce a
        perfectly plausible fund balance and still have posted a half journal;
        this is what notices.
        """
        from ledger.services import posting
        equation = posting.accounting_equation()
        if not equation["balanced"]:
            raise WorkflowError(
                f"The books do not balance{': ' + msg if msg else ''}.\n"
                f"  assets      {equation['assets']:,.2f}\n"
                f"  liabilities {equation['liabilities']:,.2f}\n"
                f"  funds       {equation['funds']:,.2f}\n"
                f"  difference  {equation['assets'] - equation['liabilities'] - equation['funds']:,.2f}")

    def assert_trial_balance_balances(self, start=None, end=None):
        """Total debits equal total credits over a period."""
        from ledger.services import posting
        _rows, totals = posting.trial_balance(start, end)
        debit, credit = totals["debit"], totals["credit"]
        if debit != credit:
            raise WorkflowError(
                f"The trial balance is out by {debit - credit:,.2f} "
                f"(debits {debit:,.2f}, credits {credit:,.2f}).")

    def assert_fund_balance(self, fund, expected, as_of=None):
        """A fund holds exactly `expected` as at a date."""
        from reports.services import balances
        as_of = as_of or PERIOD_END
        rows = balances.department_summary(None, as_of)
        actual = None
        for row in rows:
            if getattr(row.get("department", None), "id", None) == fund.id:
                actual = row.get("closing")
                break
        if actual is None:
            raise WorkflowError(
                f"{fund.name} does not appear in the fund summary as at "
                f"{as_of:%d %b %Y} at all.")
        if Decimal(actual) != Decimal(expected):
            raise WorkflowError(
                f"{fund.name} holds {actual:,.2f} as at {as_of:%d %b %Y}, "
                f"expected {Decimal(expected):,.2f}.")

    def assert_agree(self, description, **figures):
        """Two or more figures that must be equal by construction.

        Named rather than positional so a failure says WHICH source disagreed —
        the recurring fault in this codebase is a total assembled one way being
        asserted equal to the same total assembled another way, and the useful
        part of the failure is which of them moved.
        """
        # Compared as NUMBERS, not as their text. `str(Decimal(...))` keeps the
        # exponent, so Decimal("255000.00") and Decimal(255000) — the same money,
        # arriving from a DecimalField and from an untyped sum — read as a
        # disagreement and sent an author hunting a difference of nought.
        distinct = {Decimal(v).normalize() for v in figures.values()}
        if len(distinct) > 1:
            detail = "\n".join(f"  {name:<34} {Decimal(value):>14,.2f}"
                               for name, value in figures.items())
            raise WorkflowError(f"{description} — these must agree:\n{detail}")
