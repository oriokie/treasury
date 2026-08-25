"""Auto-release decides bump vs tag without cutting a real release."""
from django.test import SimpleTestCase

from core.services import auto_release as ar
from core.services import changelog as cl


class BumpPatchTests(SimpleTestCase):
    def test_patch_increments(self):
        self.assertEqual(ar.bump_patch("3.48.0"), "3.48.1")
        self.assertEqual(ar.bump_patch("v3.9.9"), "3.9.10")


class NoteFromCommitsTests(SimpleTestCase):
    def test_one_commit_is_a_sentence(self):
        note = ar.note_from_commits(["Fix the update check"])
        self.assertIn("Fix the update check", note)
        self.assertTrue(note.endswith("."))

    def test_empty_commits_still_have_a_note(self):
        self.assertTrue(ar.note_from_commits([]))


class AutoNotesMergeTests(SimpleTestCase):
    def test_curated_whats_new_wins_over_auto(self):
        # 3.48.0 has curated WHATS_NEW; an auto note must not replace it.
        ar.save_auto_notes({"3.48.0": "auto junk that must not show"})
        try:
            self.assertNotIn("auto junk", cl.notes_for("3.48.0"))
            self.assertIn("update check", cl.notes_for("3.48.0").lower())
        finally:
            ar.save_auto_notes({})

    def test_auto_note_is_used_when_no_curated_entry(self):
        ar.save_auto_notes({"9.9.9": "Automated patch release: something."})
        try:
            self.assertEqual(cl.notes_for("9.9.9"),
                             "Automated patch release: something.")
            self.assertIn("9.9.9", cl.released_versions())
        finally:
            ar.save_auto_notes({})
