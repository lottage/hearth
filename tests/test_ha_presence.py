"""Offline tests for StoneSage -> Home Assistant presence sensors (backend/ha_presence.py). The fixture has the shape of
the live merged presence (2026-09-27): sentry locations without "source", Frigate ones with it, an unnamed "someone",
and a wildlife identity (bird-02) that is not a profile."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from hearth import ha_presence as hp  # noqa: E402

T = 1790546000.0
PRESENCE = {
    "known_entities": {
        "people": [{"name": "Alex", "alert_level": "trusted_resident"}, {"name": "Sam", "alert_level": "trusted_resident"}],
        "pets": [{"name": "Bella", "species": "cat", "breed": "Domestic Shorthair"}, {"name": "Rex", "species": "dog"}],
    },
    "locations": {
        "sam": {"mtime": T - 600, "minutes_ago": 10.0, "camera": "Living", "doing": "walking in the kitchen"},
        "alex": {"mtime": T - 1200, "minutes_ago": 20.0, "camera": "Living"},
        "rex": {"mtime": T, "minutes_ago": 0.0, "camera": "kitchen living room", "source": "frigate"},
        "someone": {"mtime": T + 60, "minutes_ago": 0.0, "camera": "kitchen living room", "source": "frigate"},
        "bird-02": {"mtime": T, "minutes_ago": 5.0},
    },
}


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.out = hp.build(PRESENCE)

    def test_one_timestamp_sensor_per_profile_and_someone(self):
        self.assertEqual(sorted(e for e in self.out if e.startswith("sensor.computer_seen_")),
                         ["sensor.computer_seen_alex", "sensor.computer_seen_bella", "sensor.computer_seen_rex",
                          "sensor.computer_seen_sam", "sensor.computer_seen_someone"])   # no bird-02
        rex = self.out["sensor.computer_seen_rex"]
        self.assertEqual(rex["attributes"]["device_class"], "timestamp")
        self.assertTrue(rex["attributes"]["in_view"])
        self.assertEqual((rex["attributes"]["source"], rex["attributes"]["icon"]), ("frigate", "mdi:dog"))
        self.assertEqual(self.out["sensor.computer_seen_sam"]["attributes"]["source"], "sentry")
        self.assertEqual(self.out["sensor.computer_seen_bella"]["state"], "unknown")   # no sighting: not a stale time

    def test_last_person_is_the_newest_named_person_in_the_sentry_shape(self):
        lp = self.out["sensor.last_person_sighting"]                                  # "someone" is newer but unnamed
        self.assertEqual(lp["state"], "Sam (Resident)")
        self.assertEqual({k: lp["attributes"][k] for k in ("person", "role", "is_resident", "camera")},
                         {"person": "Sam", "role": "Resident", "is_resident": True, "camera": "Living"})
        self.assertTrue(lp["attributes"]["timestamp"])
        self.assertEqual(self.out["sensor.last_pet_sighting"]["state"], "Rex (Dog)")

    def test_a_correction_moves_last_person(self):
        corrected = json.loads(json.dumps(PRESENCE))
        corrected["locations"].pop("sam")                                        # the owner marked it wrong
        self.assertEqual(hp.build(corrected)["sensor.last_person_sighting"]["state"], "Alex (Resident)")

    def test_entity_ids(self):
        self.assertEqual(hp.entity_id("aunt-may"), "sensor.computer_seen_aunt_may")


class TestPublisher(unittest.TestCase):
    def setUp(self):
        self.now = T
        self.posts, self.failing = [], set()
        self.presence = json.loads(json.dumps(PRESENCE))
        self.p = hp.HAPresence(lambda: self.presence, self._set, clock=lambda: self.now)

    def _set(self, eid, state, attrs):
        if eid in self.failing:
            return {"ok": False, "error": "HTTP 500"}
        self.posts.append(eid)
        return {"ok": True}

    def test_posts_everything_then_only_changes(self):
        self.assertEqual(self.p.tick(), 7)
        self.posts.clear()
        self.now += 30
        self.assertEqual(self.p.tick(), 0)
        self.presence["locations"]["alex"] = {"mtime": self.now, "minutes_ago": 0.0, "camera": "kitchen living room",
                                                "source": "frigate"}
        self.p.tick()
        self.assertEqual(sorted(self.posts), ["sensor.computer_seen_alex", "sensor.last_person_sighting"])

    def test_everything_again_after_refresh_for_ha_restarts(self):
        self.p.tick()
        self.posts.clear()
        self.now += hp.REFRESH_S + 1
        self.assertEqual(self.p.tick(), 7)

    def test_a_failed_post_is_retried(self):
        self.failing.add("sensor.last_pet_sighting")
        self.p.tick()
        self.assertIn("sensor.last_pet_sighting", self.p.status()["errors"])
        self.failing.clear()
        self.posts.clear()
        self.now += 30
        self.p.tick()
        self.assertEqual(self.posts, ["sensor.last_pet_sighting"])
        self.assertEqual(self.p.status()["errors"], {})


if __name__ == "__main__":
    unittest.main()
