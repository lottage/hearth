"""Vision during garden time (2026-10-14; docs/garden/DESIGN.md "Vision during garden time").

While the garden holds the RX 6600 the vision server is lent out. Background vision work (patrol, commentary reads, sighting boxes, the
wildlife sentry) uses Frigate straight away and QUEUES the frames that needed the model; person-started vision (a camera question to
Computer, the PTZ "Sweep now") HOT-LOADS the engine back; a takeback names the card it needs. No LAN: fakes for the lease, the engines,
Frigate, the sentry's POST and the clock.
"""

import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))
sys.path.insert(0, os.path.join(ROOT, "deploy", "cluster-bridge"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

from stonesage import auth  # noqa: E402
from stonesage.courage.takeback import FILLER, Takeback  # noqa: E402
from stonesage.vision_queue import VisionQueue  # noqa: E402
from hearth import patrol as pt, vision_defer as vd  # noqa: E402
from hearth.commentary import Commentary  # noqa: E402
from agent_fixture import ScriptedLLM, make_agent, reply, tool_call  # noqa: E402
import test_patrol as tp  # noqa: E402

T0 = 1_800_000_000.0
HOUR = 3600.0


def jpg(shade=100, size=(64, 48)):
    buf = io.BytesIO()
    Image.new("L", size, shade).save(buf, format="JPEG")
    return buf.getvalue()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s

    def sleep(self, s):
        self.t += s


# ---- fakes the takeback tests share (copied from tests/test_cockpit_takeback.py, which ships only to the platform tree) ----
LITE_URL = "http://vm:8006/v1/chat/completions"
FULL_URL = "http://fake:8001/v1/chat/completions"


class TakebackClock:
    """A clock that moves when something sleeps on it, and also lets real threads run a little."""

    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        time.sleep(0.003)
        self.t += s




class FakeLeaseSvc:
    """What Takeback needs of the lease, and what Computer's loop needs of brain(): a held lease that comes back when released."""

    def __init__(self, clock, load_s=30.0, mode="lite", yield_flag=True, roles=("coordinator", "worker")):
        self.clock, self.load_s, self.mode = clock, load_s, mode
        self.lease = {"id": "L1", "layout": "longctx_6750", "state": "active", "yield_to_computer": yield_flag,
                      "evicted_roles": list(roles), "expires": clock() + 3000}
        self.released, self.released_at, self.release_delay = [], None, 0.0
        self.coordinator_down = False

    def _settle(self):
        """The revert's tail: the lease record goes (the spare brain stops) a few seconds after the coordinator answers."""
        if self.lease and self.released_at is not None and not self.coordinator_down                 and self.clock() >= self.released_at + self.load_s + 5:
            self.lease = None

    def yielding(self, role="coordinator"):
        self._settle()
        l = self.lease
        return dict(l) if l and l.get("yield_to_computer") and l.get("state") in ("starting", "active")             and role in l["evicted_roles"] else None

    def brain(self, role="coordinator"):
        self._settle()
        if not self.lease or role not in self.lease["evicted_roles"]:
            return None
        if self.mode == "lite":
            return {"mode": "lite", "url": "http://vm:8006", "until": self.lease["expires"], "yield_to_computer": True}
        return {"mode": "on_loan", "until": self.lease["expires"]}

    def release(self, lease_id=None, why="released", approver=""):
        self.released.append((lease_id, why, approver))
        self.released_at = self.clock()
        time.sleep(self.release_delay)
        return {"ok": not self.coordinator_down}

    def ready(self):
        return (not self.coordinator_down and self.released_at is not None and self.clock() >= self.released_at + self.load_s)


class MiniVM:
    """systemd with Conflicts= between the garden layout and what it evicts; :8001 answers while the coordinator runs."""

    def __init__(self):
        self.running = {"coord", "worker"}
        self.log = []

    def systemctl(self, verb, unit):
        self.log.append((verb, unit))
        if verb == "start":
            if unit == "layout":
                self.running -= {"coord", "worker"}
            elif unit in ("coord", "worker"):
                self.running.discard("layout")
            self.running.add(unit)
        else:
            self.running.discard(unit)
        return {"ok": True}

    def http_ok(self, url):
        port = url.split(":")[2].split("/")[0]
        return {"8001": "coord", "8002": "worker", "8005": "layout", "8006": "lite"}.get(port) in self.running


# ---- the queue ------------------------------------------------------------------------------------------------------------------
class QueueRig(unittest.TestCase):
    def queue(self, cfg=None, persist=True, available=True):
        self.clock = Clock()
        self.up = [available]
        self.calls, self.traces, self.got = [], [], []
        self.failures = [0]
        self.config = {"vision_queue": dict(cfg or {})}

        def vision(frame, prompt, max_tokens, temperature):
            if self.failures[0]:
                self.failures[0] -= 1
                raise OSError("engine hiccup")
            self.calls.append((frame, prompt))
            return "read:" + prompt

        d = tempfile.mkdtemp(prefix="vq-") if persist else None
        self.dir = d
        q = VisionQueue(lambda: self.config, vision, lambda: self.up[0], trace=self.traces.append, data_dir=d, clock=self.clock,
                        sleep=self.clock.sleep)
        self.q = q
        return q

    def events(self, name=None):
        return [t for t in self.traces if t["kind"] == "vision_queue" and (name is None or t["event"] == name)]


class TestQueue(QueueRig):
    def test_frames_are_kept_per_source_while_the_engine_is_away_and_traced(self):
        q = self.queue(available=False)
        for src in ("patrol", "commentary", "box", "sentry"):
            self.assertTrue(q.enqueue(src, "cam", "k", jpg(), "p-" + src)["ok"])
        self.assertEqual(q.depth(), 4)
        self.assertEqual(q.process()["processed"], 0)                      # the engine is away: nothing is read, nothing is lost
        self.assertEqual(q.depth(), 4)
        self.assertEqual([e["source"] for e in self.events("queued")], ["patrol", "commentary", "box", "sentry"])
        self.assertEqual(q.status()["by_source"], {"patrol": 1, "commentary": 1, "box": 1, "sentry": 1})

    def test_after_the_engine_is_back_items_are_read_in_order_and_delivered_late(self):
        q = self.queue(available=False)
        order = []
        q.register_handler("patrol", lambda item, text, frame: order.append(("patrol", item["key"], text)))
        q.register_handler("commentary", lambda item, text, frame: order.append(("commentary", item["key"], text)))
        for i, src in enumerate(("patrol", "commentary", "patrol")):
            q.enqueue(src, "cam", "k", jpg(i), "prompt%d" % i, key="k%d" % i)
            self.clock.advance(5)
        self.up[0] = True
        out = q.process()
        self.assertEqual(out["processed"], 3)
        self.assertEqual(order, [("patrol", "k0", "read:prompt0"), ("commentary", "k1", "read:prompt1"), ("patrol", "k2", "read:prompt2")])
        self.assertEqual(q.depth(), 0)
        self.assertEqual([e["event"] for e in self.traces if e["event"] in ("processed", "delivered_late")],
                         ["processed", "delivered_late"] * 3)
        self.assertGreater(self.events("delivered_late")[0]["queue_age_s"], 0)

    def test_the_engine_going_away_again_stops_the_pass_and_keeps_the_rest(self):
        q = self.queue()
        delivered = []
        q.register_handler("patrol", lambda item, text, frame: delivered.append(item["key"]) or self.up.__setitem__(0, False))
        for i in range(3):
            q.enqueue("patrol", "cam", "k", jpg(), "p", key="k%d" % i)
        q.process()
        self.assertEqual(delivered, ["k0"])                                # the lease came back after the first: two stay queued
        self.assertEqual(q.depth(), 2)

    def test_items_older_than_twelve_hours_are_dropped_with_a_trace_record(self):
        q = self.queue(available=False)
        q.enqueue("patrol", "cam", "k", jpg(), "old", key="old")
        self.clock.advance(11 * HOUR)
        q.enqueue("patrol", "cam", "k", jpg(), "fresh", key="fresh")
        self.clock.advance(1.5 * HOUR)                                      # "old" is now 12.5 h, "fresh" 1.5 h
        self.up[0] = True
        got = []
        q.register_handler("patrol", lambda item, text, frame: got.append(item["key"]))
        q.process()
        self.assertEqual(got, ["fresh"])
        drop = self.events("dropped_age")
        self.assertEqual(len(drop), 1)
        self.assertGreater(drop[0]["queue_age_s"], 12 * HOUR)
        self.assertEqual(self.calls[0][1], "fresh")                         # the expired frame never reached the model

    def test_the_age_limit_is_configurable(self):
        q = self.queue({"max_age_h": 1}, available=False)
        q.enqueue("patrol", "cam", "k", jpg(), "p")
        self.clock.advance(1.2 * HOUR)
        self.assertEqual(q.expire(), 1)
        self.assertEqual(q.depth(), 0)

    def test_size_caps_drop_the_oldest_with_a_trace_record(self):
        q = self.queue({"max_items": 3, "max_per_camera": 2}, available=False)
        for i in range(3):
            q.enqueue("patrol", "kitchen", "k", jpg(), "p%d" % i, key="k%d" % i)
            self.clock.advance(1)
        self.assertEqual(q.depth(), 2)                                      # per-camera cap: p0 went
        self.assertEqual(self.events("dropped_cap")[0]["source"], "patrol")
        for i in range(4):
            q.enqueue("sentry", "cam%d" % i, "k", jpg(), "s%d" % i)
            self.clock.advance(1)
        self.assertEqual(q.depth(), 3)                                      # item cap
        prompts = sorted(i["prompt"] for i in q.items.values())
        self.assertEqual(prompts, ["s1", "s2", "s3"])                       # the newest three of the lot
        q2 = self.queue({"max_mb": 0.0001}, available=False)               # ~100 bytes: every frame is over the byte cap
        q2.enqueue("patrol", "cam", "k", jpg(size=(300, 300)), "p")
        self.assertEqual(q2.depth(), 0)

    def test_a_frame_the_engine_keeps_failing_on_is_dropped_after_its_tries(self):
        q = self.queue({"max_tries": 2})
        q.register_handler("patrol", lambda *a: None)
        q.enqueue("patrol", "cam", "k", jpg(), "bad")
        q.enqueue("patrol", "cam", "k", jpg(), "good")
        self.failures[0] = 2
        q.process()                                                         # try 1: stop
        self.assertEqual(q.depth(), 2)
        q.process()                                                         # try 2: dropped
        self.assertEqual(self.events("failed")[0]["triggers"], ["vision_queue_failed"])
        self.assertEqual(q.depth(), 1)
        q.process()
        self.assertEqual([c[1] for c in self.calls], ["good"])

    def test_a_source_that_pulls_gets_results_until_it_acknowledges(self):
        q = self.queue()
        q.enqueue("sentry", "Back Yard", "sentry_frame", jpg(7), "describe", key="x", at=T0 - 600)
        q.process()
        rows = q.results("sentry")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["camera"], rows[0]["text"], rows[0]["at"]), ("Back Yard", "read:describe", T0 - 600))
        self.assertEqual(base64.b64decode(rows[0]["frame_b64"]), jpg(7))
        self.assertEqual(q.depth(), 0)                                      # read, but not yet acknowledged
        self.assertEqual(len(q.results("sentry")), 1)
        self.assertEqual(q.ack("sentry", [rows[0]["id"]]), 1)
        self.assertEqual(q.results("sentry"), [])
        self.assertEqual(len(self.events("delivered_late")), 1)
        self.assertEqual(q.ack("sentry", ["nope"]), 0)

    def test_unacknowledged_results_expire_too(self):
        q = self.queue()
        q.enqueue("sentry", "cam", "k", jpg(), "p")
        q.process()
        self.clock.advance(13 * HOUR)
        self.assertEqual(q.results("sentry"), [])
        self.assertEqual(len(self.events("dropped_age")), 1)

    def test_the_queue_survives_a_restart_and_a_dedupe_key_is_kept_once(self):
        q = self.queue(available=False)
        q.enqueue("box", "", "box", jpg(), "p", key="card1", dedupe=True)
        self.assertTrue(q.enqueue("box", "", "box", jpg(), "p", key="card1", dedupe=True)["duplicate"])
        self.assertEqual(q.depth(), 1)
        again = VisionQueue(lambda: self.config, lambda *a: "x", lambda: True, data_dir=self.dir, clock=self.clock)
        self.assertEqual(again.depth(), 1)

    def test_switched_off_and_junk_frames_are_refused(self):
        q = self.queue({"enabled": False})
        self.assertFalse(q.enqueue("patrol", "c", "k", jpg(), "p")["ok"])
        q = self.queue()
        self.assertFalse(q.enqueue("patrol", "c", "k", b"", "p")["ok"])

    def test_metrics_say_depth_oldest_age_and_counters(self):
        q = self.queue(available=False)
        q.enqueue("patrol", "c", "k", jpg(), "p")
        self.clock.advance(90)
        text = "\n".join(q.metrics())
        self.assertIn('stonesage_vision_queue_depth{source="patrol"} 1', text)
        self.assertIn("stonesage_vision_queue_oldest_age_seconds 90", text)
        self.assertIn('stonesage_vision_queue_events_total{event="queued",source="patrol"} 1', text)

    def test_kick_reads_what_waited_without_waiting_for_the_poll(self):
        q = self.queue()
        got = []
        q.register_handler("patrol", lambda item, text, frame: got.append(1))
        q.enqueue("patrol", "c", "k", jpg(), "p")
        q.kick()
        for _ in range(300):
            if got:
                break
            time.sleep(0.01)
        self.assertEqual(got, [1])


# ---- sources: Frigate first, then the late read -------------------------------------------------------------------------------
class TestPatrolWhileLent(unittest.TestCase):
    def make(self, lent=True):
        self.clock = Clock()
        self.up = [not lent]
        self.config = {}
        self.got = []
        q = VisionQueue(lambda: self.config, lambda f, p, m, t: json.dumps({"seen": ["Rex"], "note": "dog"}), lambda: self.up[0],
                        data_dir=None, clock=self.clock, sleep=self.clock.sleep)
        deferrer = vd.Deferrer(lambda: q, lambda role: 1.0 if not self.up[0] else None)
        self.q, self.deferrer = q, deferrer
        cam = tp.FakeCamera()
        cfg = {"patrol": {"enabled": True, "step_deg": 30, "max_frames": 3, "settle_s": 0, "residents": ["Alex"],
                          "cameras": {"camera.kitchen": tp.KITCHEN}}}
        presence = {"locations": {}, "known_entities": {"people": [{"name": "Alex"}], "pets": [{"name": "Rex"}]}}
        holder = {}

        def vision(frame, where, profiles):
            if not self.up[0]:
                return deferrer.patrol(frame, "Which of them? " + where, holder["p"].context())
            return {"seen": ["Rex"]}
        p = pt.Patrol(lambda: cfg, cam, cam.frame, vision, lambda c: [{"label": "dog", "name": "rex"}], lambda: presence,
                      clock=lambda: 1000.0, sleep=lambda s: None)
        holder["p"] = p
        q.register_handler("patrol", p.deliver_late)
        return p, cam

    def test_frigate_names_the_pet_straight_away_and_the_frame_is_queued(self):
        p, _ = self.make()
        traces = []
        p.on_sweep = traces.append
        res = p.sweep("camera.kitchen", tp.KITCHEN)
        self.assertEqual(set(p.locations()), {"rex"})                      # from Frigate's identity, no vision model involved
        self.assertEqual(self.q.depth(), res["frames"])                    # every frame is waiting for a closer read
        vision = traces[0]["vision"]
        self.assertEqual(vision["deferred"], res["frames"])
        self.assertEqual(vision["errors"], 0)                              # lent out is not an error
        self.assertEqual(self.q.status()["by_source"], {"patrol": res["frames"]})

    def test_a_late_read_adds_a_sighting_dated_when_the_frame_was_taken_and_never_replaces_a_newer_one(self):
        p, _ = self.make()
        p.sweep("camera.kitchen", tp.KITCHEN)
        # Alex was only seen by the late read, an hour after the frame: the sighting is dated to the frame (clock 1000)
        item = {"at": 700.0, "meta": {"entity": "camera.kitchen", "pan": 60, "profiles": ["Alex", "Rex"]}}
        self.assertTrue(p.deliver_late(item, '{"seen": ["Alex"], "note": "x"}', jpg()))
        loc = p.locations()
        self.assertIn("alex", loc)
        self.assertIn("read late", loc["alex"]["doing"])
        self.assertTrue(p.frame("camera.kitchen", int(loc["alex"]["patrol_ref"].split("|")[1])))
        newer = p.sightings["rex"]["seen_at"]
        p.deliver_late({"at": 100.0, "meta": item["meta"]}, '{"seen": ["Rex"]}', jpg())
        self.assertEqual(p.sightings["rex"]["seen_at"], newer)             # the older late read did not take Rex's place
        self.assertFalse(p.deliver_late(item, '{"seen": ["Nobody"]}', jpg()))   # a name that is not a profile is dropped

    def test_the_queue_hands_late_reads_to_patrol_after_the_engine_is_back(self):
        p, _ = self.make()
        p.sweep("camera.kitchen", tp.KITCHEN)
        p.sightings.clear()
        self.up[0] = True
        self.q.process()
        self.assertIn("rex", p.locations())                                # the late read brought Rex back, marked late
        self.assertIn("read late", p.locations()["rex"]["doing"])

    def test_with_the_engine_there_nothing_is_queued(self):
        p, _ = self.make(lent=False)
        p.sweep("camera.kitchen", tp.KITCHEN)
        self.assertEqual(self.q.depth(), 0)

    def test_sweep_now_runs_the_hot_load_first_on_the_sweeps_own_thread(self):
        p, _ = self.make(lent=False)
        order = []
        p.before_manual = lambda: order.append("hot-load")
        p._sweep = lambda e, c, pr=None, reason="scheduled": order.append(("sweep", reason))
        self.assertTrue(p.run_now("camera.kitchen")["ok"])
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(1)
        self.assertEqual(order, ["hot-load", ("sweep", "manual")])

    def test_a_hot_load_that_fails_does_not_stop_the_sweep(self):
        p, _ = self.make(lent=False)
        order = []

        def boom():
            raise RuntimeError("lease gone")
        p.before_manual = boom
        p._sweep = lambda e, c, pr=None, reason="scheduled": order.append(reason)
        p.run_now("camera.kitchen")
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(1)
        self.assertEqual(order, ["manual"])


class TestCommentaryAndBoxesWhileLent(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.q = VisionQueue(lambda: {}, lambda f, p, m, t: "x", lambda: False, clock=self.clock)
        self.deferrer = vd.Deferrer(lambda: self.q, lambda role: 1.0)

    def commentary(self):
        self.spoken, self.traces = [], []
        return Commentary(lambda: {"commentary": {"enabled": True}}, lambda e: None, lambda c: [], lambda c: None,
                          lambda j, p: "", lambda m: "", lambda t, r: self.spoken.append(t) or {"ok": True},
                          trace=self.traces.append, clock=self.clock)

    def test_a_commentary_read_is_queued_and_raises_as_before(self):
        with self.assertRaises(RuntimeError) as cm:
            self.deferrer.commentary(jpg(), "what are they doing", {"camera": "kitchen", "room": "kitchen", "who": ["Alex"]})
        self.assertIn("queued", str(cm.exception))
        self.assertEqual(self.q.status()["by_source"], {"commentary": 1})

    def test_a_stale_late_commentary_read_is_skipped_and_a_fresh_one_is_logged_never_spoken(self):
        c = self.commentary()
        stale = {"at": T0 - 3600, "camera": "kitchen", "meta": {"room": "kitchen", "who": ["Alex"]}}
        rec = c.deliver_late(stale, '{"activity": "cooking", "evidence": "pot on the hob"}')
        self.assertEqual((rec["outcome"], rec["late"]), ("held", True))
        self.assertIn("stale", rec["held"])
        fresh = {"at": T0 - 120, "camera": "kitchen", "meta": {"room": "kitchen", "who": ["Alex"]}}
        rec = c.deliver_late(fresh, '{"activity": "cooking", "evidence": "pot on the hob"}')
        self.assertEqual((rec["outcome"], rec["activity"]), ("logged", "cooking"))
        self.assertEqual(c.reads["kitchen"][-1][1], "cooking")
        self.assertEqual(self.spoken, [])                                  # the moment has gone: a late read never speaks
        self.assertEqual([t["outcome"] for t in self.traces], ["held", "logged"])

    def test_the_stale_limit_is_configurable(self):
        c = self.commentary()
        c.cfg_fn = lambda: {"commentary": {"late_max_age_s": 5000}}
        rec = c.deliver_late({"at": T0 - 3600, "camera": "kitchen", "meta": {}}, '{"activity": "cleaning", "evidence": "wiping the counter"}')
        self.assertEqual(rec["outcome"], "logged")

    def test_a_box_lookup_is_queued_once_per_card_and_lands_in_the_cache(self):
        self.assertTrue(self.deferrer.box("sentry|a.jpg|rex", jpg(), "Locate the dog", [640, 360]))
        self.deferrer.box("sentry|a.jpg|rex", jpg(), "Locate the dog", [640, 360])
        self.assertEqual(self.q.depth(), 1)
        cache = {}
        vd.register_handlers(self.q, lambda: None, lambda: None, lambda key, box: cache.__setitem__(key, box),
                             __import__("hearth.sighting_boxes", fromlist=["x"]).parse_box)
        item = next(iter(self.q.items.values()))
        self.q.handlers["box"](item, "[64, 36, 320, 180]", b"")
        self.assertEqual(cache["sentry|a.jpg|rex"], [0.1, 0.1, 0.4, 0.4])


# ---- authentication of the sentry's route ---------------------------------------------------------------------------------------
class H:
    def __init__(self, bearer):
        self.headers = {"Authorization": "Bearer " + bearer}
        self.client_address = ("192.0.2.10", 5555)


class TestSentryAuth(unittest.TestCase):
    def setUp(self):
        auth.register_scoped_key(vd.SENTRY_SCOPE, vd.SENTRY_ROUTES, lambda cfg: str(((cfg or {}).get("vision_queue") or {}).get("key") or ""))
        self.addCleanup(auth.unregister_scoped_key, vd.SENTRY_SCOPE)
        self.cfg = {"api": {"key": "owner-key"}, "vision_queue": {"key": "sentry-key"}}
        self._thr = auth.THROTTLE
        auth.THROTTLE = auth.Throttle()

    def tearDown(self):
        auth.THROTTLE = self._thr

    def who(self, bearer, method, path):
        return auth.authenticate(H(bearer), self.cfg, method, path)

    def test_the_scoped_key_opens_exactly_its_three_routes(self):
        for method, path in vd.SENTRY_ROUTES:
            self.assertEqual(self.who("sentry-key", method, path)[0], vd.SENTRY_SCOPE)
        self.assertIsNone(self.who("sentry-key", "GET", "/api/vision-queue/status")[0])        # the status page is the owner's
        self.assertIsNone(self.who("sentry-key", "POST", "/api/chat")[0])
        self.assertIsNone(self.who("sentry-key", "GET", "/api/vision-queue/frame")[0])         # right path, wrong method

    def test_a_wrong_key_or_a_key_equal_to_the_owners_opens_nothing(self):
        self.assertIsNone(self.who("guess", "POST", "/api/vision-queue/frame")[0])
        self.cfg["vision_queue"]["key"] = "owner-key"
        self.assertEqual(auth.scoped_key_value(self.cfg, vd.SENTRY_SCOPE), "")


class TestSentryFrameValidation(unittest.TestCase):
    def setUp(self):
        self.q = VisionQueue(lambda: {}, lambda *a: "x", lambda: False, clock=Clock())

    def post(self, **over):
        body = {"camera": "Back Yard", "prompt": "describe", "frame_b64": base64.b64encode(jpg()).decode(), "max_tokens": 180}
        body.update(over)
        return vd.accept_sentry_frame(self.q, body)

    def test_a_good_frame_is_queued(self):
        res = self.post()
        self.assertTrue(res["ok"])
        self.assertEqual(self.q.status()["by_source"], {"sentry": 1})

    def test_bad_input_is_refused_with_a_400(self):
        for over in ({"camera": ""}, {"prompt": ""}, {"prompt": "x" * 7000}, {"frame_b64": "!!notbase64"},
                     {"frame_b64": base64.b64encode(b"GIF89a....").decode()}, {"at": "yesterday"}):
            res = self.post(**over)
            self.assertEqual((res["ok"], res["status"]), (False, 400), over)
        self.assertEqual(self.q.depth(), 0)


# ---- the takeback: per card, hot-load ----------------------------------------------------------------------------------------------
class VisionLease(FakeLeaseSvc):
    def vision_ready(self):
        return self.ready()


class HotRig(unittest.TestCase):
    def build(self, lite=(), full=(), roles=("vision",), cfg=None, load_s=6.0, mode="lite"):
        self.clock = TakebackClock()
        self.svc = VisionLease(self.clock, load_s, mode, True, roles=roles)
        self.svc.lease["layout"] = "longctx_6600" if "vision" in roles else "longctx_6750"
        self.traces, self.pushes, self.yielded, self.backs = [], [], [], []
        self.config = {"courage": {"takeback": dict(cfg or {})}}
        self.tb = Takeback(lambda: self.config, lambda: self.svc, self.svc.ready, trace=self.traces.append,
                           push=lambda t, m: self.pushes.append((t, m)), clock=self.clock, sleep=self.clock.sleep,
                           vision_ready=self.svc.vision_ready, on_vision_back=lambda: self.backs.append(1))
        self.tb.register_yielder(lambda why, cooldown, resource="coordinator": self.yielded.append((why, cooldown, resource)) or {"ok": True})
        self.lite_llm, self.full_llm = ScriptedLLM(*lite), ScriptedLLM(*full)
        self.urls, self.looks = [], []
        svc = self.svc

        class Looks(list):
            def append(_s, eid):
                list.append(_s, (eid, len(svc.released), svc.vision_ready()))

        def post(url, body, timeout):
            self.urls.append(url)
            llm = self.lite_llm if url == LITE_URL else self.full_llm
            r = llm(url, body, timeout)
            if isinstance(r, Exception):
                raise r
            return r
        self.looks = Looks()
        self.agent, _ = make_agent(post, looks=self.looks)
        self.agent.brain = self.svc.brain
        self.agent.takeback = self.tb
        return self

    def ask(self, text="what is on the living room camera?"):
        return list(self.agent.run([{"role": "user", "content": text}], "web-1"))

    def trace(self):
        return [t for t in self.traces if t.get("kind") == "takeback"]

    def types(self, events):
        return [e["type"] for e in events]


class TestHotLoad(HotRig):
    def test_a_camera_question_while_the_garden_holds_the_6600_says_one_moment_then_looks_on_the_real_engine(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("A cat on the couch.")])
        events = self.ask()
        t = self.types(events)
        self.assertLess(t.index("holding"), t.index("tool_call"))
        self.assertEqual(events[t.index("holding")]["content"], FILLER)
        self.assertEqual(events[-1]["content"], "A cat on the couch.")
        self.assertEqual(len(self.looks), 1)                                # the look ran once...
        eid, released, ready = self.looks[0]
        self.assertEqual((released, ready), (1, True))                      # ...after the lease was released and vision answered
        self.assertEqual(self.yielded, [("takeback: tool", 600.0, "vision")])   # the garden session stops with its reason (draft saved)
        self.assertEqual(self.svc.released[0][1:], ("takeback: tool", "takeback"))
        rec = self.trace()[0]
        self.assertEqual((rec["card"], rec["resource"], rec["why"], rec["tool"], rec["outcome"]), ("vision", "vision", "tool", "camera_look", "ok"))
        self.assertAlmostEqual(rec["user_wait_ms"], 6000, delta=2500)       # the engine's restart, not the coordinator's
        self.assertEqual(self.backs, [1])                                   # the vision queue is told it can start reading
        self.assertEqual(set(self.urls), {FULL_URL})                    # the coordinator was never lent: the turn stayed on it

    def test_camera_scan_too_and_the_waiting_user_hears_nothing_else(self):
        self.build(full=[tool_call("camera_scan", {"camera": "living_room"}), reply("Nobody in the room.")])
        events = self.ask("scan the living room")
        self.assertEqual(self.types(events).count("holding"), 1)
        self.assertEqual(len(self.svc.released), 1)

    def test_the_wait_is_bounded_and_the_tool_still_runs(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("Frigate says a cat.")], load_s=500)
        events = self.ask()
        self.assertEqual(events[-1]["content"], "Frigate says a cat.")
        rec = self.trace()[0]
        self.assertEqual((rec["outcome"], rec["triggers"]), ("takeback_failed", ["takeback_failed"]))
        self.assertAlmostEqual(rec["waited_ms"], 60000, delta=2000)         # vision_wait_s: 60
        self.assertEqual(len(self.looks), 1)                                # the old fallbacks (Frigate) take it from here
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("camera engine", self.pushes[0][0])
        self.assertEqual(self.backs, [])

    def test_the_wait_bound_is_configurable(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")], load_s=500, cfg={"vision_wait_s": 12})
        self.ask()
        self.assertAlmostEqual(self.trace()[0]["waited_ms"], 12000, delta=1500)

    def test_the_garden_may_not_borrow_the_6600_again_for_ten_minutes_and_the_coordinator_is_untouched(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")])
        self.ask()
        self.assertAlmostEqual(self.tb.cooldown_left("vision"), 600 - 6, delta=10)
        self.assertEqual(self.tb.cooldown_left("coordinator"), 0)           # a vision hot-load does not cool the 6750 layouts down
        self.clock.t += 601
        self.assertEqual(self.tb.cooldown_left("vision"), 0)
        self.assertEqual(self.yielded[0][1], 600.0)                         # the yielder (the garden scheduler) was told the same
        st = self.tb.status()
        self.assertEqual(st["cooldowns_left_s"]["coordinator"], 0)

    def test_the_cooldown_follows_the_config(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")], cfg={"vision_cooldown_min": 3})
        self.ask()
        self.assertEqual(self.yielded[0][1], 180.0)
        self.assertAlmostEqual(self.tb.cooldown_left("vision"), 180 - 6, delta=10)

    def test_the_cooldown_survives_a_restart(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")])
        path = os.path.join(tempfile.mkdtemp(), "takeback.json")
        self.tb.path = path
        self.ask()
        again = Takeback(lambda: self.config, lambda: None, lambda: True, path=path, clock=self.clock)
        self.assertGreater(again.cooldown_left("vision"), 500)
        self.assertEqual(again.cooldown_left("coordinator"), 0)

    def test_a_second_camera_look_in_the_same_turn_does_not_hot_load_again(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}, "a"), tool_call("camera_look", {"camera": "living_room"}, "b"),
                         reply("ok")])
        events = self.ask()
        self.assertEqual(self.types(events).count("holding"), 1)
        self.assertEqual(len(self.svc.released), 1)

    def test_a_lease_that_is_not_background_work_is_left_alone(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("Vision is lent out.")])
        self.svc.lease["yield_to_computer"] = False
        events = self.ask()
        self.assertEqual((self.svc.released, self.yielded, self.trace()), ([], [], []))
        self.assertNotIn("holding", self.types(events))

    def test_switched_off_means_no_hot_load(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")], cfg={"enabled": False})
        self.ask()
        self.assertEqual(self.svc.released, [])

    def test_the_camera_tools_list_is_configurable(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")], cfg={"vision_tools": ["camera_scan"]})
        self.ask()
        self.assertEqual(self.svc.released, [])

    def test_hot_load_with_nothing_lent_is_a_no_op(self):
        self.build()
        self.svc.lease = None
        res = self.tb.hot_load(why="sweep", source="sweep")
        self.assertEqual((res["ok"], res["noop"]), (True, True))
        self.assertEqual((self.trace(), self.yielded, self.tb.cooldown_left("vision")), ([], [], 0))

    def test_the_sweep_button_hot_loads_the_same_way(self):
        self.build()
        res = self.tb.hot_load(why="sweep", source="sweep")
        self.assertTrue(res["ok"])
        rec = self.trace()[0]
        self.assertEqual((rec["why"], rec["source"], rec["card"]), ("sweep", "sweep", "vision"))
        self.assertEqual(self.yielded[0], ("takeback: sweep", 600.0, "vision"))

    def test_metrics_carry_the_card_and_a_hot_load_histogram(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")])
        self.ask()
        text = "\n".join(self.tb.metrics())
        self.assertIn('stonesage_takeback_total{card="vision",why="tool",outcome="ok"} 1', text)
        self.assertIn('stonesage_hotload_wait_seconds_count{card="vision"} 1', text)
        self.assertIn('stonesage_hotload_wait_seconds_bucket{card="vision",le="10"} 1', text)
        self.assertIn('stonesage_hotload_wait_seconds_bucket{card="vision",le="5"} 0', text)
        self.assertIn('stonesage_takeback_cooldown_seconds{card="vision"} ', text)
        self.assertIn("stonesage_takeback_user_wait_seconds_count 0", text)  # the coordinator's histogram is separate


class TestPerCardTakeback(HotRig):
    def test_a_camera_question_while_the_garden_holds_the_6750_does_not_disturb_it(self):
        self.build(lite=[tool_call("camera_look", {"camera": "living_room"}), reply("A cat.")], roles=("coordinator", "worker"))
        events = self.ask()
        self.assertEqual(len(self.looks), 1)
        self.assertEqual((self.svc.released, self.yielded, self.trace()), ([], [], []))
        self.assertNotIn("holding", self.types(events))
        self.assertEqual(set(self.urls), {LITE_URL})
        self.assertEqual((self.tb.cooldown_left("coordinator"), self.tb.cooldown_left("vision")), (0, 0))

    def test_a_camera_question_while_the_garden_holds_the_6600_takes_the_6600(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("A cat.")], roles=("vision",))
        self.ask()
        self.assertEqual([y[2] for y in self.yielded], ["vision"])
        self.assertEqual(len(self.svc.released), 1)

    def test_think_harder_takes_the_coordinator_card_not_the_vision_card(self):
        self.build(lite=[tool_call("think_harder", {"question": "why"})], full=[reply("Because.")], roles=("coordinator", "worker"))
        self.ask("why is it so")
        self.assertEqual([y[2] for y in self.yielded], ["coordinator"])
        self.assertEqual(self.trace()[0]["card"], "coordinator")
        self.assertGreater(self.tb.cooldown_left("coordinator"), 3000)
        self.assertEqual(self.tb.cooldown_left("vision"), 0)

    def test_think_harder_while_the_garden_holds_only_the_6600_is_a_plain_turn(self):
        self.build(full=[tool_call("think_harder", {"question": "why"}), reply("Because.")], roles=("vision",))
        events = self.ask("why is it so")
        self.assertEqual((self.svc.released, self.yielded), ([], []))
        self.assertNotIn("holding", self.types(events))

    def test_a_cockpit_beat_gives_back_a_borrowed_coordinator_but_not_the_6600(self):
        self.build(roles=("vision",))
        self.tb.on_cockpit_activity("heartbeat")
        time.sleep(0.1)
        self.assertEqual((self.svc.released, self.trace()), ([], []))
        self.build(roles=("coordinator", "worker"))
        self.tb.on_cockpit_activity("heartbeat")
        for _ in range(500):
            if self.trace():
                break
            time.sleep(0.01)
        self.assertEqual(self.trace()[0]["card"], "coordinator")

    def test_a_lease_holding_both_cards_cools_both_down(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")], roles=("coordinator", "worker", "vision"))
        self.svc.mode = "on_loan"
        self.ask()
        self.assertGreater(self.tb.cooldown_left("coordinator"), 3000)
        self.assertGreater(self.tb.cooldown_left("vision"), 400)

    def test_an_older_yielder_with_two_parameters_still_works(self):
        self.build(full=[tool_call("camera_look", {"camera": "living_room"}), reply("ok")])
        old = []
        self.tb.yielders[:] = [lambda why, cooldown: old.append((why, cooldown)) or {"ok": True}]
        self.ask()
        self.assertEqual(old, [("takeback: tool", 600.0)])


# ---- the real lease, end to end -------------------------------------------------------------------------------------------
class TestWithTheRealLease(unittest.TestCase):
    def test_a_camera_question_hot_loads_the_6600_through_the_real_lease(self):
        vm = MiniVM()
        vm.running = {"coord", "worker", "vision"}
        base_start = vm.systemctl

        def systemctl(verb, unit):
            if verb == "start" and unit == "layout":
                vm.running -= {"vision"}
            elif verb == "start" and unit == "vision":
                vm.running.discard("layout")
            return base_start(verb, unit)
        vm.systemctl = systemctl
        vm.http_ok = lambda url: {"8004": "vision", "8005": "layout"}.get(url.split(":")[2].split("/")[0]) in vm.running
        from lodestone import engine_lease as el
        cfg = {"engine_layouts": {"garden": {"unit": "layout", "evicts": ["vision"], "url": "http://vm:8005", "when": "any"}}}
        roles = {"vision": {"role": "vision", "gpu": "G2", "url": "http://vm:8004"}}
        now = [T0]
        lease = el.EngineLease(lambda: cfg, vm.systemctl, vm.http_ok, lambda: roles, lambda: {"gpus": {}}, lambda r: None,
                               clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertTrue(lease.acquire("garden", 3600, "garden", "garden-scheduler", yield_to_computer=True)["ok"])
        self.assertIsNotNone(lease.on_loan("vision"))
        clock = TakebackClock()
        stopped = []
        tb = Takeback(lambda: {}, lambda: lease, lambda: True, clock=clock, sleep=clock.sleep,
                      vision_ready=lambda: lease.on_loan("vision") is None and vm.http_ok("http://vm:8004/health"))
        tb.register_yielder(lambda why, cd, resource="coordinator": stopped.append((why, resource)))
        looks = []
        llm = ScriptedLLM(tool_call("camera_look", {"camera": "living_room"}), reply("A cat."))

        class Looks(list):
            def append(_s, eid):
                list.append(_s, (eid, lease.on_loan("vision"), "vision" in vm.running))
        agent, _ = make_agent(lambda url, body, timeout: llm(url, body, timeout), looks=Looks())
        agent.takeback = tb
        events = list(agent.run([{"role": "user", "content": "look"}], "s"))
        self.assertEqual(events[-1]["content"], "A cat.")
        self.assertEqual(stopped, [("takeback: tool", "vision")])
        self.assertIsNone(lease.lease)
        self.assertIn("vision", vm.running)
        self.assertGreater(tb.cooldown_left("vision"), 500)


if __name__ == "__main__":
    unittest.main()
