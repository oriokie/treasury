"""A cash count previously could only be created, never fixed or removed —
a mis-keyed denomination or a missed witness meant living with a wrong
record forever. Adds edit (CountSessionUpdateView) and delete
(CountSessionDeleteView), gated the same as recording a count
(Treasurer/Assistant)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR
from envelopes.models import CountSession, CountDenomination, CountWitness

SAB = dt.date(2026, 6, 27)


def _assistant(username="cc_asst"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
    return u


def _auditor(username="cc_aud"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
    return u


class CountEditDeleteTests(TestCase):
    def setUp(self):
        self.u = _assistant()
        self.client.force_login(self.u)
        self.cs = CountSession.objects.create(
            date=SAB, counted_total=Decimal("1500"), expected_total=Decimal("1500"),
            note="original note", recorded_by=self.u)
        CountDenomination.objects.create(session=self.cs, denomination=Decimal("1000"), count=1)
        CountDenomination.objects.create(session=self.cs, denomination=Decimal("500"), count=1)
        CountWitness.objects.create(session=self.cs, name="Jane", role="deacon", signed=True)

    def test_edit_page_prefills_existing_values(self):
        r = self.client.get(reverse("count_edit", args=[self.cs.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "original note")
        self.assertContains(r, "Jane")

    def test_edit_updates_denominations_and_recomputes_total(self):
        r = self.client.post(reverse("count_edit", args=[self.cs.pk]), {
            "date": SAB.isoformat(), "d_1000": "2", "d_500": "1", "note": "corrected",
            "w_name": ["Jane", "John"], "w_role": ["deacon", "elder"],
            "w_signed_0": "on", "w_signed_1": "on"})
        self.assertEqual(r.status_code, 302)
        self.cs.refresh_from_db()
        self.assertEqual(self.cs.counted_total, Decimal("2500"))
        self.assertEqual(self.cs.note, "corrected")
        self.assertEqual(self.cs.denominations.count(), 2)
        self.assertEqual(self.cs.witnesses.count(), 2)
        self.assertTrue(self.cs.witnesses.filter(name="John").exists())

    def test_edit_replaces_witnesses_not_appends(self):
        self.client.post(reverse("count_edit", args=[self.cs.pk]), {
            "date": SAB.isoformat(), "d_1000": "1", "note": "",
            "w_name": ["Only One"], "w_role": [""]})
        self.cs.refresh_from_db()
        self.assertEqual(self.cs.witnesses.count(), 1)
        self.assertEqual(self.cs.witnesses.first().name, "Only One")

    def test_delete_removes_session_and_children(self):
        r = self.client.post(reverse("count_delete", args=[self.cs.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(CountSession.objects.filter(pk=self.cs.pk).exists())
        self.assertEqual(CountDenomination.objects.filter(session_id=self.cs.pk).count(), 0)
        self.assertEqual(CountWitness.objects.filter(session_id=self.cs.pk).count(), 0)

    def test_a_colleague_can_edit_and_delete_too(self):
        """Not creator-restricted: any treasurer/assistant can fix or remove
        any count, the same as they record any count."""
        other = _assistant("cc_asst2")
        self.client.force_login(other)
        r = self.client.post(reverse("count_edit", args=[self.cs.pk]), {
            "date": SAB.isoformat(), "d_1000": "1", "note": "fixed by colleague"})
        self.assertEqual(r.status_code, 302)
        self.cs.refresh_from_db()
        self.assertEqual(self.cs.note, "fixed by colleague")
        r2 = self.client.post(reverse("count_delete", args=[self.cs.pk]))
        self.assertEqual(r2.status_code, 302)
        self.assertFalse(CountSession.objects.filter(pk=self.cs.pk).exists())

    def test_auditor_cannot_edit(self):
        self.client.force_login(_auditor())
        r = self.client.get(reverse("count_edit", args=[self.cs.pk]))
        self.assertNotEqual(r.status_code, 200)

    def test_auditor_cannot_delete(self):
        self.client.force_login(_auditor())
        r = self.client.post(reverse("count_delete", args=[self.cs.pk]))
        self.assertTrue(CountSession.objects.filter(pk=self.cs.pk).exists())

    def test_edit_and_delete_links_appear_on_list_and_detail(self):
        r = self.client.get(reverse("count_list"))
        self.assertContains(r, reverse("count_edit", args=[self.cs.pk]))
        self.assertContains(r, reverse("count_delete", args=[self.cs.pk]))
        r2 = self.client.get(reverse("count_detail", args=[self.cs.pk]))
        self.assertContains(r2, reverse("count_edit", args=[self.cs.pk]))
        self.assertContains(r2, reverse("count_delete", args=[self.cs.pk]))
