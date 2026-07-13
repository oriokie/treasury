"""The envelope ledger's contributor-name autocomplete popup was reported
rendering "outside the grid" — appearing somewhere other than directly
below the input being typed into.

Root cause: the popup used `position:fixed`, positioned in JS via
`getBoundingClientRect()` (correctly viewport-relative math), but was left
as a DOM child of the table row it belonged to — which is, in turn, nested
inside `.content`, the page's main wrapper (base.html). `.content` runs a
`transform`-animating entrance animation on every page load
(`animation: rise .3s ease both`), and per the CSS spec, an element
animating `transform` establishes a new containing block for any
`position: fixed` descendant — so the popup's "fixed" coordinates were
being resolved against `.content`'s own box, not the true viewport,
landing it away from the input rather than under it.

Fixed by moving the popup to a direct child of `document.body` the first
time it is shown, which has no such ancestor to be trapped by — the
standard "portal" pattern for exactly this class of problem. These tests
confirm the template ships that fix and cleans up after itself, since a
genuine browser-rendering assertion isn't something this test environment
can make directly.
"""
from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER


class LedgerPopupPositioningTests(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tr_popupfix", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.treasurer)

    def _body(self):
        r = self.client.get("/envelopes/ledger/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_the_popup_is_moved_to_document_body_before_being_positioned(self):
        body = self._body()
        self.assertIn("document.body.appendChild(box)", body)
        # and this must happen BEFORE the fixed-position coordinates are
        # ever calculated, not after — otherwise the first show would still
        # be trapped by .content's containing block
        append_idx = body.index("document.body.appendChild(box)")
        position_idx = body.index('box.style.left = r.left')
        self.assertLess(append_idx, position_idx)

    def test_a_reference_to_the_moved_box_is_kept_for_cleanup(self):
        body = self._body()
        self.assertIn("tr._acBox = box", body)

    def test_removing_a_row_cleans_up_its_moved_popup(self):
        body = self._body()
        self.assertIn("if(tr._acBox) tr._acBox.remove()", body)

    def test_rebuilding_the_table_cleans_up_orphaned_popups_first(self):
        body = self._body()
        cleanup = 'document.querySelectorAll(".ac-box").forEach(b => b.remove())'
        rebuild = 'body.innerHTML = ""'
        self.assertIn(cleanup, body)
        self.assertIn(rebuild, body)
        self.assertLess(body.index(cleanup), body.index(rebuild))

    def test_the_content_wrapper_still_has_its_entrance_animation(self):
        """Confirms the diagnosis directly: .content really does animate
        transform, which is what necessitated the fix above rather than
        simply leaving the popup where it was."""
        css = open("static/css/app.css").read()
        self.assertIn("animation:rise", css.replace(" ", ""))
        self.assertIn("transform:translateY(6px)", css.replace(" ", ""))
