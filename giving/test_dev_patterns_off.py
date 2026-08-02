"""Turning every development pattern off turns development detection off.

The reported failure: a treasurer disabled the development patterns and
collections were still being read as development-group giving. The fallback
that exists for a fresh database ("nobody has configured patterns, so use the
built-in spellings") could not tell an empty table from a deliberately emptied
one, so switching detection off switched the built-in version of it on.
"""
from django.test import TestCase

from giving.models import DevGroupPattern
from giving.services.allocation import (clear_pattern_cache, detect_dev_group,
                                        _dev_patterns)


class _Base(TestCase):
    """A migration seeds this table, so a real install always has patterns.
    These tests start from a controlled set rather than the seeded one, so a
    change to the seed cannot quietly rewrite what they assert."""

    def setUp(self):
        DevGroupPattern.objects.all().delete()
        clear_pattern_cache()
        self.addCleanup(clear_pattern_cache)


class SeededInstallTests(TestCase):
    """Against the patterns a real church actually has — the ones the seed
    migration installed — rather than a fixture of our own."""

    def setUp(self):
        clear_pattern_cache()
        self.addCleanup(clear_pattern_cache)

    def test_a_real_install_ships_with_patterns_configured(self):
        """Worth pinning: the built-in fallback is for an empty table, and on a
        seeded install that branch is never taken."""
        self.assertTrue(DevGroupPattern.objects.exists())

    def test_the_seeded_patterns_match_development_references(self):
        self.assertIsNotNone(detect_dev_group("devgrp7"))

    def test_turning_the_seeded_patterns_off_stops_detection(self):
        """Straight from the report: the church's own patterns, all switched
        off on the settings page, must stop matching collections."""
        DevGroupPattern.objects.update(enabled=False)
        clear_pattern_cache()
        for reference in ("devgrp7", "devgr7", "devg14", "dev grp5",
                          "development", "dev"):
            self.assertIsNone(detect_dev_group(reference), reference)


class FreshDatabaseTests(_Base):
    def test_built_in_spellings_apply_when_nothing_is_configured(self):
        """A church whose table is genuinely empty still gets sensible
        recognition rather than none at all."""
        self.assertEqual(DevGroupPattern.objects.count(), 0)
        self.assertEqual(detect_dev_group("devgrp7"), ("NUMBER", 7))
        self.assertEqual(detect_dev_group("development"), ("WORD", None))


class TurnedOffTests(_Base):
    def setUp(self):
        super().setUp()
        DevGroupPattern.objects.create(
            kind="NUMBERED", pattern=r"devgrp0*(\d+)", enabled=True,
            sort_order=1)
        DevGroupPattern.objects.create(
            kind="WORD", pattern=r"development", enabled=True, sort_order=2)

    def test_configured_patterns_are_used_while_enabled(self):
        self.assertEqual(detect_dev_group("devgrp7"), ("NUMBER", 7))
        self.assertEqual(detect_dev_group("development"), ("WORD", None))

    def test_disabling_every_pattern_stops_detection(self):
        DevGroupPattern.objects.update(enabled=False)
        clear_pattern_cache()
        self.assertIsNone(detect_dev_group("devgrp7"))
        self.assertIsNone(detect_dev_group("development"))

    def test_the_built_ins_do_not_creep_back_in(self):
        """The exact reported behaviour: with everything off, a reference the
        BUILT-IN spelling would match must not match either."""
        DevGroupPattern.objects.update(enabled=False)
        clear_pattern_cache()
        numbered, word = _dev_patterns()
        self.assertEqual(numbered, [])
        self.assertEqual(word, [])
        for reference in ("devgr7", "devg14", "devgrp3", "dev", "grp5", "gp39"):
            self.assertIsNone(detect_dev_group(reference), reference)

    def test_disabling_one_kind_leaves_the_other_working(self):
        DevGroupPattern.objects.filter(kind="NUMBERED").update(enabled=False)
        clear_pattern_cache()
        # the numbered pattern is gone, so no group NUMBER can be read...
        self.assertIsNone(detect_dev_group("devgrp7"))
        # ...while the word pattern still recognises development giving
        self.assertEqual(detect_dev_group("development"), ("WORD", None))

    def test_re_enabling_brings_detection_back(self):
        DevGroupPattern.objects.update(enabled=False)
        clear_pattern_cache()
        self.assertIsNone(detect_dev_group("devgrp7"))
        DevGroupPattern.objects.update(enabled=True)
        clear_pattern_cache()
        self.assertEqual(detect_dev_group("devgrp7"), ("NUMBER", 7))


class AllocationTests(_Base):
    def test_a_collection_is_not_allocated_to_development_when_off(self):
        """The consequence the treasurer actually saw: an ordinary collection
        being pulled into development giving."""
        from giving.services.allocation import allocate
        DevGroupPattern.objects.create(
            kind="NUMBERED", pattern=r"devgrp0*(\d+)", enabled=False,
            sort_order=1)
        clear_pattern_cache()
        resolver = allocate("DEVGRP7")
        self.assertNotIn("DEV_GROUP", str(resolver))

