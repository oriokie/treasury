"""Every page must render against a database that has data in it.

Why this exists
---------------
Three bugs in recent releases were all the same shape: a page that renders
perfectly on an empty database and fails on a real one. The public application
form (#121), the portal invitation loop (#122), and the portal standing page
(#125) each passed a green test suite while being unusable in production.

The reason is structural. Almost every test in this project builds the minimum
fixture its assertion needs, so loops over empty querysets never execute their
bodies, optional relations are never null-but-present, and template expressions
inside `{% for %}` and `{% if %}` are never evaluated at all. An empty record
renders every page and exercises almost none of the logic on it.

So this test does the opposite: it seeds the demonstration data the application
ships with — members, funds, giving, expenses, payables, a benevolent scheme,
assets — and then simply asks for every page. It asserts nothing about content.
It only asserts that the page does not fall over, which is the assertion the
other suites were quietly failing to make.

The specific trap that motivated it, worth recording because it is not obvious:
**Django resolves filter arguments eagerly.** `{{ a|default:b }}` raises
`VariableDoesNotExist` when `b` cannot be resolved, even when `a` is present and
the default is never used. So `{{ x|default:row.approved_by.username }}` is a
live 500 for every row where `approved_by` is null — and null is the normal
state of an approval field. A plain `{{ row.approved_by.username }}` renders
blank and is fine. The difference is invisible on an empty table.
"""
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import get_resolver


def _no_argument_urls():
    """Every named URL that can be reached without arguments.

    Pages needing an object id are not covered here — they need a fixture that
    knows which object, which is a different test. The no-argument pages are the
    menus, registers and dashboards, which is where a treasurer spends the day.
    """
    urls = {}
    for key, value in get_resolver().reverse_dict.items():
        if not isinstance(key, str):
            continue
        try:
            possibilities = value[0]
            for pattern, params in possibilities:
                if not params:
                    urls[key] = "/" + pattern
                    break
        except Exception:
            continue
    return urls


#: Endpoints that legitimately do not answer a bare GET. Kept short and
#: justified — anything added here is a page this test no longer protects.
SKIP = {
    "logout",                    # ends the session the rest of the run needs
    "healthz",                   # not a page
    "vendor_lookup",             # JSON, needs a query
    "portal_unavailable",        # portal-member only by design
    "after_login",               # a redirect, covered by the portal tests
}

#: Prefixes skipped here, and where they ARE covered — because the gap is
#: otherwise invisible and this file would look like it covers more than it does.
#:
#: `portal_` pages need a signed-in portal MEMBER, not a treasurer: the
#: confinement middleware bounces an office login straight back out, so asking
#: for them here would assert nothing. They are covered by
#: `benevolent.test_portal_pages.PortalPagesWithRealDataTests`, which builds a
#: member with a policy, a dues schedule and a name-only dependant — and which
#: was confirmed to fail against the broken templates before being accepted.
#: That matters: the bug that prompted THIS file was a portal page, so without
#: that companion suite the class would still be uncovered where it first bit.
SKIP_PREFIXES = ("admin:", "portal_")


class SeededPageSmokeTests(TestCase):
    """Ask for every page, on a database with data in it."""

    @classmethod
    def setUpTestData(cls):
        # The demonstration data the application ships with. Using it rather
        # than a hand-built fixture is the point: it is the closest thing to a
        # real church's database that exists in the repository, and it is
        # maintained because the demo depends on it.
        call_command("seed_demo", verbosity=0)

    def setUp(self):
        self.client = Client()
        user = User.objects.get(username="treasurer")
        user.set_password("smoke-test-pass")
        user.save()
        self.client.get("/accounts/login/")
        self.client.post("/accounts/login/",
                         {"username": "treasurer", "password": "smoke-test-pass"},
                         follow=True)

    def test_every_page_renders_with_real_data(self):
        failures = []
        for name, path in sorted(_no_argument_urls().items()):
            if name in SKIP or name.startswith(SKIP_PREFIXES):
                continue
            try:
                response = self.client.get(path)
            except Exception as exc:
                failures.append(f"  {name} ({path}) raised "
                                f"{type(exc).__name__}: {exc}")
                continue
            if response.status_code >= 500:
                failures.append(f"  {name} ({path}) returned "
                                f"{response.status_code}")

        self.assertFalse(
            failures,
            "These pages fail on a database with data in them, though they "
            "render on an empty one. The commonest cause is a filter argument "
            "that cannot resolve — `{{ a|default:b.c.d }}` raises when `b.c` is "
            "null, even though the default is never used:\n" + "\n".join(failures))


#: Detail pages, and where to get a real id for each.
#:
#: These matter more than the no-argument pages, not less: the null-relation
#: hazard lives on rows, and a row is what a detail page renders. `closed_by`,
#: `approved_by`, `member`, `created_by` are all null in the ordinary case — an
#: unapproved expense, an open period, an anonymous gift — and a template that
#: dereferences one inside a filter argument fails on exactly those rows.
DETAIL_PAGES = [
    ("expense_detail", "cashbook.Expense"),
    ("asset_detail", "assets.FixedAsset"),
    ("benevolent_case_detail", "benevolent.BenevolentCase"),
    ("benevolent_membership_detail", "benevolent.SchemeMembership"),
    ("envelope_batch_detail", "envelopes.EnvelopeBatch"),
    ("member_detail", "members.Member"),
    ("vendor_detail", "vendors.Vendor"),
    ("fund_ledger", "departments.Department"),
    ("payable_edit", "cashbook.Payable"),
]


class SeededDetailPageSmokeTests(SeededPageSmokeTests):
    """The same idea, on pages that render one record."""

    def test_detail_pages_render_for_a_real_record(self):
        from django.apps import apps
        from django.urls import NoReverseMatch, reverse

        failures, checked = [], 0
        for name, label in DETAIL_PAGES:
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            instance = model.objects.first()
            if instance is None:
                continue
            try:
                url = reverse(name, args=[instance.pk])
            except NoReverseMatch:
                continue
            checked += 1
            try:
                response = self.client.get(url)
            except Exception as exc:
                failures.append(f"  {name} ({url}) raised "
                                f"{type(exc).__name__}: {exc}")
                continue
            if response.status_code >= 500:
                failures.append(f"  {name} ({url}) returned {response.status_code}")

        self.assertFalse(failures, "Detail pages failing on real records:\n"
                         + "\n".join(failures))
        self.assertGreater(checked, 3,
                           "Too few detail pages were reachable to call this a "
                           "check — the URL names or seed data have moved.")


class NullOptionalRelationSmokeTests(SeededPageSmokeTests):
    """The pages, with every optional relation actually null.

    The two tests above prove the pages render against seeded data — which is a
    real improvement on an empty database, and still not the check that matters.
    The seed populates `approved_by`, `closed_by`, `member` and the rest, so a
    template that dereferences one inside a filter argument still never gets
    asked the question that breaks it.

    Null is not an edge case for these fields. It is the ordinary state: an
    expense awaiting approval has no approver, an open period has no closer, an
    unmatched bank credit has no member. So this blanks them and asks again.
    """

    #: (model label, field) pairs that are nullable by design and commonly null.
    NULLABLE = [
        ("cashbook.Expense", "approved_by"),
        ("cashbook.Expense", "payee"),
        ("cashbook.Expense", "voucher_no"),
        ("cashbook.Payable", "due_date"),
        ("giving.Transaction", "member"),
        ("giving.Transaction", "reference"),
        ("assets.FixedAsset", "location"),
        ("members.Member", "phone"),
        ("benevolent.BenevolentCase", "dependant"),
    ]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from django.apps import apps
        for label, field in cls.NULLABLE:
            try:
                model = apps.get_model(label)
                model_field = model._meta.get_field(field)
            except Exception:
                continue
            # a blank CharField takes "", a nullable relation or date takes None
            blank = "" if getattr(model_field, "empty_strings_allowed", False) \
                and not model_field.null else None
            try:
                model.objects.update(**{field: blank})
            except Exception:
                # not nullable after all — leave it, that is the schema's answer
                pass

    def test_pages_render_when_optional_relations_are_null(self):
        failures = []
        for name, path in sorted(_no_argument_urls().items()):
            if name in SKIP or name.startswith(SKIP_PREFIXES):
                continue
            try:
                response = self.client.get(path)
            except Exception as exc:
                failures.append(f"  {name} ({path}) raised "
                                f"{type(exc).__name__}: {exc}")
                continue
            if response.status_code >= 500:
                failures.append(f"  {name} ({path}) returned {response.status_code}")

        self.assertFalse(
            failures,
            "These pages fail when an optional relation is null — the ordinary "
            "state for an unapproved expense, an open period or an unmatched "
            "gift. Look for `{{ a|default:b.c.d }}`: Django resolves the filter "
            "argument even when the default is unused, so a null `b.c` raises:\n"
            + "\n".join(failures))

    def test_detail_pages_render_when_optional_relations_are_null(self):
        from django.apps import apps
        from django.urls import NoReverseMatch, reverse

        failures = []
        for name, label in DETAIL_PAGES:
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            instance = model.objects.first()
            if instance is None:
                continue
            try:
                url = reverse(name, args=[instance.pk])
            except NoReverseMatch:
                continue
            try:
                response = self.client.get(url)
            except Exception as exc:
                failures.append(f"  {name} ({url}) raised "
                                f"{type(exc).__name__}: {exc}")
                continue
            if response.status_code >= 500:
                failures.append(f"  {name} ({url}) returned {response.status_code}")

        self.assertFalse(failures, "Detail pages failing with null relations:\n"
                         + "\n".join(failures))


class NoTemplateSyntaxLeaksIntoPagesTests(SeededPageSmokeTests):
    """No page shows the reader template markup that should have been processed.

    Added after five templates shipped with multi-line `{# ... #}` comments.
    Django's `{# #}` is a **single-line** construct: spanning lines does not
    comment anything out, the engine never recognises it as a comment, and the
    whole block renders as literal visible text — including in `base.html`,
    which is every page in the application.

    The smoke tests above did not catch it, and could not: a page full of leaked
    comment text returns a perfectly healthy HTTP 200. Status codes tell you the
    view worked; they say nothing about what the reader is looking at.

    So this looks at the output. It is a blunt check — any `{#`, `{%` or `{{`
    surviving into rendered HTML means something was not processed — but blunt
    is right here, because the failure it catches is one a developer never sees
    (they read the template, not the page) and a user cannot miss.
    """

    #: Places these sequences legitimately appear in output: JavaScript that
    #: builds markup, and documentation of the template language itself.
    ALLOWED_CONTEXT = ("${", "javascript:", "<script", "<code", "<pre")

    def _leaks(self, html):
        import re
        found = []
        for pattern in (r"\{#", r"\{%\s*\w", r"\{\{\s*\w"):
            for m in re.finditer(pattern, html):
                window = html[max(0, m.start() - 200):m.start()]
                # Anything inside a <script> or <code> block is somebody's
                # deliberate text, not a failure to render.
                if any(token in window.lower() for token in self.ALLOWED_CONTEXT):
                    continue
                found.append(html[m.start():m.start() + 60].replace("\n", " "))
        return found

    def test_no_detail_page_leaks_unrendered_template_markup(self):
        """Detail pages too. The leak that prompted all of this was in
        `base.html`, which every page extends — but a leak in a detail
        template would be just as visible and just as invisible to a status
        check."""
        from django.apps import apps
        from django.urls import NoReverseMatch, reverse

        failures = []
        for name, label in DETAIL_PAGES:
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            instance = model.objects.first()
            if instance is None:
                continue
            try:
                url = reverse(name, args=[instance.pk])
            except NoReverseMatch:
                continue
            try:
                response = self.client.get(url)
            except Exception:
                continue
            if response.status_code != 200:
                continue
            if "text/html" not in response.headers.get("Content-Type", ""):
                continue
            leaks = self._leaks(response.content.decode("utf-8", "ignore"))
            if leaks:
                failures.append(f"  {name} ({url}): {leaks[:2]}")

        self.assertFalse(failures, "Detail pages showing raw template markup:\n"
                         + "\n".join(failures))

    def test_no_page_leaks_unrendered_template_markup(self):
        failures = []
        for name, path in sorted(_no_argument_urls().items()):
            if name in SKIP or name.startswith(SKIP_PREFIXES):
                continue
            try:
                response = self.client.get(path)
            except Exception:
                continue
            if response.status_code != 200:
                continue
            # HTML only. A spreadsheet or a zip is compressed binary, and its
            # bytes will contain "{%" by chance often enough to make this check
            # useless if they are included — which the first run of this test
            # duly demonstrated.
            if "text/html" not in response.headers.get("Content-Type", ""):
                continue
            leaks = self._leaks(response.content.decode("utf-8", "ignore"))
            if leaks:
                failures.append(f"  {name} ({path}): {leaks[:2]}")

        self.assertFalse(
            failures,
            "These pages show the reader raw template markup. The usual cause "
            "is a `{# ... #}` comment spanning several lines — Django's is a "
            "single-line construct, so a multi-line one is not a comment at "
            "all and renders in full:\n" + "\n".join(failures))
