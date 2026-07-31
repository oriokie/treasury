"""Two UX fixes to the manual envelope-entry ledger page and the batch detail
page:

1. The "Returned for correction" / "Rejected" reason banners used a
   `.callout`/`.callout-warn`/`.callout-danger` class combo that (on the
   ledger page, and previously on the batch detail page) only had a LOCAL,
   low-contrast style rather than the shared `.alert`/`.alert-amber`/
   `.alert-danger` classes used everywhere else in the app — easy to miss
   against the page's own cream background. Switched to the shared classes.

2. Autosave silently swallowed the server's actual reason when a Sabbath is
   closed or the period is locked (a permanent block, not a network hiccup),
   showing only a generic "Couldn't save — will retry" that then retried
   every 15s forever. The ledger page now renders a `#blockedBanner`
   placeholder the JS fills with the server's real message.

3. The contributor-name typeahead could leave its suggestion popup stranded
   on screen. Showing it is deferred twice — a 170ms debounce, then a network
   round trip — so if the cashier typed a name and moved straight on to the
   amount (or the next row's name) without picking a suggestion, the box
   opened AFTER blur's own hide had already run, and nothing fires to hide it
   again until that same field is focused and left once more. It then floated
   over the following rows, obscuring them. Fixed with a stale-result guard.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT
from envelopes.models import EnvelopeBatch
from envelopes.services import batches as bsvc

SAB = dt.date(2026, 6, 6)


def _assistant(username="uxfix_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


class ReturnedReasonBannerStylingTests(TestCase):
    """The reason a batch was bounced back must use the app's shared,
    visually distinct alert styling, not the low-contrast local `.callout`
    classes."""

    def setUp(self):
        self.u = _assistant()
        self.client.force_login(self.u)
        self.batch, _ = bsvc.get_or_create_draft(self.u, None, SAB)
        self.batch.status = EnvelopeBatch.Status.RETURNED
        self.batch.return_reason = "Wrong fund on row 1"
        self.batch.save()

    def test_ledger_page_uses_shared_alert_class(self):
        r = self.client.get(reverse("envelope_ledger_edit", args=[self.batch.pk]))
        self.assertContains(r, 'class="alert alert-amber')
        self.assertContains(r, "Wrong fund on row 1")
        self.assertNotContains(r, 'class="callout')

    def test_batch_detail_page_uses_shared_alert_class(self):
        r = self.client.get(reverse("envelope_batch_detail", args=[self.batch.pk]))
        self.assertContains(r, 'class="alert alert-amber')
        self.assertContains(r, "Wrong fund on row 1")
        self.assertNotContains(r, 'class="callout')

    def test_rejected_batch_detail_uses_shared_danger_class(self):
        self.batch.status = EnvelopeBatch.Status.REJECTED
        self.batch.reject_reason = "Duplicate of batch #4"
        self.batch.save()
        r = self.client.get(reverse("envelope_batch_detail", args=[self.batch.pk]))
        self.assertContains(r, 'class="alert alert-danger')
        self.assertContains(r, "Duplicate of batch #4")


class LedgerBlockedBannerMarkupTests(TestCase):
    """The ledger page always renders the blocked-save banner placeholder
    (hidden by default) that the autosave JS fills in with the server's
    actual reason on a 409 — see saveNow() in ledger.html."""

    def setUp(self):
        self.u = _assistant("uxfix_asst2")
        self.client.force_login(self.u)

    def test_blocked_banner_placeholder_present_and_hidden_by_default(self):
        r = self.client.get(reverse("envelope_ledger"))
        self.assertContains(r, 'id="blockedBanner"')
        self.assertContains(r, 'id="blockedBannerText"')
        self.assertContains(r, 'id="blockedRetryBtn"')
        content = r.content.decode()
        banner_start = content.index('id="blockedBanner"')
        # the banner tag itself carries display:none nearby (hidden by default)
        self.assertIn("display:none", content[banner_start:banner_start + 150])


class TypeaheadStaleResultGuardTests(TestCase):
    """The typeahead's stale-result guard is inline JS, so its BEHAVIOUR can
    only be exercised in a browser (it was: the pre-fix code leaves the popup
    at display:block after focus moves to the amount cell, the fixed code
    leaves it hidden). What this test protects is that the guard is still
    WIRED IN — the two conditions are cheap to delete by accident during an
    unrelated edit to this block, and losing either one silently brings the
    stranded-popup bug straight back with nothing else to catch it.
    """

    def setUp(self):
        self.u = _assistant("uxfix_asst3")
        self.client.force_login(self.u)

    def test_late_result_is_dropped_when_a_newer_lookup_or_a_blur_beat_it(self):
        content = self.client.get(reverse("envelope_ledger")).content.decode()
        # superseded-by-a-newer-keystroke, and field-no-longer-focused
        self.assertIn("mySeq !== reqSeq", content)
        self.assertIn("document.activeElement !== nameI", content)

    def test_blur_cancels_a_lookup_still_in_flight(self):
        content = self.client.get(reverse("envelope_ledger")).content.decode()
        # blur must cancel immediately; only the visual hide is delayed so a
        # click on a suggestion still lands
        self.assertIn("cancelLookup(); setTimeout(hideBox, 150)", content)
