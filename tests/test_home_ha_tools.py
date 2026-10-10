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


if __name__ == "__main__":
    unittest.main()
