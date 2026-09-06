"""DevelopmentGroup matching never creates rows; codes use contains."""
from django.test import TestCase

from departments.models import DevelopmentGroup
from departments.services.matching import match_existing_dev_group
from statements.services.importer import _resolve


class MatchExistingDevGroupTests(TestCase):
    def setUp(self):
        self.g7 = DevelopmentGroup.objects.create(number=7, name="Youth Fellowship")
        self.g12 = DevelopmentGroup.objects.create(number=12, name="Elders")

    def test_exact_number(self):
        self.assertEqual(match_existing_dev_group(7), self.g7)
        self.assertEqual(match_existing_dev_group("12"), self.g12)

    def test_prefixed_number_contains(self):
        self.assertEqual(match_existing_dev_group("GRP7"), self.g7)
        self.assertEqual(match_existing_dev_group("Group 12"), self.g12)
        self.assertEqual(match_existing_dev_group("devgrp07"), self.g7)

    def test_name_contains(self):
        self.assertEqual(match_existing_dev_group("youth"), self.g7)
        self.assertEqual(match_existing_dev_group("ELDERS"), self.g12)

    def test_unknown_does_not_create(self):
        before = DevelopmentGroup.objects.count()
        self.assertIsNone(match_existing_dev_group(99))
        self.assertIsNone(match_existing_dev_group("NoSuchGroup"))
        self.assertEqual(DevelopmentGroup.objects.count(), before)

    def test_resolve_token_never_creates(self):
        before = DevelopmentGroup.objects.count()
        dept, grp = _resolve("DEV_GROUP_55")
        self.assertIsNotNone(dept)
        self.assertIsNone(grp)
        self.assertEqual(DevelopmentGroup.objects.count(), before)
        dept2, grp2 = _resolve("DEV_GROUP_7")
        self.assertEqual(grp2, self.g7)

    def test_dev_match_code_auto_assigned(self):
        self.assertTrue(self.g7.match_code.startswith("DEV"))
        self.assertNotEqual(self.g7.match_code, self.g12.match_code)
