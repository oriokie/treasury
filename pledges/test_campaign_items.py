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
        self.assertContains(r, "Record an offline pledge")

    def test_pg_code_does_not_stamp_dev_group_on_ministry_fund(self):
        from departments.models import DevelopmentGroup
        from giving.models import Transaction
        from pledges.services.attribution import apply_code_to_import

        g = DevelopmentGroup.objects.create(number=9, name="G9 Camp")
        m = Member.objects.create(name="PATHFINDER", phone="254700000077",
                                  active=True, dev_group=g)
        p = Pledge.objects.create(
            campaign=self.camp, member=m, amount=Decimal("320"),
            item=self.sugar, item_quantity=Decimal("2"),
            status=Pledge.Status.ACTIVE, start_date=dt.date.today())
        member, dept, dg, camp, cgrp, status, via = apply_code_to_import(
            reference=f"PAY {p.match_code}",
            member=None, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(dept, self.fund)
        self.assertIsNone(dg)
        self.assertEqual(member, m)

    def test_item_name_in_narration_routes_to_campaign_fund(self):
        hit = resolve_code_attribution("CHARCOAL FOR CAMP")
        # No charcoal item yet — create one
        charcoal = PledgeCampaignItem.objects.create(
            campaign=self.camp, name="Charcoal", unit="",
            quantity_needed=Decimal("1"), unit_price=Decimal("1500"))
        hit = resolve_code_attribution("CHARCOAL FOR CAMP")
        self.assertEqual(hit.get("source"), "pledge_item")
        self.assertEqual(hit.get("department"), self.fund)
        self.assertEqual(hit.get("campaign_item"), charcoal)
        self.assertIsNone(hit.get("dev_group"))

    def test_pc_code_plus_phone_fills_multiple_item_pledges(self):
        from giving.models import Transaction
        from pledges.services import matching as match_svc

        m = Member.objects.create(name="MULTI PLEDGER", phone="254711122233",
                                  active=True)
        rice = PledgeCampaignItem.objects.create(
            campaign=self.camp, name="Rice", unit="kg",
            quantity_needed=Decimal("42"), unit_price=Decimal("180"))
        p1 = Pledge.objects.create(
            campaign=self.camp, member=m, amount=Decimal("320"),
            item=self.sugar, item_quantity=Decimal("2"),
            status=Pledge.Status.ACTIVE, start_date=dt.date(2026, 1, 1),
            submitted_contact="MULTI / 254711122233")
        p2 = Pledge.objects.create(
            campaign=self.camp, member=m, amount=Decimal("360"),
            item=rice, item_quantity=Decimal("2"),
            status=Pledge.Status.ACTIVE, start_date=dt.date(2026, 1, 1),
            submitted_contact="MULTI / 254711122233")
        cfg = SiteConfig.get()
        cfg.pledge_match_mode = SiteConfig.PledgeMatchMode.AUTO
        cfg.pledge_match_same_fund_only = True
        cfg.save()
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 15), channel="BANK", direction="CREDIT",
            amount=Decimal("700"), department=self.fund, member=m,
            payer_name="MULTI PLEDGER", payer_phone="254711122233",
            reference=f"GIFT {self.camp.match_code}",
            confirmed=True, allocation_status="AUTO")
        pledges = match_svc.active_pledges_for_contribution(gift, cfg=cfg)
        self.assertEqual(len(pledges), 2)
        match_svc.handle_new_contribution(gift, user=self.user, cfg=cfg)
        p1.refresh_from_db(); p2.refresh_from_db()
        self.assertEqual(p1.paid + p2.paid, Decimal("700"))

    def test_offline_pledge_reserves_quantity(self):
        self.client.force_login(self.user)
        r = self.client.post(
            f"/pledges/campaigns/{self.camp.pk}/items/",
            {"action": "record_offline", "item_id": self.sugar.pk,
             "quantity": "3", "name": "Desk Visitor",
             "phone": "0712345670", "member": ""})
        self.assertEqual(r.status_code, 302)
        p = Pledge.objects.get(item=self.sugar)
        self.assertEqual(p.item_quantity, Decimal("3"))
        self.assertEqual(p.status, Pledge.Status.ACTIVE)
        self.assertEqual(self.sugar.quantity_remaining, Decimal("17"))

    def test_offline_pledge_with_register_member(self):
        m = Member.objects.create(name="DESK MEMBER", phone="254700000066",
                                  active=True)
        self.client.force_login(self.user)
        r = self.client.post(
            f"/pledges/campaigns/{self.camp.pk}/items/",
            {"action": "record_offline", "item_id": self.sugar.pk,
             "quantity": "1", "member": m.pk, "name": "", "phone": ""})
        self.assertEqual(r.status_code, 302)
        p = Pledge.objects.get(item=self.sugar)
        self.assertEqual(p.member_id, m.pk)
        self.assertEqual(p.amount, Decimal("160.00"))

    def test_set_memorable_shared_code(self):
        self.client.force_login(self.user)
        r = self.client.post(
            f"/pledges/campaigns/{self.camp.pk}/items/",
            {"action": "set_match_code", "match_code": "camporee-26"})
        self.assertEqual(r.status_code, 302)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.match_code, "CAMPOREE26")
        hit = resolve_code_attribution("GIFT CAMPOREE26")
        self.assertEqual(hit.get("source"), "pledge_campaign")
        self.assertEqual(hit.get("department"), self.fund)
        self.assertEqual(
            find_pledge_campaign_by_code("pay camporee26"), self.camp)

    def test_shared_code_clash_rejected(self):
        other = PledgeCampaign.objects.create(
            name="Other Appeal", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE, match_code="TAKENCODE")
        self.client.force_login(self.user)
        r = self.client.post(
            f"/pledges/campaigns/{self.camp.pk}/items/",
            {"action": "set_match_code", "match_code": "takencode"},
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already used")
        self.camp.refresh_from_db()
        self.assertNotEqual(self.camp.match_code, "TAKENCODE")
        self.assertEqual(other.match_code, "TAKENCODE")

    def test_items_page_shows_shared_code_panel(self):
        self.client.force_login(self.user)
        r = self.client.get(f"/pledges/campaigns/{self.camp.pk}/items/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Shared payment code")
        self.assertContains(r, self.camp.match_code)
        self.assertContains(r, "Start typing a member")
