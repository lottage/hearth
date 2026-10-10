"""apps.py: how the platform loads apps by name and routes to them, without importing them (docs/LAYERS.md)."""

import json
from unittest import mock
import os
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage import apps  # noqa: E402


def ctx():
    return apps.AppContext({}, ROOT, lambda: tempfile.gettempdir())


class TestRoutes(unittest.TestCase):
    def test_exact_then_longest_prefix(self):
        r = apps.Routes()
        r.add("GET", "/api/a", "exact")
        r.add("GET", "/api/a/", "short", prefix=True)
        r.add("GET", "/api/a/b/", "long", prefix=True)
        self.assertEqual(r.find("GET", "/api/a"), "exact")
        self.assertEqual(r.find("GET", "/api/a/x"), "short")
        self.assertEqual(r.find("GET", "/api/a/b/c"), "long")
        self.assertIsNone(r.find("POST", "/api/a"))
        self.assertIsNone(r.find("GET", "/api/other"))

    def test_two_apps_cannot_claim_the_same_path(self):
        r = apps.Routes()
        r.add("GET", "/api/x", "one", owner="home")
        r.add("GET", "/api/x", "again", owner="home")           # the same app may re-register (reload)
        with self.assertRaises(ValueError):
            r.add("GET", "/api/x", "theirs", owner="other")


class TestLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sys.path.insert(0, self.tmp)

    def tearDown(self):
        sys.path.remove(self.tmp)
        for name in [m for m in sys.modules if m.split(".")[0] in ("goodapp", "badapp")]:
            del sys.modules[name]

    def make_app(self, name, body):
        os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
        open(os.path.join(self.tmp, name, "__init__.py"), "w").close()
        with open(os.path.join(self.tmp, name, "app.py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))

    def test_an_app_registers_routes_hooks_and_starters(self):
        self.make_app("goodapp", """
            started = []
            def install(ctx):
                ctx.add_route("GET", "/api/good", lambda h, parsed, body: None)
                ctx.hooks["notify_phone"] = lambda msg: "sent " + msg
                ctx.on_start(lambda: started.append(True))
        """)
        c = ctx()
        self.assertEqual(apps.load_apps(["goodapp"], c), ["goodapp"])
        self.assertIsNotNone(c.routes.find("GET", "/api/good"))
        self.assertEqual(c.routes.table()[0]["app"], "goodapp")
        self.assertEqual(c.hook("notify_phone")("hi"), "sent hi")
        c.start()
        self.assertEqual(sys.modules["goodapp.app"].started, [True])

    def test_a_broken_app_is_skipped_and_the_rest_load(self):
        self.make_app("badapp", "def install(ctx):\n    raise RuntimeError('boom')\n")
        self.make_app("goodapp", "def install(ctx):\n    ctx.hooks['x'] = 1\n")
        c = ctx()
        with self.assertLogs("StoneSage.apps", level="ERROR") as logs:
            loaded = apps.load_apps(["badapp", "missingapp", "goodapp"], c)
        self.assertEqual(loaded, ["goodapp"])
        self.assertEqual(len(logs.output), 2)                  # both failures are reported, not swallowed
        self.assertEqual(c.hooks, {"x": 1})

    def test_an_old_app_name_loads_its_replacement_once(self):
        self.make_app("goodapp", "def install(ctx):\n    ctx.hooks['x'] = 1\n")
        c = ctx()
        with mock.patch.dict(apps.APP_ALIASES, {"oldname": "goodapp"}):
            self.assertEqual(apps.load_apps(["oldname", "goodapp"], c), ["goodapp"])
        self.assertEqual(c.hooks, {"x": 1})
        self.assertEqual(apps.APP_ALIASES["home"], "hearth")   # existing installs have "apps": ["home"] in config.json

    def test_a_missing_hook_is_none_so_the_platform_can_run_without_apps(self):
        self.assertIsNone(ctx().hook("notify_phone"))

    def test_a_failing_start_hook_does_not_stop_the_others(self):
        c, ran = ctx(), []
        c.on_start(lambda: 1 / 0)
        c.on_start(lambda: ran.append(True))
        with self.assertLogs("StoneSage.apps", level="ERROR"):
            c.start()
        self.assertEqual(ran, [True])


class TestNothingIsLent(unittest.TestCase):
    """During the split server.py lent its internals to Home (22 names on 2026-10-09). That debt is paid: Home owns its
    clients and services, and gets only the platform's named services. It must stay that way."""

    def test_the_server_lends_no_internals_and_owns_no_home_clients(self):
        from stonesage import server
        self.assertFalse(hasattr(server, "HOME_LENT"))
        self.assertNotIn("lent", server.app_ctx.services)
        for name in ("hass", "immich"):
            self.assertFalse(hasattr(server, name), f"server.{name}: the Home app owns that client now")

    def test_homes_clients_are_live_references_that_follow_a_rebuild(self):
        from hearth import app, services
        before = services.immich
        try:
            services.immich = type("Rebuilt", (), {"server_url": "http://rebuilt.test"})()
            self.assertEqual(app.immich.server_url, "http://rebuilt.test")      # a live reference, not a stale copy
        finally:
            services.immich = before


class TestHomeHooksAddedLast(unittest.TestCase):
    def test_config_saved_refreshes_homes_clients(self):
        from stonesage import server
        from hearth import services
        with mock.patch.object(services.hass, "set_token") as set_token:
            server.app_ctx.hook("config_saved")({"homeassistant": {"token": "new-token"}, "immich": {}})
        set_token.assert_called_once_with("new-token")

    def test_chat_action_checks_and_runs_a_plain_order_and_ignores_other_text(self):
        from hearth import services
        calls = []
        with mock.patch.object(services.hass, "call_service", side_effect=lambda d, s, data: calls.append((d, s)) or {"ok": True}):
            self.assertIsNone(services.chat_action("tell me a joke"))
            done = services.chat_action("set the thermostat to 70")
        note, badge = done                                     # whitelisted: one checked call, a note and a badge back
        self.assertEqual(calls, [("climate", "set_temperature")])
        self.assertIn("EXECUTION", note)
        self.assertTrue(badge.startswith("Executed"))


class TestServerUsesIt(unittest.TestCase):
    def test_home_is_loaded_by_default_and_app_routes_win(self):
        from route_harness import call
        from stonesage import server
        self.assertIn("hearth", server.app_ctx.apps)

        def hello(handler, parsed, body):
            handler.send_json({"ok": True, "from": "app", "q": parsed.query, "body": body})
        server.app_ctx.routes.add("GET", "/api/test-app-route", hello)
        server.app_ctx.routes.add("POST", "/api/test-app-route", hello)
        status, _, payload = call("GET", "/api/test-app-route?x=1")
        self.assertEqual((status, json.loads(payload)), (200, {"ok": True, "from": "app", "q": "x=1", "body": {}}))
        status, _, payload = call("POST", "/api/test-app-route", {"a": 2})
        self.assertEqual(json.loads(payload)["body"], {"a": 2})


if __name__ == "__main__":
    unittest.main()
