"""What a fresh install shows (docs/LAYERS.md, "M6 rc.2: a fresh install shows only what is real"), offline: the Home app's side.

  - GET /api/ui/features (stonesage/ui_features.py + the Home app's hearth/ui_status.py hook): every field is a fact about the
    configuration; a fresh install says "nothing is on", a configured one says what is.
  - Appliances (harness/core/home_presence_hub.py): only the washer / dryer Home Assistant has, found by entity name, never
    invented; a dishwasher is not a washer.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from hearth import ui_status  # noqa: E402
from stonesage import ui_features  # noqa: E402

FRESH = {"cluster": {"coordinator_url": "http://127.0.0.1:8001/v1"}, "homeassistant": {"url": "http://homeassistant.local:8123", "token": ""}}
HOMELAB = {
    "features": {"web_terminal": True, "file_editor": True, "shelved": True},
    "obsidian": {"user_vault_path": "/notes"},
    "cluster": {"coordinator_url": "http://10.0.0.5:8001/v1", "worker_url": "http://10.0.0.5:8002/v1", "mcp_url": "http://10.0.0.5:8765"},
    "external_providers": {"gemini": {"api_key": "k"}},
    "homeassistant": {"url": "http://10.0.0.9:8123", "token": "t"},
    "household": {"people": ["Alex", "Sam"], "pets": {"Bella": "cat", "Rex": "dog"}},
    "camera_ui": {"camera.kitchen_hd_stream": {"name": "Kitchen"}},
    "irrigation": {"zones": {"front": {"name": "Front Zone"}}, "rain_delay_switch": "switch.rain", "battery_sensor": "sensor.b"},
}


def build(cfg, apps=("hearth",), home=True, **kw):
    with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.endswith(("_URL", "_KEY", "_HOST", "_VAULT", "_PATH"))},
                         clear=True), tempfile.TemporaryDirectory() as tmp:
        return ui_features.build(cfg, "9.9.9", list(apps), tmp, (lambda: ui_status.ui_features(cfg)) if home else None, **kw)


class TestFreshInstall(unittest.TestCase):
    def test_nothing_optional_is_on(self):
        f = build(FRESH)
        self.assertTrue(f["ok"])
        self.assertEqual((f["product"], f["version"]), ("StoneSage", "9.9.9"))
        self.assertEqual(f["apps"], [{"id": "hearth", "name": "Hearth"}])
        self.assertEqual(f["features"], {"web_terminal": False, "code_tools": False, "raw_ha_call": False, "file_editor": False, "shelved": False})
        self.assertFalse(f["obsidian"])
        self.assertEqual(f["providers"], {"gemini": False})
        self.assertIsNone(f["usage"])
        self.assertFalse(any(f["configured"].values()), f["configured"])

    def test_the_home_block_has_no_zones_cameras_or_names(self):
        home = build(FRESH)["home"]
        self.assertEqual((home["people"], home["pets"], home["cameras"]), ([], [], []))
        self.assertFalse(home["frigate"])
        self.assertEqual(home["irrigation"], {"zones": [], "rain_delay": False, "battery": False})
        self.assertFalse(home["ha"]["token_set"])
        self.assertFalse(home["setup_complete"])
        self.assertEqual(home["questions"], ["Who's home?"])

    def test_the_example_files_irrigation_is_nobodys(self):
        """The Home app reads config.json as it is on disk; the platform's fallback to config.example.json (a homelab's zones)
        must never reach the page. Here: no irrigation block in the config the hook gets."""
        self.assertEqual(ui_status.ui_features({})["irrigation"]["zones"], [])

    def test_the_default_agent_is_named_by_its_soul_else_computer(self):
        self.assertEqual(build(FRESH)["agent"], {"id": "courage-computer", "name": "Computer"})
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "souls", "courage-computer"))
            with open(os.path.join(tmp, "souls", "courage-computer", "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"id": "courage-computer", "name": "Atlas"}, fh)
            self.assertEqual(ui_features.agent_name(tmp), "Atlas")

    def test_a_broken_app_hook_does_not_blank_the_page(self):
        def boom():
            raise RuntimeError("no")
        with tempfile.TemporaryDirectory() as tmp:
            f = ui_features.build(FRESH, "1", ["hearth"], tmp, boom)
        self.assertTrue(f["ok"])
        self.assertIn("RuntimeError", f["home"]["error"])

    def test_without_the_home_app_there_is_no_home_block(self):
        f = build(FRESH, apps=(), home=False)
        self.assertEqual((f["apps"], f["home"]), ([], {}))


class TestConfiguredInstall(unittest.TestCase):
    def test_what_is_set_shows(self):
        f = build(HOMELAB)
        self.assertTrue(f["features"]["file_editor"] and f["features"]["web_terminal"] and f["features"]["shelved"])
        self.assertFalse(f["features"]["code_tools"])
        self.assertTrue(f["obsidian"])
        self.assertTrue(f["providers"]["gemini"])
        self.assertTrue(f["configured"]["worker"] and f["configured"]["mcp"])
        self.assertFalse(f["configured"]["qdrant"] or f["configured"]["vision"])

    def test_the_home_block_names_the_household_and_what_it_has(self):
        home = build(HOMELAB)["home"]
        self.assertEqual(home["people"], ["Alex", "Sam"])
        self.assertEqual(home["pets"], [{"name": "Bella", "species": "cat"}, {"name": "Rex", "species": "dog"}])
        self.assertEqual(home["cameras"], [{"key": "kitchen", "name": "Kitchen"}])
        self.assertEqual(home["irrigation"], {"zones": [{"key": "front", "name": "Front Zone"}], "rain_delay": True, "battery": True})
        self.assertTrue(home["ha"]["token_set"])

    def test_suggested_questions_come_from_the_household(self):
        qs = build(HOMELAB)["home"]["questions"]
        self.assertEqual(qs[:3], ["Who's home?", "Is Alex home?", "Is Sam home?"])
        self.assertIn("Where is Bella?", qs)
        self.assertLessEqual(len(qs), ui_status.MAX_QUESTIONS)
        self.assertFalse([q for q in qs if "cat?" in q.lower() and "bella" not in q.lower()])

    def test_the_obsidian_tab_also_follows_the_couchdb_it_syncs_through(self):
        self.assertTrue(build({"couchdb": {"url": "http://10.0.0.7:5984"}})["obsidian"])

    def test_a_flag_must_be_literally_true(self):
        self.assertFalse(build({"features": {"file_editor": "yes"}})["features"]["file_editor"])


class TestAppliances(unittest.TestCase):
    from harness.core.home_presence_hub import HomePresenceHub as Hub

    def states(self, *ids):
        return {i: {"entity_id": i, "state": "off"} for i in ids}

    def test_found_by_entity_name_not_brand(self):
        found = self.Hub.find_appliances(self.states("sensor.washer_current_status", "switch.washer_power", "sensor.dryer_machine_state"))
        self.assertEqual(set(found), {"washer", "dryer"})
        self.assertEqual(found["washer"], {"status": "sensor.washer_current_status", "power": "switch.washer_power", "remaining": ""})
        self.assertEqual(found["dryer"]["status"], "sensor.dryer_machine_state")

    def test_a_home_without_them_gets_no_monitor(self):
        self.assertEqual(self.Hub.find_appliances(self.states("light.kitchen", "climate.hall")), {})

    def test_a_dishwasher_is_not_a_washer(self):
        self.assertEqual(self.Hub.find_appliances(self.states("sensor.dishwasher_status", "sensor.dishwasher_current_status")), {})

    def test_a_washer_with_no_status_entity_is_not_shown(self):
        self.assertEqual(self.Hub.find_appliances(self.states("switch.washer_power")), {})

    def _status(self, entities):
        from harness.core import home_presence_hub as mod
        with mock.patch.object(mod.hass_connector, "get_all_states", return_value=entities):
            return mod.HomePresenceHub().get_appliance_status()

    def test_status_reports_only_what_was_found(self):
        self.assertEqual(self._status([{"entity_id": "light.kitchen", "state": "on"}]), {})
        out = self._status([{"entity_id": "sensor.dryer_current_status", "state": "running", "last_changed": "2026-01-01T00:00:00+00:00"},
                            {"entity_id": "switch.dryer_power", "state": "on"}])
        self.assertEqual(list(out), ["dryer"])
        self.assertTrue(out["dryer"]["is_running"])
        self.assertEqual(out["dryer"]["name"], "Dryer")

    def test_home_assistant_unreadable_means_no_appliances_not_idle_ones(self):
        from harness.core import home_presence_hub as mod
        with mock.patch.object(mod.hass_connector, "get_all_states", side_effect=RuntimeError("offline")):
            self.assertEqual(mod.HomePresenceHub().get_appliance_status(), {})

    def test_a_status_without_a_power_switch_decides_alone(self):
        out = self._status([{"entity_id": "sensor.washer_status", "state": "standby"}])
        self.assertFalse(out["washer"]["is_running"])
        self.assertIsNone(out["washer"]["power"])
        out = self._status([{"entity_id": "sensor.washer_status", "state": "spinning"}])
        self.assertTrue(out["washer"]["is_running"])


if __name__ == "__main__":
    unittest.main()
