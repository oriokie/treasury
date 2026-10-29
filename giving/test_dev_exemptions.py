"""Exemptions keep false development-group matches out of allocation.

``aymissiongrp2`` looks like group 2 to the numbered pattern (the ``grp2``
tail). An exemption that starts with ``aymission``, or that requires the
word ``dev``, leaves it alone while ``devgrp2`` still routes to group 2.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from giving.models import DevGroupExemption, DevGroupPattern
from giving.services.allocation import (allocate, clear_pattern_cache,
                                        detect_dev_group, is_dev_exempt,
                                        normalize_reference)


def _treasurer():
    u = User.objects.create_user("tr_ex", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


def _seed_patterns():
    DevGroupPattern.objects.get_or_create(label="num", defaults=dict(
        pattern=r"(?:dev(?:e?l?o?p?)?(?:gr(?:ou)?p?|gp|g)?|gr(?:ou)?p|gp)0*(\d+)",
        kind="NUMBERED", sort_order=10))
    DevGroupPattern.objects.get_or_create(label="word", defaults=dict(
        pattern=r"(?:dev(?:elop)?|grp|group|gp)", kind="WORD", sort_order=20))
    clear_pattern_cache()


class AymissionFalsePositiveTests(TestCase):
    """The reported case: AY Mission group references look like development."""

    def setUp(self):
        _seed_patterns()

    def test_aymissiongrp2_matches_without_an_exemption(self):
        self.assertEqual(detect_dev_group(normalize_reference("aymissiongrp2")),
                         ("NUMBER", 2))
        self.assertEqual(allocate("aymissiongrp2")[0], "DEV_GROUP_2")

    def test_starts_with_exemption_blocks_aymission(self):
        DevGroupExemption.objects.create(
            label="AY Mission", kind=DevGroupExemption.Kind.STARTS,
            value="aymission", enabled=True)
        clear_pattern_cache()
        norm = normalize_reference("aymissiongrp2")
        self.assertTrue(is_dev_exempt(norm))
        self.assertIsNone(detect_dev_group(norm))
        self.assertNotEqual(allocate("aymissiongrp2")[0], "DEV_GROUP_2")
        # Genuine development is untouched.
        self.assertEqual(allocate("DEVGR7")[0], "DEV_GROUP_7")

    def test_lacks_dev_exemption_blocks_bare_grp(self):
        DevGroupExemption.objects.create(
            label="Must say dev", kind=DevGroupExemption.Kind.LACKS,
            value="dev", enabled=True)
        clear_pattern_cache()
        self.assertIsNone(detect_dev_group(normalize_reference("aymissiongrp2")))
        self.assertIsNone(detect_dev_group(normalize_reference("grp2")))
        self.assertEqual(detect_dev_group(normalize_reference("devgrp2")),
                         ("NUMBER", 2))
        self.assertEqual(allocate("devgrp2")[0], "DEV_GROUP_2")

    def test_disabled_exemption_does_not_apply(self):
        DevGroupExemption.objects.create(
            label="off", kind=DevGroupExemption.Kind.STARTS,
            value="aymission", enabled=False)
        clear_pattern_cache()
        self.assertEqual(detect_dev_group(normalize_reference("aymissiongrp2")),
                         ("NUMBER", 2))

    def test_contains_exemption(self):
        # Without the exemption, "ministrygrp3" looks like group 3.
        self.assertEqual(detect_dev_group(normalize_reference("ministrygrp3")),
                         ("NUMBER", 3))
        DevGroupExemption.objects.create(
            label="Ministry", kind=DevGroupExemption.Kind.CONTAINS,
            value="ministry", enabled=True)
        clear_pattern_cache()
        self.assertIsNone(detect_dev_group(normalize_reference("ministrygrp3")))
        self.assertEqual(detect_dev_group(normalize_reference("DEVGR3")),
                         ("NUMBER", 3))

    def test_regex_exemption(self):
        DevGroupExemption.objects.create(
            label="AY variants", kind=DevGroupExemption.Kind.REGEX,
            value=r"^ay\w*mission", enabled=True)
        clear_pattern_cache()
        self.assertIsNone(detect_dev_group(normalize_reference("aymissiongrp2")))
        self.assertIsNone(detect_dev_group(normalize_reference("ay youth mission grp1")))


class ExemptionPageTests(TestCase):
    def setUp(self):
        _seed_patterns()
        self.tr = _treasurer()
        self.c = Client()
        self.c.force_login(self.tr)

    def test_tester_reports_the_false_positive_then_the_exemption(self):
        body = self.c.get("/dev-patterns/?test=aymissiongrp2").content.decode()
        self.assertIn("development group 2", body)

        self.c.post("/dev-patterns/", {
            "form": "exemption", "label": "AY Mission", "kind": "STARTS",
            "value": "aymission", "enabled": "on"})
        self.assertTrue(DevGroupExemption.objects.filter(label="AY Mission").exists())
        clear_pattern_cache()
        body = self.c.get("/dev-patterns/?test=aymissiongrp2").content.decode()
        self.assertIn("Exempted", body)
        self.assertIn("AY Mission", body)
        self.assertNotIn("development group 2", body)

    def test_value_is_normalised_on_save(self):
        self.c.post("/dev-patterns/", {
            "form": "exemption", "label": "Spaced", "kind": "STARTS",
            "value": "AY Mission", "enabled": "on"})
        ex = DevGroupExemption.objects.get(label="Spaced")
        self.assertEqual(ex.value, "aymission")

    def test_blank_value_rejected(self):
        before = DevGroupExemption.objects.count()
        self.c.post("/dev-patterns/", {
            "form": "exemption", "label": "Empty", "kind": "STARTS",
            "value": "   ", "enabled": "on"})
        self.assertEqual(DevGroupExemption.objects.count(), before)

    def test_invalid_regex_rejected(self):
        before = DevGroupExemption.objects.count()
        self.c.post("/dev-patterns/", {
            "form": "exemption", "label": "Bad", "kind": "REGEX",
            "value": "(unclosed", "enabled": "on"})
        self.assertEqual(DevGroupExemption.objects.count(), before)

    def test_toggle_and_delete(self):
        ex = DevGroupExemption.objects.create(
            label="Tmp", kind=DevGroupExemption.Kind.STARTS, value="tmp")
        self.c.post(f"/dev-patterns/exemptions/{ex.id}/toggle/", {})
        ex.refresh_from_db()
        self.assertFalse(ex.enabled)
        self.c.post(f"/dev-patterns/exemptions/{ex.id}/delete/", {})
        self.assertFalse(DevGroupExemption.objects.filter(pk=ex.id).exists())
