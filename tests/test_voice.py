"""Offline tests for the voice path: transcript clean-up, STT fallback, and the recorder bench's scoring."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "deploy", "voice-server"))

from hearth import voice_bench  # noqa: E402
from hearth import voice_client  # noqa: E402
from lodestone import endpoints  # noqa: E402
from text_norm import normalize_transcript  # noqa: E402


class TestNormalizeTranscript(unittest.TestCase):
    def test_spaced_acronyms_are_joined(self):
        # Parakeet writes "T V"; the reflex matcher looks for "tv"
        self.assertEqual(normalize_transcript("Turn off the living room T V lights."), "Turn off the living room TV lights.")
        self.assertEqual(normalize_transcript("Turn up the A C"), "Turn up the AC")
        self.assertEqual(normalize_transcript("plug it into the U S B port"), "plug it into the USB port")

    def test_ordinary_text_is_untouched(self):
        for t in ("Where is Rex?", "I want a pizza", "Is Bella in the kitchen?", "Yes."):
            self.assertEqual(normalize_transcript(t), t)

    def test_whitespace_is_tidied(self):
        self.assertEqual(normalize_transcript("  turn   on the light \n"), "turn on the light")
        self.assertEqual(normalize_transcript(""), "")

    def test_the_fixed_transcript_reaches_the_reflex_matcher(self):
        from hearth import reflex
        order = reflex.parse(normalize_transcript("Turn off the living room T V lights."))
        self.assertIsNotNone(order)
        self.assertIn("tv", order["name"])


class TestSttFallback(unittest.TestCase):
    CONFIG = {"voice": {"stt_url": "http://primary", "stt_fallback_url": "http://old", "stt_engine": "parakeet"}}

    def test_the_voice_server_is_used_first(self):
        calls = []

        def post(base, audio, mime, model, timeout):
            calls.append((base, model))
            return {"text": "hello", "engine": "parakeet"}
        with mock.patch.object(voice_client, "_post_audio", post):
            res = voice_client.transcribe(self.CONFIG, b"x" * 200)
        self.assertEqual(calls, [("http://primary", "parakeet")])
        self.assertEqual((res["text"], res["via"]), ("hello", "voice-server"))

    def test_a_dead_voice_server_falls_back_to_the_old_one(self):
        def post(base, audio, mime, model, timeout):
            if base == "http://primary":
                raise ConnectionRefusedError("down")
            return {"text": "from the old one"}
        with mock.patch.object(voice_client, "_post_audio", post):
            res = voice_client.transcribe(self.CONFIG, b"x" * 200)
        self.assertEqual((res["text"], res["via"]), ("from the old one", "fallback"))

    def test_every_server_failing_raises_with_the_reasons(self):
        def post(*a, **k):
            raise TimeoutError("slow")
        with mock.patch.object(voice_client, "_post_audio", post):
            with self.assertRaises(RuntimeError) as ctx:
                voice_client.transcribe(self.CONFIG, b"x" * 200)
        self.assertIn("voice-server", str(ctx.exception))
        self.assertIn("fallback", str(ctx.exception))

    def test_defaults_apply_without_config(self):
        self.assertEqual(voice_client.settings({})["stt_url"], endpoints.url("voice", {}))   # the endpoint table's
        self.assertEqual(voice_client.settings({"voice": {"timeout_s": 3}})["timeout_s"], 3)


class TestBenchScoring(unittest.TestCase):
    def test_word_errors(self):
        self.assertEqual(voice_bench.word_errors("Where's Rex?", "Where is Rex"), 0)         # contractions are equal
        self.assertEqual(voice_bench.word_errors("Set the thermostat to seventy two.", "set the thermostat to 72"), 0)
        self.assertEqual(voice_bench.word_errors("Turn on the porch light.", "Turn on the porch like."), 1)
        self.assertEqual(voice_bench.word_errors("Is Bella in the kitchen?", ""), 5)
        self.assertEqual(voice_bench.word_errors("Yes.", "No"), 1)

    def test_phrase_ids_are_unique_and_far_repeats_exist(self):
        phrases = voice_bench.phrases_for({"people": ["Alex", "Sam"], "pets": {"Bella": "cat", "Rex": "dog"}})
        ids = [p["id"] for p in phrases]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue([p for p in phrases if p["cond"] == "far"])
        for name in ("Rex", "Bella", "Sam", "Alex"):           # the household's names are what the bench tests
            self.assertTrue(any(name in p["text"] for p in phrases), name)
        self.assertEqual(ids[4:8], ["rex", "bella", "sam", "alex"])   # ids = names: saved takes keep matching
        self.assertFalse(any("{" in p["text"] for p in voice_bench.phrases_for({})))

    def _record(self, tmp, phrase_id, texts):
        """save_and_score with every server faked: `texts` maps engine name -> what it 'heard'."""
        def fake(base, audio, mime, engine, timeout=30.0):
            if engine not in texts:
                raise ConnectionError("engine down")
            return {"text": texts[engine], "ms": 100}
        cfg = {"voice": {"stt_url": "http://vm", "stt_fallback_url": "http://old"}, "household": {"people": ["Alex", "Sam"], "pets": {"Bella": "cat", "Rex": "dog"}}}
        with mock.patch.object(voice_bench, "transcribe_with", fake):
            return voice_bench.save_and_score(cfg, tmp, phrase_id, b"x" * 200, "audio/webm")

    def test_save_score_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = {e: "Where is Rex?" for e in voice_bench.ENGINES}
            good["whisper-base"] = "Where is Kyla?"
            good["base.en"] = "Where is Kyle?"
            rec = self._record(tmp, "rex", good)
            by = {r["engine"]: r for r in rec["results"]}
            self.assertEqual(by["parakeet"]["errors"], 0)
            self.assertEqual(by["whisper-base"]["errors"], 1)
            self.assertTrue(os.path.exists(os.path.join(tmp, "voice_samples", rec["file"])))
            s = voice_bench.summary(tmp)
            self.assertEqual(s["recorded"], 1)
            self.assertEqual(s["engines"][0]["wer_pct"], 0.0)                  # best first
            self.assertEqual({e["engine"]: e["wer_pct"] for e in s["engines"]}[voice_bench.LXC], 33.3)

    def test_a_rerecorded_phrase_replaces_the_earlier_take(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._record(tmp, "rex", {e: "Where is Kyle?" for e in voice_bench.ENGINES})
            self._record(tmp, "rex", {e: "Where is Rex?" for e in voice_bench.ENGINES})
            s = voice_bench.summary(tmp)
            self.assertEqual(s["recorded"], 1)
            self.assertEqual(s["engines"][0]["wer_pct"], 0.0)

    def test_a_down_engine_is_counted_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._record(tmp, "yes", {"parakeet": "Yes."})                     # every other engine raises
            s = voice_bench.summary(tmp)
            rows = {e["engine"]: e for e in s["engines"]}
            self.assertEqual(rows["parakeet"]["wer_pct"], 0.0)
            self.assertEqual(rows["moonshine-base"]["failed"], 1)

    def test_far_takes_are_scored_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._record(tmp, "rex", {e: "Where is Rex?" for e in voice_bench.ENGINES})
            self._record(tmp, "rex_far", {e: "Where is Kyle?" for e in voice_bench.ENGINES})
            row = voice_bench.summary(tmp)["engines"][0]
            self.assertEqual(row["far_wer_pct"], 33.3)
            self.assertEqual(row["wer_pct"], 16.7)

    def test_an_unknown_phrase_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._record(tmp, "nope", {})


if __name__ == "__main__":
    unittest.main()
