"""Offline tests for the dashboard's one-touch "Block TikTok" (backend/adguard_blocks.py). AdGuard is a fake that records what it
was sent, so nothing real is touched. The shapes follow AdGuard Home 0.107.x (blocked_services/get|update|all)."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from hearth import adguard_blocks as ab  # noqa: E402


class FakeAdGuard:
    """Stands in for the HTTP layer: (method, url, headers, body, timeout) -> (status, bytes)."""

    def __init__(self, ids=None, schedule=None, services=("tiktok", "youtube"), new_api=True, login_ok=True, up=True, keeps=True):
        self.ids, self.schedule, self.services = list(ids or []), schedule, list(services)
        self.new_api, self.login_ok, self.up, self.keeps = new_api, login_ok, up, keeps
        self.sent = []

    def __call__(self, method, url, headers, body, timeout):
        if not self.up:
            raise OSError("connection refused")
        path = url.split("/control", 1)[1]
        self.sent.append((method, path, json.loads(body) if body else None, headers.get("Authorization")))
        if not self.login_ok:
            return 401, b"Unauthorized"
        if path == "/blocked_services/get":
            return 200, json.dumps({"ids": self.ids, "schedule": self.schedule} if self.new_api else self.ids).encode()
        if path == "/blocked_services/all":
            return 200, json.dumps({"blocked_services": [{"id": s} for s in self.services]}).encode()
        if method == "PUT" and path == "/blocked_services/update":
            if not self.new_api:
                return 404, b""
            if self.keeps:
                self.ids = json.loads(body)["ids"]
            return 200, b"OK"
        if method == "POST" and path == "/blocked_services/set":
            self.ids = json.loads(body)
            return 200, b"OK"
        return 404, b""


def make(fake, **kw):
    puts = []
    client = ab.AdGuardClient("http://adguard", "u", "p", http=fake)
    blocks = ab.AdGuardBlocks("http://ha", "tok", client, set_boolean=puts.append, clock=kw.get("clock", lambda: 1000.0))
    return blocks, puts


class PlanTests(unittest.TestCase):
    def test_adds_only_when_missing_and_keeps_others_in_order(self):
        self.assertEqual(ab.plan(["youtube"], "tiktok", True), (["youtube", "tiktok"], True))
        self.assertEqual(ab.plan(["tiktok", "youtube"], "tiktok", True), (["tiktok", "youtube"], False))

    def test_removes_only_that_service(self):
        self.assertEqual(ab.plan(["youtube", "tiktok", "x"], "tiktok", False), (["youtube", "x"], True))
        self.assertEqual(ab.plan(["youtube"], "tiktok", False), (["youtube"], False))


class ApplyTests(unittest.TestCase):
    def test_block_sends_one_update_keeping_other_services_and_the_schedule(self):
        sched = {"time_zone": "Local", "mon": {"start": 0, "end": 3600000}}
        f = FakeAdGuard(ids=["youtube"], schedule=sched)
        b, puts = make(f)
        self.assertEqual(b.apply(True), "applied")
        self.assertEqual(f.ids, ["youtube", "tiktok"])
        updates = [s for s in f.sent if s[0] == "PUT"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][2], {"ids": ["youtube", "tiktok"], "schedule": sched})
        self.assertTrue(updates[0][3].startswith("Basic "))
        self.assertEqual(puts, [])  # nothing reverted

    def test_unblock_and_idempotence(self):
        f = FakeAdGuard(ids=["tiktok"])
        b, _ = make(f)
        self.assertEqual(b.apply(False), "applied")
        self.assertEqual(f.ids, [])
        before = len([s for s in f.sent if s[0] in ("PUT", "POST")])
        self.assertEqual(b.apply(False), "already in that state")
        self.assertEqual(len([s for s in f.sent if s[0] in ("PUT", "POST")]), before)  # no second write

    def test_old_api_fallback(self):
        f = FakeAdGuard(ids=[], new_api=False)
        b, _ = make(f)
        self.assertEqual(b.apply(True), "applied")
        self.assertEqual(f.ids, ["tiktok"])
        self.assertTrue(any(s[0] == "POST" and s[1] == "/blocked_services/set" for s in f.sent))

    def test_unknown_service_is_refused_and_the_switch_put_back(self):
        f = FakeAdGuard(services=("youtube",))
        b, puts = make(f)
        self.assertIn("no blocked service", b.apply(True))
        self.assertEqual(puts, [False])
        self.assertEqual(f.ids, [])

    def test_bad_login_and_unreachable_put_the_switch_back(self):
        for kw, expect in ((dict(login_ok=False), "rejected the login"), (dict(up=False), "not reachable")):
            b, puts = make(FakeAdGuard(**kw))
            self.assertIn(expect, b.apply(True))
            self.assertEqual(puts, [False], kw)

    def test_a_change_adguard_does_not_keep_is_reported_not_trusted(self):
        f = FakeAdGuard(keeps=False)
        b, puts = make(f)
        self.assertIn("did not keep", b.apply(True))
        self.assertEqual(puts, [False])

    def test_no_credentials(self):
        puts = []
        b = ab.AdGuardBlocks("http://ha", "tok", None, set_boolean=puts.append)
        self.assertIn("no AdGuard credentials", b.apply(True))
        self.assertEqual(puts, [False])
        self.assertFalse(b.status()["credentials"])
        puts.clear()
        b.apply(False)                      # even asking for "off" must leave the tile off, never flip it back to on
        self.assertEqual(puts, [False])


class LoopAndDriftTests(unittest.TestCase):
    def test_its_own_revert_is_not_applied_again(self):
        t = [1000.0]
        f = FakeAdGuard(up=False)
        b, puts = make(f, clock=lambda: t[0])
        b.apply(True)                       # fails, puts the switch back to off
        self.assertTrue(b.own_echo(False))  # the state_changed for that put is ours: the listener skips it
        self.assertFalse(b.own_echo(True))  # a real tap to on is not
        t[0] += ab.OWN_ECHO_S + 1
        self.assertFalse(b.own_echo(False))  # a later, genuine off is applied normally

    def test_reconcile_follows_reality(self):
        t = [1000.0]
        f = FakeAdGuard(ids=["tiktok"])
        b, puts = make(f, clock=lambda: t[0])
        self.assertTrue(b.reconcile())
        self.assertEqual(puts, [True])      # HA told the truth (it had drifted)
        f.ids = []
        self.assertFalse(b.reconcile())
        self.assertEqual(puts, [True, False])

    def test_reconcile_waits_right_after_an_apply(self):
        t = [1000.0]
        f = FakeAdGuard()
        b, puts = make(f, clock=lambda: t[0])
        b.apply(True)
        puts.clear()
        self.assertIsNone(b.reconcile())    # we only just changed it: do not overwrite the tile with a stale read
        t[0] += ab.RECENT_APPLY_S + 1
        self.assertTrue(b.reconcile())

    def test_reconcile_cannot_read_leaves_the_switch_alone(self):
        b, puts = make(FakeAdGuard(up=False))
        self.assertIsNone(b.reconcile())
        self.assertEqual(puts, [])


class ConfigTests(unittest.TestCase):
    def test_off_unless_enabled_and_credentials_from_env(self):
        self.assertIsNone(ab.from_config({}, lambda *_: None))
        cfg = {"homeassistant": {"url": "http://ha", "token": "t"}, "adguard_blocks": {"enabled": True, "url": "http://ag"}}
        os.environ.pop("ADGUARD_USER", None)
        os.environ.pop("ADGUARD_PASSWORD", None)
        self.assertFalse(ab.from_config(cfg, lambda *_: None).status()["credentials"])
        os.environ["ADGUARD_USER"], os.environ["ADGUARD_PASSWORD"] = "u", "p"
        try:
            self.assertTrue(ab.from_config(cfg, lambda *_: None).status()["credentials"])
        finally:
            del os.environ["ADGUARD_USER"], os.environ["ADGUARD_PASSWORD"]


if __name__ == "__main__":
    unittest.main()
