"""The Home Assistant tool pack (hearth/ha_tools.py) and the HA/shell policy split (hearth/ha_policy.py, command_whitelist.py).

The platform's tool registry must work, and say nothing about homes, until the Home app registers into it."""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage import command_whitelist  # noqa: E402
from stonesage import tool_harness  # noqa: E402
from hearth import ha_policy, ha_tools  # noqa: E402

HA_TOOLS = {"home_assistant_call", "home_assistant_entities", "home_assistant_get_state"}


def fresh_registry():
    return tool_harness.ToolRegistry(amem_enabled=False, qdrant_enabled=False)


class TestPlatformWithoutHome(unittest.TestCase):
    def test_a_bare_registry_has_no_home_tools_or_rules(self):
        reg = fresh_registry()
        self.assertFalse(HA_TOOLS & set(reg.tools))
        self.assertNotIn("home_assistant", reg.compile_tool_system_prompt())
        self.assertFalse(any(v.startswith("home_assistant") for v in reg.aliases.values()))

    def test_text_calls_to_home_tools_are_not_recognised_without_the_pack(self):
        with mock.patch.object(tool_harness, "tool_registry", fresh_registry()):
            self.assertEqual(tool_harness.extract_tool_calls_from_text('ha_state(entity_id="light.kitchen")'), [])
            self.assertEqual(len(tool_harness.extract_tool_calls_from_text('read_file(path="README.md")')), 1)


class TestHomePack(unittest.TestCase):
    def setUp(self):
        self.reg = fresh_registry()
        ha_tools.register(self.reg)

    def test_register_adds_the_tools_aliases_and_rules(self):
        self.assertTrue(HA_TOOLS <= set(self.reg.tools))
        self.assertTrue(all(self.reg.tools[t]["is_core"] for t in HA_TOOLS))
        self.assertEqual(self.reg.aliases["ha_state"], "home_assistant_get_state")
        prompt = self.reg.compile_tool_system_prompt()
        self.assertIn("SMART HOME REAL-TIME SENSORY INVARIANTS:", prompt)
        self.assertLess(prompt.index("SMART HOME"), prompt.index("TOOL CALL SYNTAX"))   # rules before the syntax block

    def test_registering_twice_does_not_duplicate_the_rules(self):
        ha_tools.register(self.reg)
        self.assertEqual(self.reg.compile_tool_system_prompt().count("SMART HOME REAL-TIME"), 1)

    def test_text_calls_to_home_tools_are_recognised_with_the_pack(self):
        with mock.patch.object(tool_harness, "tool_registry", self.reg):
            calls = tool_harness.extract_tool_calls_from_text('Let me check: ha_state(entity_id="light.kitchen")')
        self.assertEqual(calls, [("ha_state", {"entity_id": "light.kitchen"})])


class TestActionsAreChecked(unittest.TestCase):
    def no_network(self, *a, **kw):
        raise AssertionError("a refused action must never reach Home Assistant")

    def test_a_rejected_action_never_reaches_home_assistant(self):
        with mock.patch.object(ha_tools, "_get_ha_config", return_value=("http://ha.test", "token")), \
                mock.patch.object(ha_tools.urllib.request, "urlopen", side_effect=self.no_network):
            r = ha_tools.tool_home_assistant_call("lock", "unlock", {"entity_id": "lock.front_door"})
        self.assertFalse(r["ok"])
        self.assertIn("rejected", r["error"])

    def test_a_broken_check_fails_closed(self):
        with mock.patch.object(ha_tools, "validate_ha_action", side_effect=RuntimeError("policy file damaged")), \
                mock.patch.object(ha_tools, "_get_ha_config", return_value=("http://ha.test", "token")), \
                mock.patch.object(ha_tools.urllib.request, "urlopen", side_effect=self.no_network):
            r = ha_tools.tool_home_assistant_call("light", "turn_off", {"entity_id": "light.kitchen"})
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["error"])

    def test_the_default_address_is_not_a_private_lan_ip(self):
        self.assertNotIn("192.168.", ha_tools.DEFAULT_HA_URL)


class TestPolicySplit(unittest.TestCase):
    def test_shell_rules_stay_in_the_platform_and_ha_rules_moved_to_home(self):
        self.assertTrue(callable(command_whitelist.validate_terminal_command))
        self.assertFalse([n for n in dir(command_whitelist) if n.startswith("HA_") or "ha_action" in n])
        self.assertTrue(callable(ha_policy.validate_ha_action))
        self.assertTrue(callable(ha_policy.extract_whitelisted_chat_action))

    def test_the_rules_still_bite(self):
        ok, _ = command_whitelist.validate_terminal_command("rm -rf /")
        self.assertFalse(ok)
        allowed, _, data = ha_policy.validate_ha_action("climate", "set_temperature", {"temperature": 120})
        self.assertTrue(not allowed or 60.0 <= data.get("temperature", 0) <= 85.0)   # refused or clamped to the safe range


class TestHomeNamesComeFromConfigOrHomeAssistant(unittest.TestCase):
    """No thermostat or light of any particular house is built in (docs/LAYERS.md M2)."""

    def lister(self, climate=(), light=()):
        return lambda domain: list({"climate": climate, "light": light}.get(domain, ()))

    def test_no_thermostat_means_no_guess(self):
        with mock.patch.object(ha_policy, "_entity_lister", self.lister()), mock.patch.object(ha_policy, "_settings", lambda: {}):
            ok, msg, _ = ha_policy.validate_ha_action("climate", "set_temperature", {"temperature": 70})
            self.assertFalse(ok)
            self.assertIn("ha_policy.thermostat", msg)
            self.assertIsNone(ha_policy.extract_whitelisted_chat_action("set the thermostat to 70"))

    def test_the_only_climate_entity_is_the_default_and_config_wins(self):
        with mock.patch.object(ha_policy, "_entity_lister", self.lister(climate=["climate.hall"])),                 mock.patch.object(ha_policy, "_settings", lambda: {}):
            ok, _, data = ha_policy.validate_ha_action("climate", "set_temperature", {"temperature": 70})
            self.assertEqual((ok, data["entity_id"]), (True, "climate.hall"))
        two = self.lister(climate=["climate.hall", "climate.den"])
        with mock.patch.object(ha_policy, "_entity_lister", two), mock.patch.object(ha_policy, "_settings", lambda: {}):
            self.assertEqual(ha_policy.default_thermostat(), "")                      # two: it does not pick for you
        with mock.patch.object(ha_policy, "_entity_lister", two),                 mock.patch.object(ha_policy, "_settings", lambda: {"thermostat": "Climate.Den"}):
            self.assertEqual(ha_policy.default_thermostat(), "climate.den")

    def test_lights_come_from_config_words_or_the_closest_entity_name(self):
        lights = self.lister(light=["light.porch_floodlight", "light.back_yard_floodlight", "light.hall"])
        with mock.patch.object(ha_policy, "_entity_lister", lights), mock.patch.object(ha_policy, "_settings", lambda: {}):
            self.assertEqual(ha_policy.light_for("the back yard floodlight"), "light.back_yard_floodlight")
            self.assertEqual(ha_policy.light_for("porch light"), "light.porch_floodlight")
            self.assertEqual(ha_policy.light_for("the kitchen light"), "")
            act = ha_policy.extract_whitelisted_chat_action("turn on the porch light")
            self.assertEqual(act["service_data"], {"entity_id": "light.porch_floodlight"})
        with mock.patch.object(ha_policy, "_entity_lister", lights),                 mock.patch.object(ha_policy, "_settings", lambda: {"chat_lights": {"garden": "light.hall"}}):
            self.assertEqual(ha_policy.light_for("garden lights"), "light.hall")


class TestPolicyHardening(unittest.TestCase):
    def check(self, domain, service, data):
        return ha_policy.validate_ha_action(domain, service, data)[0]

    def test_every_entity_in_a_list_is_checked_not_only_the_first(self):
        self.assertTrue(self.check("light", "turn_on", {"entity_id": ["light.a", "light.b"]}))
        self.assertFalse(self.check("light", "turn_on", {"entity_id": ["light.a", "lock.front_door"]}))
        self.assertFalse(self.check("switch", "turn_on", {"entity_id": "switch.a, cover.garage_door"}))
        self.assertFalse(self.check("switch", "turn_off", {"entity_id": ["switch.a", "switch.reboot_server"]}))

    def test_openers_and_locks_never_go_through_this_route_whatever_the_domain(self):
        for eid in ("lock.front_door", "cover.garage", "alarm_control_panel.home", "valve.main", "siren.hall", "switch.garage_door_opener",
                    "button.garage_door", "switch.gate_opener", "switch.deadbolt", "LOCK.Front"):
            for domain, service in (("switch", "turn_on"), ("button", "press"), ("homeassistant", "turn_on"), ("automation", "trigger")):
                if domain in ha_policy.HA_ALLOWED_SERVICES and service in ha_policy.HA_ALLOWED_SERVICES[domain]:
                    self.assertFalse(self.check(domain, service, {"entity_id": eid}), (domain, service, eid))

    def test_homeassistant_services_need_explicit_entities(self):
        for data in ({}, {"entity_id": "all"}, {"entity_id": ["all"]}, {"area_id": "living_room"}, {"device_id": "abc"},
                     {"entity_id": "light.a", "area_id": "garage"}, {"label_id": "x"}):
            self.assertFalse(self.check("homeassistant", "turn_on", data), data)
        self.assertTrue(self.check("homeassistant", "turn_on", {"entity_id": "light.a"}))

    def test_a_thermostat_order_still_works(self):
        ok, why, data = ha_policy.validate_ha_action("climate", "set_temperature", {"entity_id": "climate.hall", "temperature": 70})
        self.assertTrue(ok, why)

    def test_computer_asks_by_class_not_only_by_exact_service(self):
        from stonesage.courage.tools import always_confirm
        self.assertTrue(always_confirm("lock", "unlock"))
        self.assertTrue(always_confirm("cover", "open_cover"))
        self.assertTrue(always_confirm("alarm_control_panel", "alarm_disarm"))
        self.assertTrue(always_confirm("switch", "turn_on", "switch.garage_door_opener"))
        self.assertTrue(always_confirm("cover", "toggle", "cover.garage_door"))
        self.assertTrue(always_confirm("switch", "toggle", "switch.side_gate"))
        self.assertFalse(always_confirm("switch", "turn_off", "switch.garage_door_opener"), "closing/off does not need a yes")
        self.assertFalse(always_confirm("cover", "close_cover", "cover.garage_door"))
        self.assertFalse(always_confirm("light", "turn_on", "light.kitchen"))
        self.assertFalse(always_confirm("lock", "lock", "lock.front_door"))


if __name__ == "__main__":
    unittest.main()
