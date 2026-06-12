"""Workflow coverage for members: creation, editing, the duplicate-merge that
repoints donations and records the absorbed spelling as an alias, search, and
bulk edits."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR
from departments.models import Department
from members.models import Member, MemberAlias
from giving.models import Transaction


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class MemberCrudTests(TestCase):
    def setUp(self):
        self.assistant = _user("m_as", ASSISTANT)
        self.client.force_login(self.assistant)

    def test_create_member(self):
        r = self.client.post(reverse("member_create"),
                             {"name": "Ruth Momanyi", "phone": "0700111222"})
        self.assertIn(r.status_code, (200, 302))
        m = Member.objects.get(name="RUTH MOMANYI")
        self.assertEqual(m.phone, "254700111222")           # normalised
        self.assertEqual(m.name_key, "MOMANYI RUTH")          # order-insensitive key

    def test_detail_and_edit(self):
        m = Member.objects.create(name="Sam Kip", phone="254700333444")
        self.assertEqual(self.client.get(reverse("member_detail", args=[m.pk])).status_code, 200)
        self.client.post(reverse("member_edit", args=[m.pk]),
                         {"name": "Samuel Kip", "phone": "254700333444"})
        m.refresh_from_db()
        self.assertEqual(m.name, "SAMUEL KIP")

    def test_search_finds_member(self):
        Member.objects.create(name="Findable Person", phone="254700999888")
        r = self.client.get(reverse("member_search") + "?q=Findable")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"FINDABLE", r.content)


class MemberMergeTests(TestCase):
    def setUp(self):
        self.assistant = _user("mm_as", ASSISTANT)
        self.client.force_login(self.assistant)
        self.fund = Department.objects.create(name="Tithe", fund_type="TRUST")

    def test_merge_repoints_donations_and_keeps_alias(self):
        keep = Member.objects.create(name="Ruth Momanyi", phone="254700111000")
        absorb = Member.objects.create(name="Ruthie Momanyi", phone="254700111999")
        t = Transaction.objects.create(date=dt.date.today(), channel="BANK",
            direction="CREDIT", amount=Decimal("400"), department=self.fund,
            member=absorb, allocation_status="AUTO", confirmed=True, core_ref="MG1")
        self.client.post(reverse("member_merge"),
                         {"keep": keep.pk, "absorb": absorb.pk})
        t.refresh_from_db()
        self.assertEqual(t.member_id, keep.pk)               # donation repointed
        self.assertFalse(Member.objects.filter(pk=absorb.pk).exists())  # absorbed gone
        # absorbed spelling preserved as an alias so future statements still match
        self.assertTrue(MemberAlias.objects.filter(member=keep,
                                                   name_key="MOMANYI RUTHIE").exists())

    def test_duplicates_page_renders(self):
        self.assertEqual(self.client.get(reverse("member_duplicates")).status_code, 200)


class MemberAccessTests(TestCase):
    def test_auditor_cannot_create_member(self):
        au = _user("m_au", AUDITOR)
        self.client.force_login(au)
        r = self.client.post(reverse("member_create"),
                            {"name": "Should Fail", "phone": "0700000000"})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(Member.objects.filter(name="Should Fail").exists())


class MemberImportExportTests(TestCase):
    def setUp(self):
        self.assistant = _user("mie_as", ASSISTANT)
        self.client.force_login(self.assistant)

    def test_export_streams_csv(self):
        Member.objects.create(name="Exported One", phone="254700000123")
        r = self.client.get(reverse("member_export"))
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"EXPORTED ONE", r.content)

    def test_import_page_renders(self):
        self.assertEqual(self.client.get(reverse("member_import")).status_code, 200)

    def test_bulk_update_group(self):
        m1 = Member.objects.create(name="Bulk A", phone="254700000201")
        m2 = Member.objects.create(name="Bulk B", phone="254700000202")
        self.client.post(reverse("member_bulk"),
            {"ids": [m1.pk, m2.pk], "field": "group", "value": "YOUTH"})
        m1.refresh_from_db(); m2.refresh_from_db()
        self.assertEqual(m1.group, "YOUTH")
        self.assertEqual(m2.group, "YOUTH")
