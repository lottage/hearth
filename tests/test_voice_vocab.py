"""Offline tests for the household vocabulary (deploy/voice-server/vocab.py)."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "deploy", "voice-server"))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

import vocab  # noqa: E402

NAMES = ["Rex", "Bella", "Sam", "Alex", "Rosalind"]          # "Rosalind": a long name, for the two-edit rule


class TestCorrect(unittest.TestCase):
    def test_the_misses_heard_on_real_recordings_are_fixed(self):
        # the kind a recogniser makes on real recordings: a short name one letter off, a long name two letters off
        self.assertEqual(vocab.correct("Where's Bela?", NAMES), "Where's Bella?")
        self.assertEqual(vocab.correct("It is Rosalyn home.", NAMES), "It is Rosalind home.")

    def test_possessives_and_case(self):
        self.assertEqual(vocab.correct("Is Bela's bowl empty", NAMES), "Is Bella's bowl empty")
        self.assertEqual(vocab.correct("where is rosaland", NAMES), "where is Rosalind")

    def test_correct_names_and_ordinary_words_are_untouched(self):
        for t in ("Where's Rex?", "Is Bella in the kitchen?", "Is Sam home?", "Is Alex home?", "Is Rosalind home?",
                  "Turn on the porch light.", "What's the temperature inside?", "Show me the backyard camera.",
                  "Tell me a joke about toasters."):
            self.assertEqual(vocab.correct(t, NAMES), t)

    def test_every_recorded_phrase_survives(self):
        from hearth import voice_bench
        for p in voice_bench.phrases_for({"people": ["Alex", "Sam"], "pets": {"Bella": "cat", "Rex": "dog"}}):
            self.assertEqual(vocab.correct(p["text"], NAMES), p["text"], p["text"])

    def test_a_different_first_letter_is_not_a_near_miss(self):
        self.assertEqual(vocab.correct("Fella is here", NAMES), "Fella is here")   # one edit from Bella, but another name

    def test_short_words_and_far_words_are_left_alone(self):
        self.assertEqual(vocab.correct("Bel is a word", NAMES), "Bel is a word")
        self.assertEqual(vocab.correct("Billy said hello", NAMES), "Billy said hello")     # 2 edits from Bella, too far for a short word
        self.assertEqual(vocab.correct("Alaska is cold", NAMES), "Alaska is cold")

    def test_no_names_or_no_text_is_a_no_op(self):
        self.assertEqual(vocab.correct("Where's Bela?", []), "Where's Bela?")
        self.assertEqual(vocab.correct("", NAMES), "")

    def test_a_swapped_pair_of_letters_is_one_edit(self):
        self.assertEqual(vocab.correct("Where is Belal", NAMES), "Where is Bella")


class TestLoad(unittest.TestCase):
    def test_names_hints_and_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("# comment\nRex\n\n+thermostat  # a device word\n+TV lights\nBella\n")
            path = f.name
        try:
            names, hints = vocab.load(path)
        finally:
            os.unlink(path)
        self.assertEqual(names, ["Rex", "Bella"])
        self.assertEqual(hints, ["Rex", "thermostat", "TV lights", "Bella"])          # names are hinted too; +words only hint
        self.assertEqual(vocab.prompt_from(hints), "Rex, thermostat, TV lights, Bella.")

    def test_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(vocab.load("/nonexistent/vocab.txt"), ([], []))
        self.assertEqual(vocab.prompt_from([]), "")

    def test_the_shipped_example_vocab_file_has_example_names(self):
        names, hints = vocab.load(os.path.join(ROOT, "deploy", "voice-server", "vocab.example.txt"))
        for n in ("Rex", "Bella", "Sam", "Alex"):
            self.assertIn(n, names)
        self.assertNotIn("Computer", names)                    # a common word: hinted, never corrected to
        self.assertIn("Computer", hints)


if __name__ == "__main__":
    unittest.main()
