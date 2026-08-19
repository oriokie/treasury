import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from members.models import Member
from giving.models import Transaction
from pledges.models import PledgeCampaign, Pledge, PledgePayment
from pledges.services import matching as match_svc


def _treasurer():
    u = User.objects.create_user("t_pl", password="x")
    g, _ = Group.objects.get_or_create(name="Treasurer")
    u.groups.add(g)
    return u


def _assistant():
    u = User.objects.create_user("a_pl", password="x")
    g, _ = Group.objects.get_or_create(name="Assistant")
    u.groups.add(g)
    return u


class PledgeAccountingBoundaryTests(TestCase):
    """The cardinal rule: a pledge never changes a fund balance. Only a real,
    matched contribution does."""

    def setUp(self):
        self.u = _treasurer()
        self.dept = Department.objects.create(name="Building", fund_type="LOCAL",
                                              category="DEVELOPMENT")
        self.member = Member.objects.create(name="Giver One", phone="254700111222")
        self.camp = PledgeCampaign.objects.create(name="Build", target_department=self.dept)

    def _fund_closing(self):
        from reports.services import balances
        rows = balances.department_summary(None, None, consolidated=False)
        return next((r["closing"] for r in rows if r["department"].id == self.dept.id),
                    Decimal("0"))

    def test_pledge_does_not_change_fund_balance(self):
        before = self._fund_closing()
        Pledge.objects.create(campaign=self.camp, member=self.member,
                              amount=Decimal("50000"), status="ACTIVE")
        self.assertEqual(self._fund_closing(), before)  # promise alone moves nothing

    def test_only_real_contribution_moves_fund(self):
        before = self._fund_closing()
        p = Pledge.objects.create(campaign=self.camp, member=self.member,
                                  amount=Decimal("50000"), status="ACTIVE",
                                  start_date=dt.date(2026, 1, 1))
        Transaction.objects.create(date=dt.date(2026, 6, 20), channel="CASH",
            direction="CREDIT", amount=Decimal("20000"), department=self.dept,
            member=self.member, allocation_status="MANUAL", confirmed=True)
        match_svc.auto_match_pledge(p, user=self.u)
        # fund moved by exactly the real gift, not the pledge
        self.assertEqual(self._fund_closing() - before, Decimal("20000"))
        p.refresh_from_db()
        self.assertEqual(p.paid, Decimal("20000"))
        self.assertEqual(p.outstanding, Decimal("30000"))


class PledgeMatchingTests(TestCase):
    def setUp(self):
        self.u = _treasurer()
        self.dept = Department.objects.create(name="Roof", fund_type="LOCAL",
                                              category="DEVELOPMENT")
        self.member = Member.objects.create(name="Faith Member", phone="254700999000")
        self.camp = PledgeCampaign.objects.create(name="Roof Appeal",
                                                  target_department=self.dept)
        self.p = Pledge.objects.create(campaign=self.camp, member=self.member,
            amount=Decimal("10000"), status="ACTIVE",
            start_date=dt.date(2026, 1, 1), end_date=dt.date(2026, 12, 31))

    def test_auto_match_keeps_extra_giving_on_a_completed_pledge(self):
        # two gifts totalling 15,000; the pledge is 10,000 and there is no
        # other promise, so the extra still belongs on this tracker
        for amt, ref in [("6000", "R1"), ("9000", "R2")]:
            Transaction.objects.create(date=dt.date(2026, 6, 10), channel="BANK",
                direction="CREDIT", amount=Decimal(amt), department=self.dept,
                member=self.member, allocation_status="AUTO", confirmed=True,
                core_ref=ref)
        applied = match_svc.auto_match_pledge(self.p, user=self.u)
        self.assertEqual(applied, Decimal("15000"))
        self.p.refresh_from_db()
        self.assertEqual(self.p.paid, Decimal("15000"))
        self.assertEqual(self.p.status, Pledge.Status.FULFILLED)

    def test_extra_giving_fills_another_open_pledge_first(self):
        other = Pledge.objects.create(
            campaign=self.camp, member=self.member, amount=Decimal("4000"),
            status="ACTIVE", start_date=dt.date(2026, 1, 1),
            end_date=dt.date(2026, 12, 31))
        Transaction.objects.create(date=dt.date(2026, 6, 10), channel="BANK",
            direction="CREDIT", amount=Decimal("15000"), department=self.dept,
            member=self.member, allocation_status="AUTO", confirmed=True,
            core_ref="SPLIT1")
        match_svc.auto_match_all(user=self.u)
        self.p.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.p.paid + other.paid, Decimal("15000"))
        self.assertEqual(self.p.outstanding, Decimal("0"))
        self.assertEqual(other.outstanding, Decimal("0"))
        # leftover beyond both promises stays on a completed pledge
        self.assertEqual(self.p.paid + other.paid, Decimal("15000"))
        self.assertGreaterEqual(max(self.p.paid, other.paid), Decimal("10000"))

    def test_unconfirmed_gift_not_matched(self):
        Transaction.objects.create(date=dt.date(2026, 6, 10), channel="BANK",
            direction="CREDIT", amount=Decimal("5000"), department=self.dept,
            member=self.member, allocation_status="REVIEW", confirmed=False,
            core_ref="UNC1")
        applied = match_svc.auto_match_pledge(self.p, user=self.u)
        self.assertEqual(applied, Decimal("0"))

    def test_contribution_only_matched_once(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 10), channel="BANK",
            direction="CREDIT", amount=Decimal("4000"), department=self.dept,
            member=self.member, allocation_status="AUTO", confirmed=True,
            core_ref="ONCE1")
        match_svc.auto_match_pledge(self.p, user=self.u)
        # a second sweep must not match the same contribution again
        applied2 = match_svc.auto_match_pledge(self.p, user=self.u)
        self.assertEqual(applied2, Decimal("0"))
        self.assertEqual(self.p.payments.count(), 1)


class PledgeWorkflowTests(TestCase):
    def setUp(self):
        self.treas = _treasurer()
        self.asst = _assistant()
        self.dept = Department.objects.create(name="Camp", fund_type="LOCAL",
                                              category="MINISTRY")
        self.member = Member.objects.create(name="Member X")
        self.camp = PledgeCampaign.objects.create(name="Camp Fund")

    def test_assistant_pledge_is_draft_treasurer_is_active(self):
        ca = Client(); ca.force_login(self.asst)
        ca.post("/pledges/new/", {"campaign": self.camp.id, "member": self.member.id,
            "amount": "5000", "frequency": "ONE_OFF", "start_date": "2026-06-01"})
        p = Pledge.objects.get(member=self.member)
        self.assertEqual(p.status, Pledge.Status.DRAFT)
        # treasurer approves
        ct = Client(); ct.force_login(self.treas)
        ct.post(f"/pledges/{p.id}/approve/", {"action": "approve"})
        p.refresh_from_db()
        self.assertEqual(p.status, Pledge.Status.ACTIVE)
        self.assertIsNotNone(p.approved_by)

    def test_lapsed_when_overdue_with_balance(self):
        p = Pledge.objects.create(campaign=self.camp, member=self.member,
            amount=Decimal("1000"), status="ACTIVE",
            end_date=dt.date(2020, 1, 1))  # long past
        p.recompute_status()
        self.assertEqual(p.status, Pledge.Status.LAPSED)
        self.assertTrue(p.is_overdue)

    def test_pages_render(self):
        c = Client(); c.force_login(self.treas)
        p = Pledge.objects.create(campaign=self.camp, member=self.member,
                                  amount=Decimal("1000"), status="ACTIVE")
        for url in ["/pledges/", "/pledges/list/", "/pledges/report/",
                    f"/pledges/{p.id}/", f"/pledges/campaigns/{self.camp.id}/",
                    f"/pledges/member/{self.member.id}/statement/",
                    "/pledges/reminders/"]:
            self.assertEqual(c.get(url).status_code, 200, url)


class PledgeCampaignProgressTests(TestCase):
    def test_campaign_aggregates(self):
        u = _treasurer()
        dept = Department.objects.create(name="Fund", fund_type="LOCAL",
                                         category="OFFERING")
        m1 = Member.objects.create(name="A One")
        m2 = Member.objects.create(name="B Two")
        camp = PledgeCampaign.objects.create(name="Drive", goal_amount=Decimal("100000"))
        Pledge.objects.create(campaign=camp, member=m1, amount=Decimal("30000"),
                              status="ACTIVE")
        p2 = Pledge.objects.create(campaign=camp, member=m2, amount=Decimal("20000"),
                                   status="ACTIVE")
        self.assertEqual(camp.total_pledged, Decimal("50000"))
        # match a real gift to p2
        t = Transaction.objects.create(date=dt.date(2026, 6, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("12000"), department=dept,
            member=m2, allocation_status="MANUAL", confirmed=True)
        PledgePayment.objects.create(pledge=p2, transaction=t, amount=Decimal("12000"),
                                     date=t.date)
        self.assertEqual(camp.total_received, Decimal("12000"))
        self.assertEqual(camp.total_outstanding, Decimal("38000"))


class InlineMatchingHookTests(TestCase):
    """Phase 2: handle_new_contribution behaves per SiteConfig.pledge_match_mode."""

    def setUp(self):
        self.u = _treasurer()
        self.dept = Department.objects.create(name="Hook Fund", fund_type="LOCAL",
                                              category="DEVELOPMENT")
        self.member = Member.objects.create(name="Hook Giver", phone="254700555111")
        self.camp = PledgeCampaign.objects.create(name="Hook Drive",
            target_department=self.dept, status="ACTIVE")
        self.p = Pledge.objects.create(campaign=self.camp, member=self.member,
            amount=Decimal("30000"), status="ACTIVE", start_date=dt.date(2026, 1, 1),
            end_date=dt.date(2026, 12, 31))

    def _contrib(self, amount, ref):
        return Transaction.objects.create(date=dt.date(2026, 6, 20), channel="CASH",
            direction="CREDIT", amount=Decimal(amount), department=self.dept,
            member=self.member, allocation_status="MANUAL", confirmed=True,
            payer_name="Hook Giver", core_ref=ref)

    def test_off_does_nothing(self):
        from core.models import SiteConfig
        from pledges.services.matching import handle_new_contribution
        from pledges.models import PledgePayment, PledgeMatchSuggestion
        cfg = SiteConfig.get(); cfg.pledge_match_mode = "OFF"; cfg.save()
        t = self._contrib("5000", "OFF1")
        note = handle_new_contribution(t, user=self.u)
        self.assertIsNone(note)
        self.assertEqual(PledgePayment.objects.filter(transaction=t).count(), 0)
        self.assertEqual(PledgeMatchSuggestion.objects.filter(transaction=t).count(), 0)

    def test_suggest_creates_pending_only(self):
        from core.models import SiteConfig
        from pledges.services.matching import handle_new_contribution
        from pledges.models import PledgePayment, PledgeMatchSuggestion
        cfg = SiteConfig.get(); cfg.pledge_match_mode = "SUGGEST"; cfg.save()
        t = self._contrib("10000", "SUG1")
        handle_new_contribution(t, user=self.u)
        # a suggestion exists, but no payment yet
        self.assertEqual(PledgeMatchSuggestion.objects.filter(
            transaction=t, status="PENDING").count(), 1)
        self.assertEqual(PledgePayment.objects.filter(transaction=t).count(), 0)

    def test_suggest_confirm_applies_match(self):
        from core.models import SiteConfig
        from pledges.services.matching import handle_new_contribution
        from pledges.models import PledgeMatchSuggestion
        cfg = SiteConfig.get(); cfg.pledge_match_mode = "SUGGEST"; cfg.save()
        t = self._contrib("10000", "SUG2")
        handle_new_contribution(t, user=self.u)
        s = PledgeMatchSuggestion.objects.get(transaction=t)
        c = Client(); c.force_login(self.u)
        c.post(f"/pledges/suggestions/{s.id}/", {"action": "confirm"})
        self.p.refresh_from_db(); s.refresh_from_db()
        self.assertEqual(self.p.paid, Decimal("10000"))
        self.assertEqual(s.status, "CONFIRMED")

    def test_a_later_gift_after_fulfilment_stays_on_the_pledge(self):
        from core.models import SiteConfig
        from pledges.services.matching import handle_new_contribution
        cfg = SiteConfig.get(); cfg.pledge_match_mode = "AUTO"; cfg.save()
        handle_new_contribution(self._contrib("30000", "AUT1"), user=self.u)
        extra = self._contrib("5000", "AUT2")
        handle_new_contribution(extra, user=self.u)
        self.p.refresh_from_db()
        self.assertEqual(self.p.paid, Decimal("35000"))
        self.assertEqual(self.p.status, Pledge.Status.FULFILLED)

    def test_auto_splits_a_gift_across_open_pledges(self):
        from core.models import SiteConfig
        from pledges.services.matching import handle_new_contribution
        cfg = SiteConfig.get(); cfg.pledge_match_mode = "AUTO"; cfg.save()
        other = Pledge.objects.create(
            campaign=self.camp, member=self.member, amount=Decimal("20000"),
            status="ACTIVE", start_date=dt.date(2026, 1, 1),
            end_date=dt.date(2026, 12, 31))
        handle_new_contribution(self._contrib("55000", "SPLIT"), user=self.u)
        self.p.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.p.outstanding, Decimal("0"))
        self.assertEqual(other.outstanding, Decimal("0"))
        self.assertEqual(self.p.paid + other.paid, Decimal("55000"))


class PublicPledgeFormTests(TestCase):
    """The public member pledge form: off by default, write-only, draft-only,
    spam-guarded. Security-sensitive, so the boundaries are tested explicitly."""

    def setUp(self):
        from core.models import SiteConfig
        self.cfg = SiteConfig.get()
        self.camp = PledgeCampaign.objects.create(name="Public Drive", status="ACTIVE")

    def test_disabled_returns_404(self):
        self.cfg.pledge_public_form_enabled = False; self.cfg.save()
        self.assertEqual(Client().get("/pledge/").status_code, 404)

    def test_enabled_renders(self):
        self.cfg.pledge_public_form_enabled = True; self.cfg.save()
        self.assertEqual(Client().get("/pledge/").status_code, 200)

    def test_honeypot_silently_drops(self):
        import time
        self.cfg.pledge_public_form_enabled = True; self.cfg.save()
        c = Client(); c.get("/pledge/"); time.sleep(2.1)
        before = Pledge.objects.count()
        r = c.post("/pledge/", {"name": "Bot", "campaign": self.camp.id,
                                "amount": "5000", "website": "http://x.com"})
        self.assertEqual(r.status_code, 302)            # redirected to thanks
        self.assertEqual(Pledge.objects.count(), before)  # nothing created

    def test_legit_submission_is_unverified_draft(self):
        import time
        self.cfg.pledge_public_form_enabled = True; self.cfg.save()
        c = Client(); c.get("/pledge/"); time.sleep(2.1)
        c.post("/pledge/", {"name": "Genuine Member", "phone": "0700888999",
            "campaign": self.camp.id, "amount": "25000", "frequency": "MONTHLY"})
        p = Pledge.objects.get(submitted_contact__icontains="Genuine Member")
        self.assertEqual(p.status, Pledge.Status.DRAFT)
        self.assertTrue(p.self_submitted)
        self.assertFalse(p.member.active)   # provisional until a treasurer reviews

    def test_phone_is_required(self):
        import time
        self.cfg.pledge_public_form_enabled = True; self.cfg.save()
        c = Client(); c.get("/pledge/"); time.sleep(2.1)
        before = Pledge.objects.count()
        r = c.post("/pledge/", {"name": "No Phone", "campaign": self.camp.id,
                                "amount": "5000"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "M-PESA")
        self.assertEqual(Pledge.objects.count(), before)

    def test_immediate_accept_mode_activates_without_approval(self):
        import time
        from core.models import SiteConfig
        self.cfg.pledge_public_form_enabled = True
        self.cfg.pledge_public_submit_mode = SiteConfig.PledgePublicSubmitMode.ACTIVE
        self.cfg.save()
        c = Client(); c.get("/pledge/"); time.sleep(2.1)
        with self.captureOnCommitCallbacks(execute=True):
            c.post("/pledge/", {"name": "Quick Accept", "phone": "0700111222",
                "campaign": self.camp.id, "amount": "15000"})
        p = Pledge.objects.get(submitted_contact__icontains="Quick Accept")
        self.assertEqual(p.status, Pledge.Status.ACTIVE)
        self.assertTrue(p.self_submitted)
        self.assertIsNotNone(p.approved_at)

    def test_submit_sends_thank_you_sms_when_enabled(self):
        import time
        self.cfg.pledge_public_form_enabled = True
        self.cfg.sms_enabled = True
        self.cfg.pledge_send_submit_thanks = True
        self.cfg.save()
        c = Client(); c.get("/pledge/"); time.sleep(2.1)
        with self.captureOnCommitCallbacks(execute=True):
            c.post("/pledge/", {"name": "Sms Member", "phone": "0700333444",
                "campaign": self.camp.id, "amount": "8000"})
        from pledges.models import PledgeReminderLog
        log = PledgeReminderLog.objects.first()
        self.assertIsNotNone(log)
        self.assertIn("thank you for pledging", log.message.lower())

    def test_too_fast_submit_blocked(self):
        self.cfg.pledge_public_form_enabled = True; self.cfg.save()
        c = Client(); c.get("/pledge/")     # no wait
        before = Pledge.objects.count()
        c.post("/pledge/", {"name": "Too Fast", "campaign": self.camp.id,
                            "amount": "1000"})
        self.assertEqual(Pledge.objects.count(), before)


class PledgeImportTests(TestCase):
    """Treasurer-only bulk pledge import: matching, draft-only, member/campaign
    creation, and the accounting boundary (imported pledges move no money)."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        self.u = User.objects.create_user("imp", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.u.groups.add(g)
        from members.models import Member
        from pledges.models import PledgeCampaign
        self.existing = Member.objects.create(name="EXISTING ONE",
                                              phone="254700111000")
        self.camp = PledgeCampaign.objects.create(name="Build Drive", status="ACTIVE")

    def _file(self, rows):
        import io, openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Pledges"
        ws.append(["Member name", "Phone", "Campaign", "Amount", "Frequency",
                   "Start date", "End date", "Note"])
        for r in rows:
            ws.append(r)
        buf = io.BytesIO(); wb.save(buf); buf.seek(0)
        return buf.getvalue()

    def test_template_downloads(self):
        from django.test import Client
        c = Client(); c.force_login(self.u)
        r = c.get("/pledges/import/?download=1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])

    def test_import_matches_creates_and_drafts(self):
        from django.test import Client
        from django.core.files.uploadedfile import SimpleUploadedFile
        from members.models import Member
        from pledges.models import Pledge, PledgeCampaign
        c = Client(); c.force_login(self.u)
        data = self._file([
            ["EXISTING ONE", "0700111000", "Build Drive", 50000, "Monthly",
             "2026-01-01", "2026-12-31", "m"],
            ["NEW PERSON", "0722333444", "Build Drive", 20000, "One-off", "", "", ""],
            ["THIRD GIVER", "", "Fresh Campaign", 10000, "Annual", "", "", ""],
        ])
        c.post("/pledges/import/", {"file": SimpleUploadedFile("p.xlsx", data)})
        # resolve via the live session
        session = c.session
        plan = session["pledge_import_plan"]
        self.assertIsNotNone(plan[0]["member_id"])      # matched by phone
        self.assertIsNone(plan[1]["member_id"])         # unmatched
        post = {"apply": "1",
                f"member_0": f"member:{plan[0]['member_id']}",
                f"member_1": "create",
                f"member_2": "create",
                f"campaign_2": "create"}
        c.post("/pledges/import/", post)
        self.assertEqual(Pledge.objects.count(), 3)
        self.assertEqual(Pledge.objects.filter(status="DRAFT").count(), 3)
        self.assertTrue(Member.objects.filter(name="NEW PERSON").exists())
        self.assertTrue(PledgeCampaign.objects.filter(name="Fresh Campaign").exists())

    def test_import_does_not_touch_fund_balances(self):
        from django.test import Client
        from django.core.files.uploadedfile import SimpleUploadedFile
        from departments.models import Department
        from reports.services import balances
        from decimal import Decimal
        dept = Department.objects.create(name="BuildFund", fund_type="LOCAL",
                                         category="DEVELOPMENT")
        self.camp.target_department = dept; self.camp.save()
        before = next((r["closing"] for r in
                       balances.department_summary(None, None, consolidated=False)
                       if r["department"].id == dept.id), Decimal("0"))
        c = Client(); c.force_login(self.u)
        data = self._file([["EXISTING ONE", "0700111000", "Build Drive", 50000,
                            "One-off", "", "", ""]])
        c.post("/pledges/import/", {"file": SimpleUploadedFile("p.xlsx", data)})
        session = c.session
        plan = session["pledge_import_plan"]
        c.post("/pledges/import/", {"apply": "1",
               "member_0": f"member:{plan[0]['member_id']}"})
        after = next((r["closing"] for r in
                      balances.department_summary(None, None, consolidated=False)
                      if r["department"].id == dept.id), Decimal("0"))
        self.assertEqual(after, before)  # importing pledges moves no money


class CampaignScopedImportTests(TestCase):
    """Item 5: importing pledges from a campaign page scopes every row to that
    campaign — no Campaign column needed."""

    def setUp(self):
        from django.contrib.auth.models import User, Group
        u = User.objects.create_user("trez", password="x")
        g, _ = Group.objects.get_or_create(name="Treasurer")
        u.groups.add(g)
        self.client.login(username="trez", password="x")
        from pledges.models import PledgeCampaign
        self.camp = PledgeCampaign.objects.create(name="Scoped Fund",
            status="ACTIVE", created_by=u)

    def _file(self):
        import io, openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Pledges"
        ws.append(["Member name", "Phone", "Amount", "Frequency"])
        ws.append(["ALICE SCOPE", "0712345678", 30000, "Monthly"])
        ws.append(["BOB SCOPE", "", 10000, "One-off"])
        buf = io.BytesIO(); wb.save(buf)
        return SimpleUploadedFile("p.xlsx", buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_scoped_import_attaches_all_to_campaign(self):
        from pledges.models import Pledge
        url = f"/pledges/campaigns/{self.camp.id}/import/"
        r = self.client.post(url, {"file": self._file()})
        self.assertEqual(r.status_code, 200)
        plan = self.client.session.get("pledge_import_plan")
        self.assertTrue(plan)
        post = {"apply": "1"}
        for i, p in enumerate(plan):
            post[f"member_{i}"] = "create" if not p["member_id"] else f"member:{p['member_id']}"
        self.client.post(url, post)
        self.assertEqual(Pledge.objects.filter(campaign=self.camp).count(), 2)
        self.assertTrue(all(p.status == Pledge.Status.DRAFT
                            for p in Pledge.objects.filter(campaign=self.camp)))

    def test_campaign_page_has_import_link(self):
        r = self.client.get(f"/pledges/campaigns/{self.camp.id}/")
        self.assertContains(r, f"/pledges/campaigns/{self.camp.id}/import/")
