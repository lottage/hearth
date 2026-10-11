"""Cameras, irrigation zones and reflex phrases are the site's own: read from config.json, never built in
(docs/LAYERS.md M2). Each is empty without a config."""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, ROOT)

CFG = {
    "camera_ui": {
        "camera.den_hd_stream": {"name": "Den", "live": "webrtc", "ptz": "den", "default_preset": "Couch"},
        "camera.gate_hd_stream_direct": {"name": "Front Gate", "live": "snapshot"},
    },
    "patrol": {"cameras": {"camera.den_hd_stream": {"home_preset": "Wide"}}},
}


class TestCameras(unittest.TestCase):
    def setUp(self):
        from stonesage import server  # noqa: F401  (binds Home's services)
        from hearth import services
        self.services = services
        p = mock.patch.object(services, "config", CFG)
        p.start()
        self.addCleanup(p.stop)

    def test_the_ptz_registry_is_built_from_camera_ui(self):
        reg = self.services.camera_ptz_registry()
        self.assertEqual(list(reg), ["camera.den_hd_stream"])                  # the gate has no ptz
        den = reg["camera.den_hd_stream"]
        self.assertEqual(den["preset_entity"], "select.den_move_to_preset")
        self.assertEqual(den["move_buttons"]["left"], "button.den_move_left")
        self.assertEqual(den["angle_entity"], "number.den_movement_angle")
        self.assertEqual(den["default_preset"], "Couch")

    def test_default_preset_falls_back_to_the_patrols_home_preset(self):
        cfg = {"camera_ui": {"camera.den_hd_stream": {"ptz": "den"}}, "patrol": CFG["patrol"]}
        with mock.patch.object(self.services, "config", cfg):
            self.assertEqual(self.services.camera_ptz_registry()["camera.den_hd_stream"]["default_preset"], "Wide")

    def test_the_camera_a_question_is_about(self):
        pick = self.services._select_camera_for_query
        self.assertEqual(pick("who is at the gate?"), ("camera.gate_hd_stream_direct", "Front Gate"))
        self.assertEqual(pick("what is in the den"), ("camera.den_hd_stream", "Den"))
        self.assertEqual(pick("anything happening?"), ("camera.den_hd_stream", "Den"))      # the first camera is the default

    def test_no_cameras_means_no_camera(self):
        with mock.patch.object(self.services, "config", {}):
            self.assertEqual(self.services._select_camera_for_query("front door"), ("", ""))
            self.assertIsNone(self.services.capture_live_camera_perception("front door"))
            self.assertEqual(self.services.camera_ptz_registry(), {})


class TestIrrigation(unittest.TestCase):
    def test_zones_come_from_config_and_unknown_ones_are_refused(self):
        from harness.core import irrigation_engine as g
        cfg = {"irrigation": {"zones": {"beds": {"name": "Beds", "valve": "valve.beds"}}, "rain_delay_switch": "switch.rain"}}
        with mock.patch.object(g, "_load_stonesage_config", return_value=cfg):
            zones = g.zone_map()
            self.assertEqual(zones["beds"]["default_duration_mins"], 15)
            self.assertFalse(g.irrigation_engine.open_zone("lawn")["ok"])
        with mock.patch.object(g, "_load_stonesage_config", return_value={}):
            self.assertEqual(g.zone_map(), {})
            self.assertFalse(g.irrigation_engine.set_rain_delay(True)["ok"])


    def test_the_old_garden_key_is_still_read_when_irrigation_is_absent(self):
        from harness.core import irrigation_engine as g
        old = {"garden": {"zones": {"beds": {"name": "Beds", "valve": "valve.beds"}}}}
        with mock.patch.object(g, "_load_stonesage_config", return_value=old):
            self.assertEqual(list(g.zone_map()), ["beds"])
        both = {"garden": {"zones": {"old": {"name": "Old", "valve": "valve.old"}}},
                "irrigation": {"zones": {"new": {"name": "New", "valve": "valve.new"}}}}
        with mock.patch.object(g, "_load_stonesage_config", return_value=both):
            self.assertEqual(list(g.zone_map()), ["new"])           # the new key wins


class TestReflexPrototypes(unittest.TestCase):
    def test_nothing_is_seeded_without_config_and_config_phrases_are(self):
        from harness.data_fabric import system1_reflex as s1
        eng = s1.System1ReflexEngine.__new__(s1.System1ReflexEngine)
        stored = []
        eng.normalize_key = lambda t: t.lower()
        eng._pass0_store_reflex = lambda *a, **k: stored.append(a)
        eng.qdrant = mock.MagicMock()
        eng.collection_name = "c"
        with mock.patch("harness.config._load_stonesage_config", return_value={}):
            eng.seed_default_prototypes()
        self.assertEqual(stored, [])
        cfg = {"system1_prototypes": [{"phrase": "lights off", "domain": "light", "service": "turn_off",
                                       "service_data": {"entity_id": "light.den"}}, {"phrase": "broken"}]}
        with mock.patch("harness.config._load_stonesage_config", return_value=cfg):
            eng.seed_default_prototypes()
        self.assertEqual([a[0] for a in stored], ["lights off"])


class TestPersonalNotes(unittest.TestCase):
    """Which notes count as personal is the site's list (config.json obsidian.personal_*), plus a few generic words."""

    def collection(self, cfg, path, text):
        from stonesage.obsidian_ingestor import ObsidianIngestor
        return ObsidianIngestor(cfg).determine_collection(path, text)

    def test_generic_defaults_and_the_operators_name(self):
        self.assertEqual(self.collection({}, "Personal/budget.md", "x"), "companion_profile")
        self.assertEqual(self.collection({}, "LLM/notes.md", "kernel flags"), "codebase_knowledge")
        self.assertEqual(self.collection({}, "Junk/todo.md", "x"), "agent_memories")
        self.assertEqual(self.collection({"operator": {"name": "Robin"}}, "LLM/n.md", "Robin's plan"), "companion_profile")

    def test_the_sites_own_words_come_from_config(self):
        cfg = {"obsidian": {"personal_paths": ["garage"], "personal_words": ["sourdough"]}}
        self.assertEqual(self.collection(cfg, "Garage/oil.md", "x"), "companion_profile")
        self.assertEqual(self.collection(cfg, "LLM/a.md", "my sourdough starter"), "companion_profile")
        self.assertEqual(self.collection(cfg, "Personal/b.md", "x"), "codebase_knowledge")      # replaced, not added to


if __name__ == "__main__":
    unittest.main()
