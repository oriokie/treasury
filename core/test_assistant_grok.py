"""xAI Grok provider wiring + richer LLM grounding for the assistant."""
from django.test import SimpleTestCase, TestCase

from core.services import assistant


class GrokProviderWiringTests(SimpleTestCase):
    def test_grok_is_a_known_openai_compatible_base(self):
        self.assertEqual(assistant._OPENAI_BASES["GROK"], "https://api.x.ai/v1")
        self.assertEqual(assistant._DEFAULT_MODEL["GROK"], "grok-3")

    def test_grok_host_normalises_to_v1(self):
        self.assertEqual(
            assistant._normalise_openai_base("https://api.x.ai", "GROK"),
            "https://api.x.ai/v1")

    def test_history_messages_are_capped_and_ordered(self):
        hist = [
            {"q": "How much tithe?", "d": {"text": "Tithe was 12,000."}},
            {"q": "And last month?", "d": {"text": "Last month 10,000."}},
        ]
        msgs = assistant._history_messages(hist)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertIn("last month", msgs[2]["content"].lower())


class RichContextTests(TestCase):
    def test_rich_context_includes_the_plain_snapshot(self):
        ctx = assistant._rich_data_context()
        self.assertIn("Today:", ctx)
        self.assertIn("Collections year-to-date", ctx)


class AnswerSourceAndFollowupTests(TestCase):
    def test_rules_answers_are_tagged_live_books(self):
        d = assistant.answer("what needs attention")
        self.assertEqual(d.get("source"), "rules")
        self.assertEqual(d.get("source_label"), "Live books")
        self.assertTrue(d.get("suggestions"))

    def test_followup_cues_are_recognised(self):
        hist = [{"q": "Tithe this month", "d": {"text": "12000"}}]
        self.assertTrue(assistant._looks_like_followup("and last month?", hist))
        self.assertFalse(assistant._looks_like_followup(
            "how much tithe this year", hist))
