from django.test import TestCase, Client, override_settings
from django.contrib.auth.models import User
from core.models import SiteConfig

@override_settings(DEBUG=False, ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False)
class RenderTests(TestCase):
    def setUp(self):
        cfg = SiteConfig.get(); cfg.require_2fa_for_treasurers=False; cfg.save()
        self.u = User.objects.create_superuser("rt", "rt@x.com", "pw12345")

    def test_login_renders(self):
        r = self.client.get("/accounts/login/", secure=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "auth-card")

    def test_404_renders_premium(self):
        r = self.client.get("/no-such-page-xyz/", secure=True)
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "ep-card", status_code=404)
        self.assertContains(r, "ep-mark", status_code=404)

    def test_dashboard_renders(self):
        self.client.force_login(self.u)
        r = self.client.get("/", secure=True)
        self.assertEqual(r.status_code, 200)

    def test_2fa_setup_uses_partial(self):
        self.client.force_login(self.u)
        r = self.client.get("/2fa/setup/", secure=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "ws-head")          # partial rendered
        self.assertContains(r, "Set up two-factor")


class SabbathSnapshotTests(TestCase):
    """The dashboard 'Latest Sabbath' snapshot must use the recognised-income basis
    and never double-count the envelope-twin rows."""

    def test_snapshot_excludes_envelope_twins(self):
        import datetime as dt
        from decimal import Decimal
        from django.contrib.auth.models import User
        from departments.models import Department
        from giving.models import Transaction
        from core.models import SiteConfig
        cfg = SiteConfig.get(); cfg.require_2fa_for_treasurers = False; cfg.save()
        u = User.objects.create_superuser("snaprt", "s@x.com", "pw12345")
        d = Department.objects.create(name="Snap Fund", fund_type="LOCAL", category="MINISTRY")
        today = dt.date.today()
        last_sab = today - dt.timedelta(days=(today.weekday() - 5) % 7)
        Transaction.objects.create(date=last_sab, service_sabbath=last_sab, channel="CASH",
            direction="CREDIT", amount=Decimal("1000"), department=d, confirmed=True,
            allocation_status="MANUAL")
        Transaction.objects.create(date=last_sab, service_sabbath=last_sab, channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=d, confirmed=True,
            allocation_status="AUTO")
        # excluded envelope twin — must NOT be counted
        Transaction.objects.create(date=last_sab, service_sabbath=last_sab, channel="ENVELOPE",
            direction="CREDIT", amount=Decimal("9999"), department=d, confirmed=True,
            allocation_status="MANUAL", excluded_from_income=True)
        from django.test import Client
        c = Client(); c.force_login(u)
        r = c.get("/")
        snap = r.context["sabbath"]
        self.assertEqual(snap["total"], Decimal("1500"))   # excludes the 9999 twin
        self.assertEqual(snap["gifts"], 2)                  # twin not counted
        self.assertTrue(snap["has_data"])
