"""Layout guards.

A template that closes more `<div>`s than it opens closes the page containers
early, so everything after it renders OUTSIDE the main column — which, because
the sidebar is sticky on `top` only, shows up as content spilling under the
sidebar once the page scrolls sideways. Two templates had exactly that fault
(the asset profile's disposal form had lost its card wrapper but kept its two
closing tags; the envelope import page had one stray close in its upload
branch), and it is invisible until someone opens the page and scrolls.

These tests are deliberately crude — counting tags, not parsing HTML — because
the fault they catch is crude and the cost of missing it is a broken page.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

OPEN = re.compile(r"<div\b")
CLOSE = re.compile(r"</div>")
# tables with this many columns or more must be able to scroll inside their card
WIDE = 4


def _template_dir():
    for d in settings.TEMPLATES[0]["DIRS"]:
        p = Path(d)
        if p.exists():
            return p
    return Path(settings.BASE_DIR) / "templates"


class DivBalanceTests(TestCase):
    def test_every_template_closes_the_divs_it_opens(self):
        offenders = {}
        for path in sorted(_template_dir().rglob("*.html")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            opened, closed = len(OPEN.findall(text)), len(CLOSE.findall(text))
            if opened != closed:
                offenders[str(path.relative_to(_template_dir()))] = opened - closed
        self.assertEqual(offenders, {},
                         "templates whose <div> tags do not balance (a surplus of "
                         "closing tags breaks out of the page layout): %s" % offenders)

    def test_no_template_closes_a_div_it_never_opened(self):
        """Balanced totals are not enough — a stray close followed by a stray
        open would net to zero while still breaking the page."""
        offenders = []
        for path in sorted(_template_dir().rglob("*.html")):
            depth = 0
            for n, line in enumerate(path.read_text(encoding="utf-8",
                                                    errors="ignore").splitlines(), 1):
                depth += len(OPEN.findall(line)) - len(CLOSE.findall(line))
                if depth < 0:
                    offenders.append(f"{path.relative_to(_template_dir())}:{n}")
                    break
        # templates that legitimately open a wrapper in one {% if %} branch and
        # close it in another are the only acceptable exceptions; there are none
        # today, so the list must stay empty.
        self.assertEqual(offenders, [],
                         "templates that close a <div> before opening one: %s" % offenders)


class WideTableContainmentTests(TestCase):
    """A table wider than its card must scroll inside the card. Without that it
    widens the whole document, and the sticky sidebar does not move with it."""

    def _wide_tables_outside_a_wrapper(self, path):
        text = path.read_text(encoding="utf-8", errors="ignore")
        bad = []
        for m in re.finditer(r"<table[^>]*>", text):
            head = text[m.end():m.end() + 900]
            columns = len(re.findall(r"<th\b", head.split("</tr>")[0])) if "</tr>" in head else 0
            if columns < WIDE:
                continue
            before = text[:m.start()]
            # the nearest enclosing element must be a scroll wrapper
            opened = before.rfind('class="table-wrap"')
            closed = before.rfind("</div>")
            if opened == -1 or closed > opened:
                bad.append(m.group(0)[:60])
        return bad

    def test_wide_asset_tables_can_scroll(self):
        base = _template_dir() / "assets"
        offenders = {}
        for path in sorted(base.rglob("*.html")):
            bad = self._wide_tables_outside_a_wrapper(path)
            if bad:
                offenders[path.name] = bad
        self.assertEqual(offenders, {},
                         "wide tables not inside .table-wrap, so they widen the page "
                         "instead of scrolling: %s" % offenders)
