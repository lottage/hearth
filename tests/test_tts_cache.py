"""Offline tests for the spoken-confirmation cache (backend/tts_cache.py)."""

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from hearth import tts_cache  # noqa: E402
from stonesage.courage.agent import CANNED_LINES, already_text, switched_text  # noqa: E402
from test_courage_agent import FakeHA, make_agent, run, ScriptedLLM  # noqa: E402


class Synth:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, text, voice, speed):
        self.calls.append((text, voice, speed))
        return None if self.fail else b"RIFF" + text.encode() + b"x" * 40


class TestTtsCache(unittest.TestCase):
    def test_the_second_request_comes_from_disk(self):
        with tempfile.TemporaryDirectory() as d:
            synth = Synth()
            a, hit_a = tts_cache.get_or_make(d, "Living Room TV Lights off.", "bm_george", 1.06, synth)
            b, hit_b = tts_cache.get_or_make(d, "Living Room TV Lights off.", "bm_george", 1.06, synth)
            self.assertEqual((hit_a, hit_b), (False, True))
            self.assertEqual(a, b)
            self.assertEqual(len(synth.calls), 1)

    def test_voice_speed_and_exact_text_are_part_of_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            synth = Synth()
            for args in (("Done.", "bm_george", 1.06), ("Done.", "am_adam", 1.06), ("Done.", "bm_george", 1.0),
                         ("Done", "bm_george", 1.06)):
                tts_cache.get_or_make(d, *args, synth)
            self.assertEqual(len(synth.calls), 4)

    def test_whitespace_around_the_text_does_not_miss(self):
        with tempfile.TemporaryDirectory() as d:
            synth = Synth()
            tts_cache.get_or_make(d, "Done.", "bm_george", 1.06, synth)
            _a, hit = tts_cache.get_or_make(d, "  Done.  \n", "bm_george", 1.06, synth)
            self.assertTrue(hit)

    def test_a_long_reply_is_not_kept(self):
        with tempfile.TemporaryDirectory() as d:
            synth = Synth()
            long_text = "It is currently seventy two degrees. " * 10
            tts_cache.get_or_make(d, long_text, "bm_george", 1.06, synth)
            tts_cache.get_or_make(d, long_text, "bm_george", 1.06, synth)
            self.assertEqual(len(synth.calls), 2)
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".wav")], [])

    def test_a_failed_synthesis_is_not_cached(self):
        with tempfile.TemporaryDirectory() as d:
            audio, hit = tts_cache.get_or_make(d, "Done.", "bm_george", 1.06, Synth(fail=True))
            self.assertEqual((audio, hit), (None, False))
            good = Synth()
            audio, hit = tts_cache.get_or_make(d, "Done.", "bm_george", 1.06, good)
            self.assertTrue(audio)
            self.assertEqual(len(good.calls), 1)

    def test_the_least_recently_used_clips_go_first(self):
        with tempfile.TemporaryDirectory() as d:
            old_max = tts_cache.MAX_BYTES
            tts_cache.MAX_BYTES = 3 * 60          # room for about three of these tiny clips
            try:
                synth = Synth()
                for i, text in enumerate(("One.", "Two.", "Three.")):
                    tts_cache.get_or_make(d, text, "v", 1.0, synth)
                    os.utime(tts_cache._path(d, text, "v", 1.0), (1000 + i, 1000 + i))
                _a, hit = tts_cache.get_or_make(d, "One.", "v", 1.0, synth)      # touch "One." : now the newest
                self.assertTrue(hit)
                tts_cache.get_or_make(d, "Four.", "v", 1.0, synth)               # over the cap: "Two." is the oldest
                names = {t: os.path.exists(tts_cache._path(d, t, "v", 1.0)) for t in ("One.", "Two.", "Three.", "Four.")}
                self.assertTrue(names["One."] and names["Four."], names)
                self.assertFalse(names["Two."], names)
            finally:
                tts_cache.MAX_BYTES = old_max

    def test_warm_makes_only_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            synth = Synth()
            tts_cache.get_or_make(d, "Done.", "bm_george", 1.06, synth)
            made = tts_cache.warm(d, ["Done.", "Yes.", "No."], "bm_george", 1.06, synth, pause_s=0)
            self.assertEqual(made, 2)
            self.assertEqual(tts_cache.warm(d, ["Done.", "Yes.", "No."], "bm_george", 1.06, synth, pause_s=0), 0)


class TestConfirmationLines(unittest.TestCase):
    def test_lines_cover_on_off_and_already_for_each_device_without_duplicates(self):
        lines = tts_cache.reflex_lines([("Living Room TV Lights", "off"), ("Porch Light", "on")], ["Done.", "Done."])
        self.assertIn("Living Room TV Lights on.", lines)
        self.assertIn("Living Room TV Lights off.", lines)
        self.assertIn("The Living Room TV Lights are already off.", lines)      # plural name -> "are"
        self.assertIn("The Porch Light is already on.", lines)
        self.assertEqual(len(lines), len(set(lines)))
        self.assertEqual(len(lines), 4 * 2 + 1)

    def test_the_lines_are_exactly_what_the_reflex_says(self):
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(), ha=ha)                                # any model call would raise
        said = run(agent, "turn off the kitchen light")[-1]["content"]
        self.assertEqual(said, switched_text("Kitchen Light", "off"))
        self.assertIn(said, tts_cache.reflex_lines([("Kitchen Light", "on")]))
        said = run(agent, "bedroom lamp off")[-1]["content"]
        self.assertEqual(said, already_text("Bedroom Lamp", "off"))
        self.assertIn(said, tts_cache.reflex_lines([("Bedroom Lamp", "off")]))

    def test_canned_lines_include_what_the_loop_says_without_a_model(self):
        self.assertIn("Right. Leaving it alone.", CANNED_LINES)
        agent, _ = make_agent(ScriptedLLM())
        agent.pending.put("s1", {"name": "ha_call", "args": {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
                                 "summary": "turn on the kitchen light"})
        self.assertEqual(run(agent, "no", session="s1")[-1]["content"], "Right. Leaving it alone.")


if __name__ == "__main__":
    unittest.main()
