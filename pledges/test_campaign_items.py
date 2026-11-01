"""Itemised campaign needs — import, remaining stock, public pledge + codes."""
import datetime as dt
import io
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgeCampaignItem
from pledges.services.items import parse_items_file, remaining_allows
from pledges.services.codes import find_pledge_campaign_by_code, resolve_code_attribution
from pledges.services import reminders as rem


class ItemImportTests(TestCase):
    def test_parses_camporee_style_csv(self):
        raw = (
            "Item Description,Quantity,Unit Price (KES),Total Price (KES)\n"
            "Sugar,20,160.00,3200.00\n"
            "Rice,42,180.00,7560.00\n"
            "TOTAL (KES),,,155220\n"
        ).encode()
        f = io.BytesIO(raw)
        f.name = "budget.csv"
        rows, err = parse_items_file(f)
        self.assertEqual(err, "")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "Sugar")
        self.assertEqual(rows[0]["quantity_needed"], Decimal("20"))
        self.assertEqual(rows[0]["unit_price"], Decimal("160.00"))


class ItemPledgeFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("items_tr", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Camporee Fund", fund_type="LOCAL", category="MINISTRY")
        self.camp = PledgeCampaign.objects.create(
            name="PF 2026 Camporee", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE,
            items_public_enabled=True)
        self.camp.ensure_items_share_token()
        self.camp.ensure_match_code()
        self.sugar = PledgeCampaignItem.objects.create(
            campaign=self.camp, name="Sugar", unit="kg",
            quantity_needed=Decimal("20"), unit_price=Decimal("160"))
        cfg = SiteConfig.get()
        cfg.pledge_public_submit_mode = SiteConfig.PledgePublicSubmitMode.ACTIVE
        cfg.pledge_send_submit_thanks = False  # avoid SMS in tests
        cfg.save()
        self.client = Client()

    def test_public_page_opens(self):
        r = self.client.get(
            f"/pledge/items/{self.camp.items_share_token}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Sugar")
        self.assertContains(r, self.camp.name)

    def test_disabled_link_is_404(self):
        self.camp.items_public_enabled = False
        self.camp.save(update_fields=["items_public_enabled"])
        r = self.client.get(
            f"/pledge/items/{self.camp.items_share_token}/")
        self.assertEqual(r.status_code, 404)

    def test_pledge_records_quantity_and_amount(self):
        url = f"/pledge/items/{self.camp.items_share_token}/"
        self.client.get(url)
        s = self.client.session
        s["item_pledge_form_ts"] = 0
        s.save()
        r = self.client.post(
            url,
            {"name": "Jane Pathfinder", "phone": "0712345678",
             "item": self.sugar.pk, "quantity": "2", "website": ""},
            follow=True)
        self.assertEqual(r.status_code, 200)
        p = Pledge.objects.get(item=self.sugar)
        self.assertEqual(p.item_quantity, Decimal("2"))
        self.assertEqual(p.amount, Decimal("320.00"))
        self.assertEqual(p.status, Pledge.Status.ACTIVE)
        self.assertTrue(p.match_code.startswith("PG"))
        self.assertEqual(self.sugar.quantity_remaining, Decimal("18"))

    def test_cannot_oversubscribe(self):
        url = f"/pledge/items/{self.camp.items_share_token}/"
        self.client.get(url)
        s = self.client.session
        s["item_pledge_form_ts"] = 0
        s.save()
        r = self.client.post(
            url,
            {"name": "Too Much", "phone": "0711111111",
             "item": self.sugar.pk, "quantity": "50", "website": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Only")
        self.assertFalse(Pledge.objects.filter(item=self.sugar).exists())

    def test_remaining_counts_drafts(self):
        m = Member.objects.create(name="DRAFT PLEDGER", phone="254700000099")
        Pledge.objects.create(
            campaign=self.camp, member=m, amount=Decimal("1600"),
            item=self.sugar, item_quantity=Decimal("10"),
            status=Pledge.Status.DRAFT, start_date=dt.date.today())
        self.assertFalse(remaining_allows(self.sugar, Decimal("11")))
        self.assertTrue(remaining_allows(self.sugar, Decimal("10")))

    def test_campaign_pc_code_routes_to_fund(self):
        hit = resolve_code_attribution(f"GIFT {self.camp.match_code}")
        self.assertEqual(hit.get("source"), "pledge_campaign")
        self.assertEqual(hit.get("department"), self.fund)
        self.assertEqual(
            find_pledge_campaign_by_code(self.camp.match_code), self.camp)

    def test_thanks_sms_includes_code(self):
        m = Member.objects.create(name="SMS ITEM", phone="254700000088")
        p = Pledge.objects.create(
            campaign=self.camp, member=m, amount=Decimal("320"),
            item=self.sugar, item_quantity=Decimal("2"),
            status=Pledge.Status.ACTIVE, start_date=dt.date.today())
        text = rem.build_pledge_text(
            p, kind="THANKS", template=rem.DEFAULTS["THANKS"])
        self.assertIn(p.match_code, text)
        self.assertIn("Sugar", text)

    def test_treasurer_items_page(self):
        self.client.force_login(self.user)
        r = self.client.get(f"/pledges/campaigns/{self.camp.pk}/items/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Import spreadsheet")
        self.assertContains(r, "Sugar")
