"""Hearth's setup wizard (hearth/setup_wizard.py, docs/LAYERS.md M4), offline: a fake Home Assistant on 127.0.0.1, a config file
in a temp directory, and capture of every log record to prove the token never leaves config.json."""

import json
import logging
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage.apps import AppContext  # noqa: E402
from hearth import setup_wizard as sw  # noqa: E402

TOKEN = "eyJ-test-token-0123456789abcdef"
STATES = [
    {"entity_id": "person.alex", "state": "home", "attributes": {"friendly_name": "Alex"}},
    {"entity_id": "person.sam_lee", "state": "away", "attributes": {"friendly_name": "Sam Lee"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
    {"entity_id": "camera.kitchen_hd_stream", "state": "idle", "attributes": {"friendly_name": "Kitchen HD Stream"}},
    {"entity_id": "camera.kitchen_sd_stream", "state": "idle", "attributes": {"friendly_name": "Kitchen SD Stream"}},
    {"entity_id": "camera.driveway_hd_stream_direct", "state": "idle", "attributes": {"friendly_name": "Driveway"}},
]
SERVICES = [
    {"domain": "notify", "services": {
        "mobile_app_alex_phone": {}, "mobile_app_pixel_9": {}, "notify": {},
        "alexa_media_kitchen_echo_dot": {}, "alexa_media_den_echo_show_8_2": {}, "alexa_media_everywhere": {},
        "alexa_media_last_called": {}, "alexa_media_den_echo_dot": {}}},
    {"domain": "light", "services": {"turn_on": {}}},
]


class FakeHA(BaseHTTPRequestHandler):
    seen_auth = []                 # Authorization headers of every request that reached the fake
    redirect_to = None

    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeHA.seen_auth.append((self.server.server_port, self.headers.get("Authorization")))
        if self.path.startswith("/api/") and FakeHA.redirect_to and self.server.server_port != FakeHA.redirect_to[0]:
            return self._send(302, {}, {"Location": FakeHA.redirect_to[1]})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"message": "401: Unauthorized"})
        if self.path == "/api/":
            return self._send(200, {"message": "API running."})
        if self.path == "/api/config":
            return self._send(200, {"version": "2026.9.1", "location_name": "Test House"})
        if self.path == "/api/states":
            return self._send(200, STATES)
        if self.path == "/api/services":
            return self._send(200, SERVICES)
        self._send(404, {})


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Handler:
    """Stands in for the HTTP request handler: records what a route answered."""
    def __init__(self):
        self.status, self.data = None, None

    def send_json(self, data, status=200):
        self.status, self.data = status, data


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "config.json")
        self.ha = serve()
        self.url = f"http://127.0.0.1:{self.ha.server_port}"
        FakeHA.seen_auth, FakeHA.redirect_to = [], None
        self.live = {"unrelated": 1}
        self.saved_hooks = []
        self.agent = mock.Mock()
        self.ctx = AppContext(self.live, ROOT, lambda: self.tmp, services={
            "load_config": lambda: self.read(),
            "save_config": lambda cfg: json.dump(cfg, open(self.path, "w", encoding="utf-8"), indent=2),
            "config_file": self.path,
            "courage_agent_if_built": lambda: self.agent,
        })
        self.ctx.hooks["config_saved"] = lambda cfg: self.saved_hooks.append(cfg)
        self.ctx._current = "hearth"
        mock.patch.object(sw, "ctx", self.ctx).start()
        sw.install(self.ctx)
        self.logs = Captured()
        logging.getLogger().addHandler(self.logs)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        logging.getLogger().removeHandler(self.logs)
        mock.patch.stopall()
        self.ha.shutdown()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def write(self, cfg):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def call(self, method, path, body=None):
        h = Handler()
        fn = self.ctx.routes.find(method, path)
        self.assertIsNotNone(fn, f"{method} {path} is not registered")
        fn(h, mock.Mock(query=""), body or {})
        return h.status, h.data

    def configured(self, **extra):
        cfg = {"homeassistant": {"url": self.url, "token": TOKEN},
               "household": {"people": ["Alex", "Sam"], "owner_pronouns": ["she", "her"],
                             "pets": {"Bella": "cat"}, "phones": {"alex": "mobile_app_alex_phone"},
                             "speakers": {"kitchen": "alexa_media_kitchen"}, "ptz_cameras": ["kitchen"]},
               "camera_ui": {"camera.kitchen_hd_stream": {"name": "Kitchen", "live": "webrtc", "ptz": "kitchen"}},
               "boost": {"enabled": False}}
        cfg.update(extra)
        self.write(cfg)
        return cfg

    def assertNoToken(self, obj):
        self.assertNotIn(TOKEN, json.dumps(obj))

    def assertTokenNotLogged(self):
        self.assertFalse([l for l in self.logs.lines if TOKEN in l], "the token was logged")


class TestStatus(SetupCase):
    def test_no_config_file_is_incomplete(self):
        status, data = self.call("GET", "/api/hearth/setup")
        self.assertEqual(status, 200)
        self.assertFalse(data["complete"])
        self.assertEqual(data["missing"], ["homeassistant.url", "homeassistant.token", "household.people"])
        self.assertEqual(data["homeassistant"], {"url": "", "url_set": False, "token_set": False})

    def test_the_examples_placeholders_do_not_count_as_set(self):
        self.write({"homeassistant": {"url": "http://192.0.2.5:8123", "token": "YOUR_HA_LONG_LIVED_TOKEN"}})
        data = self.call("GET", "/api/hearth/setup")[1]
        self.assertTrue(data["homeassistant"]["url_set"])
        self.assertFalse(data["homeassistant"]["token_set"])
        self.assertIn("homeassistant.token", data["missing"])

    def test_a_configured_home_is_complete_and_never_shows_the_token(self):
        self.configured()
        status, data = self.call("GET", "/api/hearth/setup")
        self.assertTrue(data["complete"])
        self.assertEqual(data["missing"], [])
        self.assertTrue(data["homeassistant"]["token_set"])
        self.assertNoToken(data)
        self.assertNotIn("token", data["homeassistant"])
        self.assertEqual(data["household"]["people"], ["Alex", "Sam"])
        self.assertEqual(data["household"]["phones"], {"alex": "mobile_app_alex_phone"})
        self.assertEqual(data["cameras"], [{"key": "kitchen", "entity": "camera.kitchen_hd_stream", "name": "Kitchen"}])
        self.assertEqual(data["suggested"], [])

    def test_phones_speakers_and_cameras_are_only_suggested(self):
        self.write({"homeassistant": {"url": self.url, "token": TOKEN}, "household": {"people": ["Alex"]}})
        data = self.call("GET", "/api/hearth/setup")[1]
        self.assertTrue(data["complete"])
        self.assertEqual(data["suggested"], ["household.phones", "household.speakers", "cameras"])


class TestConnection(SetupCase):
    def test_ok_reports_the_version(self):
        status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": self.url + "/", "token": TOKEN})
        self.assertEqual((status, data["ok"], data["version"], data["location"]), (200, True, "2026.9.1", "Test House"))
        self.assertNoToken(data)

    def test_a_refused_token_says_so(self):
        status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": self.url, "token": "wrong-token-value"})
        self.assertFalse(data["ok"])
        self.assertIn("refused the token", data["error"])
        self.assertIn("401", data["error"])

    def test_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": f"http://127.0.0.1:{port}", "token": TOKEN})
        self.assertFalse(data["ok"])
        self.assertIn("Cannot reach", data["error"])
        self.assertNoToken(data)

    def test_the_stored_token_serves_the_stored_address_only(self):
        self.configured()
        status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": self.url})
        self.assertTrue(data["ok"], data)
        other = serve()
        try:
            FakeHA.seen_auth.clear()
            status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": f"http://127.0.0.1:{other.server_port}"})
        finally:
            other.shutdown()
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertEqual(FakeHA.seen_auth, [], "the stored token was sent to another address")

    def test_a_masked_token_is_not_a_token(self):
        status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": self.url, "token": "••••cdef"})
        self.assertEqual(status, 400)

    def test_bad_addresses_are_refused_before_any_request(self):
        for bad in ("", "homeassistant.local", "ftp://x/", "file:///etc/passwd", "http://user:pw@host/", None, 5):
            status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": bad, "token": TOKEN})
            self.assertEqual((status, data["ok"]), (400, False), bad)
        self.assertEqual(FakeHA.seen_auth, [])

    def test_the_token_does_not_follow_a_redirect(self):
        other = serve()
        try:
            FakeHA.redirect_to = (other.server_port, f"http://127.0.0.1:{other.server_port}/api/")
            status, data = self.call("POST", "/api/hearth/setup/test-ha", {"url": self.url, "token": TOKEN})
        finally:
            other.shutdown()
        self.assertFalse(data["ok"])
        self.assertEqual([p for p, _ in FakeHA.seen_auth if p == other.server_port], [])
        self.assertTokenNotLogged()


class TestDiscover(SetupCase):
    def test_mapping_from_states_and_services(self):
        d = sw.discover_from(STATES, SERVICES)
        self.assertEqual([(p["key"], p["name"]) for p in d["people"]], [("alex", "Alex"), ("sam_lee", "Sam Lee")])
        self.assertEqual(d["phones"], [{"service": "mobile_app_alex_phone", "person": "alex"},
                                       {"service": "mobile_app_pixel_9", "person": ""}])
        rooms = {s["service"]: s["room"] for s in d["speakers"]}
        self.assertEqual(rooms, {"alexa_media_den_echo_dot": "den", "alexa_media_den_echo_show_8_2": "den_2",
                                 "alexa_media_everywhere": "everywhere", "alexa_media_kitchen_echo_dot": "kitchen"})
        self.assertNotIn("alexa_media_last_called", rooms)
        cams = {c["key"]: c for c in d["cameras"]}
        self.assertEqual(sorted(cams), ["driveway", "kitchen"])
        self.assertEqual(cams["kitchen"]["entity"], "camera.kitchen_hd_stream")
        self.assertEqual(cams["kitchen"]["name"], "Kitchen")
        self.assertEqual(cams["kitchen"]["variants"], ["camera.kitchen_hd_stream", "camera.kitchen_sd_stream"])
        self.assertEqual(cams["driveway"]["entity"], "camera.driveway_hd_stream_direct")
        self.assertEqual(d["pets"], [])

    def test_camera_keys_are_the_households(self):
        from hearth.household import camera_key
        for c in sw.discover_from(STATES, [])["cameras"]:
            self.assertEqual(c["key"], camera_key(c["entity"]))

    def test_the_route_reads_the_stored_connection(self):
        self.configured()
        status, data = self.call("GET", "/api/hearth/setup/discover")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["people"]), 2)
        self.assertNoToken(data)
        self.assertTokenNotLogged()

    def test_without_a_connection_it_says_what_to_do(self):
        status, data = self.call("GET", "/api/hearth/setup/discover")
        self.assertEqual((status, data["ok"]), (400, False))
        self.assertIn("step 1", data["error"])

    def test_a_refused_token_is_a_clear_error(self):
        self.write({"homeassistant": {"url": self.url, "token": "wrong-token-value"}})
        status, data = self.call("GET", "/api/hearth/setup/discover")
        self.assertEqual((status, data["ok"]), (502, False))
        self.assertIn("refused the token", data["error"])

    def test_it_reads_and_writes_nothing(self):
        cfg = self.configured()
        before = open(self.path, "rb").read()
        self.call("GET", "/api/hearth/setup/discover")
        self.assertEqual(open(self.path, "rb").read(), before)
        self.assertEqual(os.listdir(self.tmp), ["config.json"])


class TestSave(SetupCase):
    BODY = {
        "homeassistant": {"url": "http://ha.example:8123/", "token": TOKEN},
        "household": {
            "people": ["Alex", "Sam"], "owner_pronouns": "she/her", "pets": {"Bella": "Cat", "Rex": "dog"},
            "phones": {"alex": "mobile_app_alex_phone"}, "speakers": {"kitchen": "alexa_media_kitchen"},
            "descriptions": {"Alex": "dark hair", "Bella": "grey tabby", "Rex": ""},
        },
        "camera_ui": {"camera.porch": {"name": "Porch"}},
    }

    def test_a_first_save_writes_the_sections_and_no_backup(self):
        status, out = self.call("POST", "/api/hearth/setup/save", self.BODY)
        self.assertEqual(status, 200, out)
        cfg = self.read()
        self.assertEqual(cfg["homeassistant"], {"url": "http://ha.example:8123", "token": TOKEN})
        self.assertEqual(cfg["household"]["pets"], {"Bella": "cat", "Rex": "dog"})
        self.assertEqual(cfg["household"]["owner_pronouns"], ["she", "her"])
        self.assertEqual(cfg["household"]["descriptions"], {"Alex": "dark hair", "Bella": "grey tabby"})
        self.assertEqual(cfg["camera_ui"], {"camera.porch": {"name": "Porch"}})
        self.assertIsNone(out["backup"])
        self.assertTrue(out["status"]["complete"])
        self.assertNoToken(out)
        self.assertTokenNotLogged()

    def test_unknown_keys_survive_and_the_old_file_is_kept(self):
        old = self.configured()
        old["household"]["infra_words"] = ["rack"]
        old["cluster"] = {"coordinator_url": "http://10.0.0.5:8001/v1"}
        self.write(old)
        status, out = self.call("POST", "/api/hearth/setup/save",
                                {"household": {"people": ["Alex", "Sam", "Jo"]}, "camera_ui": {"camera.kitchen_hd_stream": {"name": "Kitchen cam"}}})
        self.assertEqual(status, 200, out)
        cfg = self.read()
        self.assertEqual(cfg["household"]["people"], ["Alex", "Sam", "Jo"])
        self.assertEqual(cfg["household"]["infra_words"], ["rack"])                 # not in the body: untouched
        self.assertEqual(cfg["household"]["phones"], old["household"]["phones"])
        self.assertEqual(cfg["household"]["ptz_cameras"], ["kitchen"])
        self.assertEqual(cfg["cluster"], old["cluster"])
        self.assertEqual(cfg["boost"], {"enabled": False})
        self.assertEqual(cfg["homeassistant"], old["homeassistant"])                # token kept: none was sent
        self.assertEqual(cfg["camera_ui"]["camera.kitchen_hd_stream"],
                         {"name": "Kitchen cam", "live": "webrtc", "ptz": "kitchen"})  # a tile's other settings stay
        backups = [f for f in os.listdir(self.tmp) if f.startswith("config.json.bak-")]
        self.assertEqual(backups, [out["backup"]])
        self.assertEqual(json.load(open(os.path.join(self.tmp, backups[0]))), old)  # the backup is the old file
        self.assertEqual(sorted(out["changed"]), ["camera_ui.camera.kitchen_hd_stream", "household.people"])

    def test_a_masked_or_blank_token_keeps_the_stored_one(self):
        self.configured()
        for tok in ("", "••••cdef", "   "):
            self.call("POST", "/api/hearth/setup/save", {"homeassistant": {"url": self.url, "token": tok}})
            self.assertEqual(self.read()["homeassistant"]["token"], TOKEN)

    def test_a_camera_can_be_removed(self):
        self.configured()
        status, out = self.call("POST", "/api/hearth/setup/save", {"camera_ui_remove": ["camera.kitchen_hd_stream"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.read()["camera_ui"], {})

    def test_nothing_changed_writes_nothing(self):
        self.configured()
        status, out = self.call("POST", "/api/hearth/setup/save", {"household": {"people": ["Alex", "Sam"]}})
        self.assertEqual((status, out["changed"], out["message"]), (200, [], "Nothing changed."))
        self.assertEqual(os.listdir(self.tmp), ["config.json"])

    def test_the_running_app_sees_the_change(self):
        self.configured()
        self.live.update(self.read())
        status, out = self.call("POST", "/api/hearth/setup/save",
                                {"homeassistant": {"url": "http://ha.example:8123", "token": "new-token-value-9876"},
                                 "household": {"people": ["Kim", "Max"], "phones": {"kim": "mobile_app_kim_phone"}}})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.live["household"]["people"], ["Kim", "Max"])           # the live config object, in place
        self.assertEqual(self.live["homeassistant"]["url"], "http://ha.example:8123")
        self.assertEqual(self.live["unrelated"], 1)
        self.assertEqual(len(self.saved_hooks), 1)                                   # config_saved: HA client, Immich
        self.assertEqual(self.saved_hooks[0]["homeassistant"]["token"], "new-token-value-9876")
        self.assertEqual(self.agent.tools.deps.household.people, ["Kim", "Max"])     # Computer's tools
        self.assertEqual(self.agent.tools.deps.household.phones, ["kim"])
        self.assertTrue(out["restart_needed"])
        self.assertTrue(any("phone approvals" in n for n in out["notes"]))

    def test_changes_that_need_no_restart_say_so(self):
        self.configured()
        self.live.update(self.read())
        status, out = self.call("POST", "/api/hearth/setup/save", {"household": {"speakers": {"den": "alexa_media_den"}}})
        self.assertFalse(out["restart_needed"])

    def test_no_agent_yet_is_fine(self):
        self.configured()
        self.ctx.services["courage_agent_if_built"] = lambda: None
        status, out = self.call("POST", "/api/hearth/setup/save", {"household": {"people": ["Kim"]}})
        self.assertEqual(status, 200)

    def test_the_token_is_never_returned_or_logged(self):
        status, out = self.call("POST", "/api/hearth/setup/save", self.BODY)
        self.call("GET", "/api/hearth/setup")
        self.call("POST", "/api/hearth/setup/test-ha", {"url": self.url, "token": TOKEN})
        for body in ({"household": {"people": [""]}, "homeassistant": {"token": TOKEN + " x"}},):
            _, bad = self.call("POST", "/api/hearth/setup/save", body)
            self.assertNoToken(bad)
        self.assertNoToken(out)
        self.assertTokenNotLogged()
        self.assertTrue(self.logs.lines)           # something was logged (field names), so the capture works
        self.assertTrue(any("household.people" in l for l in self.logs.lines))


class TestValidation(SetupCase):
    def errors(self, body):
        status, out = self.call("POST", "/api/hearth/setup/save", body)
        self.assertEqual(status, 400, out)
        self.assertFalse(out["ok"])
        self.assertFalse(os.path.exists(self.path), "a rejected save must write nothing")
        return " | ".join(out["errors"])

    def test_names_must_not_be_empty_or_repeated(self):
        self.assertIn("a name is empty", self.errors({"household": {"people": ["Alex", " "]}}))
        self.assertIn("listed twice", self.errors({"household": {"people": ["Alex", "alex"]}}))
        self.assertIn("a name is empty", self.errors({"household": {"pets": {"": "cat"}}}))
        self.assertIn("needs a species", self.errors({"household": {"pets": {"Bella": ""}}}))

    def test_keys_must_be_lower_case(self):
        self.assertIn("lower-case", self.errors({"household": {"phones": {"Alex": "mobile_app_alex_phone"}}}))
        self.assertIn("lower-case", self.errors({"household": {"speakers": {"living room": "alexa_media_x"}}}))
        self.assertIn("notify service", self.errors({"household": {"phones": {"alex": "notify.mobile_app_x"}}}))

    def test_pronouns_are_a_pair(self):
        for bad in ("she", ["she"], "a/b/c", ["she", ""], 5):
            self.assertIn("pair", self.errors({"household": {"owner_pronouns": bad}}), bad)

    def test_urls_are_http_or_https(self):
        for bad in ("ftp://x", "homeassistant.local:8123", "http://", "javascript:alert(1)", 3):
            self.assertIn("homeassistant.url", self.errors({"homeassistant": {"url": bad}}), bad)

    def test_a_token_with_spaces_is_refused(self):
        self.assertIn("token", self.errors({"homeassistant": {"token": "abc def"}}))

    def test_descriptions_belong_to_someone(self):
        self.assertIn("not one of the people or pets",
                      self.errors({"household": {"people": ["Alex"], "descriptions": {"Zed": "tall"}}}))

    def test_cameras_are_camera_entities_with_names(self):
        self.assertIn("not a camera entity", self.errors({"camera_ui": {"light.x": {"name": "X"}}}))
        self.assertIn("display name", self.errors({"camera_ui": {"camera.x": {"name": " "}}}))

    def test_unknown_fields_and_empty_bodies_are_refused(self):
        self.assertIn("unknown field 'boost'", self.errors({"boost": {"enabled": True}}))
        self.assertIn("household.colour: unknown field", self.errors({"household": {"colour": "red"}}))
        self.assertIn("nothing to save", self.errors({}))


class TestRegisteredInTheApp(unittest.TestCase):
    def test_hearth_registers_the_routes(self):
        from stonesage import server
        table = {(r["method"], r["path"]): r["app"] for r in server.app_ctx.routes.table()}
        for route in (("GET", "/api/hearth/setup"), ("POST", "/api/hearth/setup/test-ha"),
                      ("GET", "/api/hearth/setup/discover"), ("POST", "/api/hearth/setup/save")):
            self.assertEqual(table.get(route), "hearth", route)
        self.assertIn("config_file", server.app_ctx.services)
        self.assertTrue(callable(server.app_ctx.services["courage_agent_if_built"]))


if __name__ == "__main__":
    unittest.main()
