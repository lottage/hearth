"""Replies are spoken aloud: no markdown, no U+202F, and the persona says so (plain_text in courage/agent.py)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from stonesage.courage.agent import plain_text  # noqa: E402
from hearth.persona import render_persona  # noqa: E402
from household_fixture import TEST_HOUSEHOLD  # noqa: E402
from agent_fixture import ScriptedLLM, reply, run  # noqa: E402


class TestPlainText(unittest.TestCase):
    def test_bold_italic_and_code_lose_their_markers(self):
        self.assertEqual(plain_text("It's currently **8:32 AM**."), "It's currently 8:32 AM.")
        self.assertEqual(plain_text("It is *very* warm, see `ha_call`."), "It is very warm, see ha_call.")

    def test_headings_and_bullets_lose_their_markers(self):
        self.assertEqual(plain_text("## Lights\n- kitchen on\n* hall off"), "Lights\nkitchen on\nhall off")

    def test_typographic_spaces_become_normal_spaces(self):
        self.assertEqual(plain_text("8:32\u202fAM and 4\u00a0p.m."), "8:32 AM and 4 p.m.")

    def test_ordinary_text_is_untouched(self):
        for text in ("2 * 3 * 4 is 24.", "The living_room lamp is on.", "Done.", "", None):
            self.assertEqual(plain_text(text), text or "")

    def test_idempotent(self):
        once = plain_text("**Bold** and *it* and `x`\n- item")
        self.assertEqual(plain_text(once), once)

    def test_streamed_pieces_add_up_to_the_whole(self):
        pieces = ["It's **sunny** outside.", "\n- kitchen on.", "\n\nThat's all."]
        self.assertEqual("".join(plain_text(p) for p in pieces), plain_text("".join(pieces)))


class TestFinalAnswerIsPlain(unittest.TestCase):
    def test_the_final_event_has_no_markdown(self):
        from test_courage_agent import make_agent
        agent, _ = make_agent(ScriptedLLM(reply("It's currently **8:32 AM**.")))
        final = [e for e in run(agent, "What time is it?") if e["type"] == "final"][-1]
        self.assertEqual(final["content"], "It's currently 8:32 AM.")


class TestPersonaSaysPlainText(unittest.TestCase):
    def test_the_persona_asks_for_plain_replies(self):
        text = render_persona(TEST_HOUSEHOLD)
        self.assertIn("plain text, no markdown", text)


if __name__ == "__main__":
    unittest.main()
