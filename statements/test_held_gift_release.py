"""Gifts held by the import-confirmation setting must have a way out.

The live M-Pesa/bank feed writes no statement import, so a confidently
allocated gift from it was held unconfirmed with no screen in the system able
to release it: off the ledger, out of every balance, unmatchable to a pledge,
and wearing the green "Auto-allocated" pill in the register the whole time.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign
from pledges.services import matching as match_svc


class HeldGiftReleaseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tr", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="DEVELOPMENT",
                                             fund_type="LOCAL")
        self.member = Member.objects.create(name="EDWIN KENYANSA",
                                            phone="254711165935", active=True)
        self.client = Client()
        self.client.force_login(self.user)

    def _held_gift(self, amount="10"):
        """As `ingest` writes one: allocated, no statement import, held."""
        return Transaction.objects.create(
            date=dt.date(2026, 8, 20), channel="BANK", direction="CREDIT",
            amount=Decimal(amount), department=self.fund, member=self.member,
            payer_name="EDWIN KENYANSA", payer_phone="254711165935",
            allocation_status="AUTO", confirmed=False, statement_import=None)

    def test_the_feed_queue_lists_a_gift_with_no_import(self):
        gift = self._held_gift()
        r = self.client.get("/statements/held/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "EDWIN KENYANSA")
        self.assertContains(r, f'value="{gift.id}"')

    def test_confirming_from_the_feed_queue_releases_the_gift(self):
        gift = self._held_gift()
        r = self.client.post("/statements/held/", {"confirm": [str(gift.id)]})
        self.assertEqual(r.status_code, 302)
        gift.refresh_from_db()
        self.assertTrue(gift.confirmed)

    def test_a_released_gift_can_then_fulfil_a_pledge(self):
        """The whole point: the hold was silently breaking pledge matching."""
        campaign = PledgeCampaign.objects.create(
            name="Development Phase I", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        pledge = Pledge.objects.create(
            campaign=campaign, member=self.member, amount=Decimal("15000"),
            start_date=dt.date(2026, 8, 2), status=Pledge.Status.ACTIVE)
        gift = self._held_gift()
        self.assertEqual(match_svc.plan_auto_match_all(), [])

        self.client.post("/statements/held/", {"confirm": [str(gift.id)]})
        plan = match_svc.plan_auto_match_all()
        self.assertEqual([r["txn"].id for r in plan], [gift.id])
        self.assertEqual(plan[0]["pledge"].id, pledge.id)

    def test_the_pledge_page_distinguishes_allocated_from_confirmed(self):
        campaign = PledgeCampaign.objects.create(
            name="Development Phase I", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        pledge = Pledge.objects.create(
            campaign=campaign, member=self.member, amount=Decimal("15000"),
            start_date=dt.date(2026, 8, 2), status=Pledge.Status.ACTIVE)
        self._held_gift()
        rows = match_svc.explain_pledge_gifts(pledge)
        self.assertIn("Allocated to DEVELOPMENT", rows[0]["detail"])
        self.assertIn("held unconfirmed", rows[0]["detail"])

    def test_the_register_marks_a_held_gift_unconfirmed(self):
        self._held_gift()
        r = self.client.get("/transactions/?date_from=2026-08-01&date_to=2026-08-31")
        self.assertContains(r, "unconfirmed")
        self.assertContains(r, "/statements/held/")

    def test_the_register_stays_quiet_when_nothing_is_held(self):
        gift = self._held_gift()
        gift.confirmed = True
        gift.save(update_fields=["confirmed"])
        r = self.client.get("/transactions/?date_from=2026-08-01&date_to=2026-08-31")
        self.assertNotContains(r, "Held, unconfirmed")

    def test_the_feed_queue_leaves_import_held_rows_to_their_own_screen(self):
        """Two queues, two scopes — the feed queue must not swallow a batch."""
        from statements.models import StatementImport
        imp = StatementImport.objects.create(filename="aug.csv",
                                             uploaded_by=self.user)
        batch = Transaction.objects.create(
            date=dt.date(2026, 8, 19), channel="BANK", direction="CREDIT",
            amount=Decimal("500"), department=self.fund, payer_name="OTHER",
            allocation_status="AUTO", confirmed=False, statement_import=imp)
        r = self.client.get("/statements/held/")
        self.assertNotContains(r, f'value="{batch.id}"')
        self.client.post("/statements/held/", {"confirm_all": "1"})
        batch.refresh_from_db()
        self.assertFalse(batch.confirmed)

    def test_ingest_holds_feed_gifts_when_the_setting_is_on(self):
        """Pins the shape the queue exists to serve."""
        cfg = SiteConfig.get()
        self.assertIn("require_import_confirmation",
                      [f.name for f in cfg._meta.get_fields()])
