"""first_run.py: a new install's config.json from answers (docs/LAYERS.md M2), never touching the real config."""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage import first_run  # noqa: E402

ANSWERS = {"operator.name": "Kim", "operator.pronouns": "she/her", "household.people": "Kim, Max",
           "household.pets": "Pip:Cat, Moss:dog", "cluster.coordinator_url": "http://10.0.0.5:8001/v1",
           "homeassistant.token": "t0ken"}


class TestBuild(unittest.TestCase):
    def test_answers_become_config(self):
        cfg = first_run.build_config(ANSWERS)
        self.assertEqual(cfg["operator"], {"name": "Kim", "pronouns": ["she", "her"]})
        self.assertEqual(cfg["household"]["people"], ["Kim", "Max"])
        self.assertEqual(cfg["household"]["pets"], {"Pip": "cat", "Moss": "dog"})
        self.assertEqual(cfg["household"]["owner_pronouns"], ["she", "her"])     # the operator is the owner
        self.assertEqual(cfg["cluster"]["coordinator_url"], "http://10.0.0.5:8001/v1")

    def test_existing_config_is_kept_and_blank_answers_change_nothing(self):
        old = {"cluster": {"worker_url": "http://w:2/v1"}, "boost": {"enabled": False}, "operator": {"name": "Kim"}}
        cfg = first_run.build_config({"operator.name": "", "household.people": "Kim"}, old)
        self.assertEqual(cfg["cluster"]["worker_url"], "http://w:2/v1")
        self.assertEqual(cfg["boost"], {"enabled": False})
        self.assertEqual(cfg["operator"]["name"], "Kim")
        self.assertEqual(old, {"cluster": {"worker_url": "http://w:2/v1"}, "boost": {"enabled": False},
                               "operator": {"name": "Kim"}})                  # the input is not modified

    def test_bad_pronouns_fall_back(self):
        self.assertEqual(first_run.build_config({"operator.pronouns": "he"})["operator"]["pronouns"], ["they", "them"])

    def test_the_result_feeds_the_home_app(self):
        from hearth.household import household_from_config
        hh = household_from_config(first_run.build_config(ANSWERS))
        self.assertEqual((hh.owner, hh.owner_pronouns, hh.keys), ("Kim", ("she", "her"), ["kim", "max", "pip", "moss"]))

    def test_missing_questions(self):
        cfg = first_run.build_config(ANSWERS)
        gaps = first_run.missing_questions(cfg)
        self.assertNotIn("operator.name", gaps)
        self.assertIn("cluster.worker_url", gaps)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def test_ask_only_what_is_missing_and_keep_a_backup(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"operator": {"name": "Kim"}}, f)
        asked = []

        def read(q):
            asked.append(q)
            return ""                                   # take every default

        cfg = first_run.load(self.path)
        answers = first_run.ask(cfg, first_run.missing_questions(cfg), read=read, secret=lambda q: "s3cret")
        self.assertFalse(any(q.startswith("Your name") for q in asked))
        self.assertEqual(answers["homeassistant.token"], "s3cret")
        backup = first_run.write(first_run.build_config(answers, cfg), self.path)
        self.assertTrue(backup and os.path.exists(backup))
        written = first_run.load(self.path)
        self.assertEqual(written["operator"]["name"], "Kim")
        self.assertEqual(written["cluster"]["coordinator_url"], "http://127.0.0.1:8001/v1")

    def test_answers_file_and_check(self):
        a = os.path.join(self.dir, "answers.json")
        with open(a, "w", encoding="utf-8") as f:
            json.dump(ANSWERS, f)
        self.assertEqual(first_run.main(["--answers", a, "--config", self.path]), 0)
        self.assertEqual(first_run.load(self.path)["operator"]["name"], "Kim")
        self.assertEqual(first_run.main(["--check", "--config", self.path]), 0)


if __name__ == "__main__":
    unittest.main()
