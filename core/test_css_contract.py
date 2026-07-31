"""A class the templates rely on must be a class the stylesheet defines.

This suite exists because the member portal was built almost entirely out of two
class names — `.panel` and `.table` — that had no definition anywhere. Twenty-
seven templates asked for `.panel` and eleven for `.table`, and every one of them
rendered as a bare `<div>` and an unstyled HTML table: no surface, no border, no
radius, no header treatment, no zebra striping, no numeric alignment. The same
fault, in smaller doses, left `.compact` inert on ninety-two tables and the KPI
figures on the payables, petty cash and budget board screens set as plain body
text inside otherwise styled cards.

None of it raised anything. A missing CSS class is silent by construction: the
page loads, the markup is valid, and the only symptom is that a screen quietly
looks unfinished. That is precisely why it needs a test — it is invisible to
every check that asks whether a page rendered.

**Scope, deliberately narrow.** Single-use class names are frequently legitimate
JavaScript hooks or semantic markers that were never meant to carry style, and
failing on those would make this suite noise. What cannot be innocent is a name
used across *several* templates with no definition behind it: that is a shared
component vocabulary with a hole in it. So the rule is applied at
`SHARED_THRESHOLD` templates and above.

**A ratchet, not a clean bill of health.** `KNOWN_UNDEFINED` records the classes
that were already in this state when the rule was introduced and are outside the
work that introduced it. They are logged in `docs/recommendations.md` rather than
silently tolerated. The list may shrink and must never grow: a new name here
fails the build, and a fixed one has to be struck off, so the debt cannot be
quietly reissued.
"""
import os
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR)
TEMPLATE_ROOT = BASE / "templates"
STYLESHEET = BASE / "static" / "css" / "app.css"

# A class must be defined once it is used in this many templates or more.
SHARED_THRESHOLD = 3

# Pre-existing gaps, outside the scope of the change that added this rule.
# Tracked in docs/recommendations.md (#117). Shrink only.
KNOWN_UNDEFINED = {
    "btn-link", "btn-primary", "field-label", "form-check",
    "head-actions", "ph-sub", "report-table", "u-sm",
}

CLASS_IN_CSS = re.compile(r"\.([A-Za-z][\w-]*)")
# Only literal class attributes. Anything containing template syntax is skipped:
# the rendered value is not knowable here, and guessing produces false failures.
CLASS_ATTR = re.compile(r'class="([^"{}]*)"')
STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
EXTENDS_OR_INCLUDE = re.compile(r"{%\s*(?:extends|include)\s+[\"']([^\"']+)[\"']")
CLASS_TOKEN = re.compile(r"[A-Za-z][\w-]*\Z")


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@lru_cache(maxsize=1)
def _stylesheet_classes():
    return frozenset(CLASS_IN_CSS.findall(_read(STYLESHEET)))


@lru_cache(maxsize=None)
def _styles_reachable_from(path):
    """Classes styled by a template, plus everything it extends or includes.

    Inheritance has to be followed or the result is nonsense: the portal's
    `pt-*` classes live in `portal/_base.html` and are used by every page that
    extends it, and the sign-in styles live in a partial that the auth pages
    include.
    """
    src = _read(Path(path))
    found = set()
    for block in STYLE_BLOCK.findall(src):
        found |= set(CLASS_IN_CSS.findall(block))
    for name in EXTENDS_OR_INCLUDE.findall(src):
        child = TEMPLATE_ROOT / name
        if child.exists() and str(child) != str(path):
            found |= _styles_reachable_from(str(child))
    return frozenset(found)


def _templates():
    for root, _dirs, files in os.walk(TEMPLATE_ROOT):
        for name in files:
            if name.endswith(".html"):
                yield Path(root) / name


def undefined_classes():
    """{class name: {templates that use it without any definition}}."""
    css = _stylesheet_classes()
    out = {}
    for path in _templates():
        local = _styles_reachable_from(str(path))
        for attr in CLASS_ATTR.findall(_read(path)):
            for token in attr.split():
                if not CLASS_TOKEN.match(token):
                    continue
                if token in css or token in local:
                    continue
                out.setdefault(token, set()).add(
                    str(path.relative_to(BASE)))
    return out


class SharedComponentClassesAreDefinedTests(SimpleTestCase):

    def test_no_shared_class_is_left_undefined(self):
        undefined = undefined_classes()
        offenders = {
            name: files for name, files in undefined.items()
            if len(files) >= SHARED_THRESHOLD and name not in KNOWN_UNDEFINED
        }
        if offenders:
            lines = []
            for name in sorted(offenders):
                files = sorted(offenders[name])
                lines.append(f"  .{name} — {len(files)} templates, "
                             f"e.g. {', '.join(files[:3])}")
            self.fail(
                "These classes are used across several templates but nothing "
                "defines them, so every one of those screens renders unstyled "
                "and nothing complains:\n" + "\n".join(lines)
                + "\n\nDefine them in static/css/app.css, or use the existing "
                  "class that already does the job.")

    def test_the_known_list_has_not_grown(self):
        """The ratchet. A fixed class must be struck off, not left listed."""
        undefined = undefined_classes()
        stale = sorted(
            name for name in KNOWN_UNDEFINED
            if len(undefined.get(name, ())) < SHARED_THRESHOLD)
        self.assertFalse(
            stale,
            "These are recorded as known-undefined but are no longer in that "
            f"state: {', '.join(stale)}. Remove them from KNOWN_UNDEFINED so "
            "the list keeps meaning something.")


class ComponentsIntroducedForThePortalStayDefinedTests(SimpleTestCase):
    """The specific classes this change defined, pinned by name.

    The threshold rule above would catch their removal, but only as one entry in
    a list. These name them, so a deletion says which component went and which
    screens it takes with it.
    """

    CASES = [
        ("panel", "the member portal, supplier register and benevolent screens"),
        ("panel-brass", "accent rail on notice panels"),
        ("panel-amber", "accent rail on action-needed panels"),
        ("entry-grid", "the add-a-row forms under the payables registers"),
        ("field-xs", "inline settle-a-payable amount inputs"),
    ]

    def test_each_component_is_defined(self):
        css = _stylesheet_classes()
        for name, used_by in self.CASES:
            with self.subTest(component=name):
                self.assertIn(
                    name, css,
                    f".{name} is no longer defined in app.css; it styles {used_by}.")

    def test_the_table_alias_shares_the_ledger_definition(self):
        """`.table` and `.ledger` are one definition under two names.

        Written as `:is(.ledger,.table)` on purpose. A second, parallel block for
        `.table` would drift from the ledger's the first time either is touched,
        which is the failure this whole suite is about.
        """
        css = _read(STYLESHEET)
        self.assertIn(
            "table:is(.ledger,.table){", css,
            "The .table/.ledger unification is gone. If .table has been given "
            "its own rules, the two will drift; keep one definition.")
        self.assertNotIn(
            "table.ledger{", css,
            "A .ledger-only base rule has reappeared, so .table no longer "
            "inherits the table styling the portal depends on.")
