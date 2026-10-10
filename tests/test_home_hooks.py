"""The platform asks the Home app through hooks (apps.py), never by importing it. Each test drives a PLATFORM function and
checks it reaches Home, and that it degrades sensibly when no app answers (the platform must run without Home)."""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage import server  # noqa: E402
from hearth import services  # noqa: E402


def without(hook):
    """The platform with this hook unanswered (as if no app were installed)."""
    hooks = dict(server.app_ctx.hooks)
    hooks.pop(hook, None)
    return mock.patch.object(server.app_ctx, "hooks", hooks)


class TestHomeAnswersTheHooks(unittest.TestCase):
    def test_home_registers_every_hook_the_platform_asks(self):
        for h in ("courage_home", "notify_phone", "privacy_terms", "gpu_busy", "household", "ground_context"):
            self.assertTrue(callable(server.app_ctx.hook(h)), h)

    def test_the_agent_is_built_from_homes_part(self):
        with mock.patch.object(server, "_courage_agent", None):
            agent = server.get_courage_agent()
        from hearth.reflex import resolve
        self.assertIs(agent.reflex_resolve, resolve)
        self.assertIs(agent.presence_fn, services._courage_presence)
        self.assertIs(agent.tools.deps.camera_scan, services.scan_camera_presets)
        self.assertIsNotNone(agent.learned)

    def test_no_home_means_no_computer_rather_than_a_broken_one(self):
        with without("courage_home"), mock.patch.object(server, "_courage_agent", None):
            with self.assertRaises(RuntimeError):
                server.get_courage_agent()

    def test_phone_pushes_go_through_home(self):
        sent = []
        with mock.patch.dict(server.app_ctx.hooks, notify_phone=lambda t, m, d: sent.append((t, m, d)) or {"ok": True}):
            server._escalation_push("Computer got stuck", "on: where is Rex")
            server._lease_push("Lease", "ten minutes left", [{"action": "RENEW", "title": "Another hour"}], "lease-1")
        self.assertEqual(sent[0][2], {"tag": "computer-stuck", "group": "courage"})
        self.assertEqual(sent[1][2]["actions"][0]["title"], "Another hour")
        with without("notify_phone"):
            self.assertFalse(server._escalation_push("t", "m")["ok"])

    def test_privacy_terms_come_from_home_and_only_grow(self):
        with mock.patch.dict(server._boost_terms_cache, at=0.0, terms=frozenset({"Earlier"})), \
                mock.patch.dict(server.app_ctx.hooks, privacy_terms=lambda: {"Rex", "Bella"}):
            self.assertEqual(server._boost_home_terms(), frozenset({"Earlier", "Rex", "Bella"}))
        with mock.patch.dict(server._boost_terms_cache, at=0.0, terms=frozenset({"Earlier"})), without("privacy_terms"):
            self.assertEqual(server._boost_home_terms(), frozenset({"Earlier"}))

    def test_a_lease_asks_home_whether_the_vision_engine_is_busy(self):
        with mock.patch.dict(server.app_ctx.hooks, gpu_busy=lambda roles: "patrol sweeping Kitchen" if "vision" in roles else None), \
                mock.patch.object(server, "_last_courage_turn", lambda: None):
            self.assertEqual(server._lease_in_use(["vision"]), "patrol sweeping Kitchen")
            self.assertIsNone(server._lease_in_use(["embedder"]))
        with without("gpu_busy"), mock.patch.object(server, "_last_courage_turn", lambda: None):
            self.assertIsNone(server._lease_in_use(["vision"]))

    def test_keyword_grounding_includes_homes_sections(self):
        with mock.patch.dict(server.app_ctx.hooks, ground_context=lambda agent, q: ["### [HOME]: the porch light is on"]):
            text = server.ground_hardware_context("home-agent", "is the porch light on")
        self.assertIn("the porch light is on", text)
        self.assertNotIn("Home Assistant OS", text)          # the platform's wrapper no longer claims a Home Assistant
        with without("ground_context"):
            self.assertEqual(server.ground_hardware_context("home-agent", "is the porch light on"), "")


if __name__ == "__main__":
    unittest.main()
