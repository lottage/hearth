"""Offline tests for the PTZ patrol: schedule rules, who counts as home, a full simulated sweep that finds the end
stop, and the vision JSON parsing. The camera is a fake whose picture depends on its pan position."""

import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from PIL import Image  # noqa: E402

from hearth import patrol as pt  # noqa: E402

KITCHEN = {"name": "Kitchen", "ptz": "kitchen", "frames": "frigate:kitchen", "home_preset": "Living Room",
           "quiet_when_home": True}
DRIVE = {"name": "Driveway", "ptz": "drive", "frames": "go2rtc:drive", "home_preset": "Doors",
         "battery_sensor": "sensor.drive_battery"}


class TestRules(unittest.TestCase):
    def test_indoor_camera_pauses_while_a_resident_is_home(self):
        self.assertIsNone(pt.interval_for(KITCHEN, 15, None, True, ["Alex"]))
        self.assertEqual(pt.interval_for(KITCHEN, 15, None, False, []), 15)

    def test_solar_camera_battery_gate(self):
        self.assertEqual(pt.interval_for(DRIVE, 15, 80, True, []), 15)    # daylight, healthy
        self.assertEqual(pt.interval_for(DRIVE, 15, 80, False, []), 60)   # night
        self.assertEqual(pt.interval_for(DRIVE, 15, 45, True, []), 60)    # daylight but below 60 %
        self.assertIsNone(pt.interval_for(DRIVE, 15, 25, True, []))       # below 30 %
        self.assertIsNone(pt.interval_for(DRIVE, 15, None, True, []))     # battery unknown

    def test_residents_home_prefers_ha_and_falls_back_to_cameras(self):
        ha = {"Alex": "not_home", "Sam": None}.get
        self.assertEqual(pt.residents_home(["Alex", "Sam"], ha, {"alex": 1.0, "sam": 30.0}), ["Sam"])
        self.assertEqual(pt.residents_home(["Alex", "Sam"], ha, {"sam": 400.0}), [])
        self.assertEqual(pt.residents_home(["Alex"], {"Alex": "home"}.get, {}), ["Alex"])

    def test_vision_json_parsing(self):
        self.assertEqual(pt.parse_vision_json('```json\n{"seen": ["Rex"], "note": "dog on rug"}\n```')["seen"], ["Rex"])
        self.assertEqual(pt.parse_vision_json("I see a dog")["seen"], [])


def jpeg(shade):
    buf = io.BytesIO()
    Image.new("L", (160, 90), shade).save(buf, format="JPEG")
    return buf.getvalue()


class FakeCamera:
    """Pan range 0..240 deg in 30 deg steps; the picture's brightness encodes the position."""
    def __init__(self):
        self.pos, self.angle, self.calls = 120, 45, []     # the owner set a 45 deg step in HA
        self.on_press = None
        self.presets = {}                                    # temporary presets saved by the patrol
        self.save_ok = True

    def press_button(self, eid):
        if self.on_press:
            self.on_press(eid)
        self.calls.append(eid)
        step = self.angle if eid.endswith("left") is False else -self.angle
        self.pos = max(0, min(240, self.pos + step))
        return {"ok": True}

    def call_service(self, domain, service, data, timeout=4):
        if data.get("entity_id", "").endswith("movement_angle"):
            self.angle = data["value"]
        if service == "save_preset":
            if not self.save_ok:
                return {"ok": False, "error": "camera busy"}
            self.presets[data["name"]] = self.pos
        if service == "delete_preset":
            self.presets.pop(data["preset"], None)
        return {"ok": True}

    def select_option(self, eid, option, timeout=4):
        self.calls.append(f"{eid}={option}")
        if option in self.presets:
            self.pos = self.presets[option]
        else:
            self.pos = 120                                   # the home preset
        return {"ok": True}

    def get_state(self, eid):
        return {"sensor.drive_battery": {"state": "90"}, "sun.sun": {"state": "above_horizon"},
                "number.kitchen_movement_angle": {"state": str(self.angle)}}.get(eid)

    def frame(self, _source):
        return jpeg(20 + self.pos)                 # 0..240 deg -> shade 20..260 (clamped by PIL at 255)


class TestSweep(unittest.TestCase):
    def make(self, vision_seen=("Rex",), in_view=True):
        cam = FakeCamera()
        cfg = {"patrol": {"enabled": True, "step_deg": 30, "max_frames": 9, "settle_s": 0, "residents": ["Alex"],
                          "cameras": {"camera.kitchen": KITCHEN}}}
        presence = {"locations": {}, "known_entities": {"people": [{"name": "Alex"}], "pets": [{"name": "Rex"}]}}
        self.presence_reads = 0

        def presence_fn():
            self.presence_reads += 1
            return presence
        p = pt.Patrol(lambda: cfg, cam, cam.frame, lambda f, w, prof: {"seen": list(vision_seen) + ["Nobody"]},
                      lambda c: [{"label": "dog"}] if in_view else [], presence_fn, clock=lambda: 1000.0,
                      sleep=lambda s: None)
        return p, cam

    def test_sweep_starts_at_left_end_stops_at_right_end_and_returns_home(self):
        p, cam = self.make()
        res = p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(res["frames"], 8)                          # 30..240 deg (0 deg = wall is skipped), then the end stop
        self.assertEqual(cam.calls[:4], ["button.kitchen_move_left"] * 3 + ["button.kitchen_move_right"])
        self.assertEqual(p.status()["cameras"]["camera.kitchen"]["first_pan"], 30)
        self.assertEqual(cam.calls[-1], f"select.kitchen_move_to_preset={pt.RETURN_PRESET}")
        self.assertEqual((cam.pos, cam.presets), (120, {}))     # back where it was; temporary preset deleted
        self.assertEqual(cam.angle, 45)                             # the owner's step size restored, not a default
        self.assertEqual(set(p.locations()), {"rex"})              # unknown names from the VLM are dropped
        self.assertTrue(p.frame("camera.kitchen", 0))

    def test_last_sweep_survives_a_restart_and_next_is_set(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "patrol_state.json")
        p, cam = self.make()
        p.state_path = path
        p.state["camera.kitchen"] = {"interval_s": 900}
        p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(p.status()["cameras"]["camera.kitchen"]["next"], 1000.0 + 900)
        p2 = pt.Patrol(p.cfg_fn, cam, cam.frame, p.vision_fn, p.frigate_in_view, p.presence_fn,
                       clock=lambda: 1100.0, sleep=lambda s: None, state_path=path)
        self.assertEqual(p2.state["camera.kitchen"]["last"], 1000.0)
        self.assertEqual(p2.due("camera.kitchen", KITCHEN), "waiting")   # 100 s after the sweep, not due again

    def test_corrections_on_patrol_sightings(self):
        p, _ = self.make()
        p.sweep("camera.kitchen", KITCHEN)
        ref = p.locations()["rex"]["patrol_ref"]
        self.assertFalse(p.correct(ref, "Bella", "reject")["ok"])            # not what that card showed
        res = p.correct(ref, "Rex", "relabel", "Aunt May")
        self.assertTrue(res["ok"] and res["frame"])                         # frame handed back for face training
        self.assertEqual(set(p.locations()), {"aunt-may"})
        self.assertTrue(p.correct(p.locations()["aunt-may"]["patrol_ref"], "Aunt May", "reject")["ok"])
        self.assertEqual(p.locations(), {})
        self.assertFalse(p.correct("garbage", "Rex", "reject")["ok"])

    def test_frigate_camera_skips_vision_when_nobody_is_in_view(self):
        p, _ = self.make(in_view=False)
        p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(p.locations(), {})

    def test_hand_moved_camera_is_left_alone_and_home_resident_pauses(self):
        p, _ = self.make()
        p.note_manual("camera.kitchen")
        self.assertEqual(p.due("camera.kitchen", KITCHEN), "moved by hand recently")
        p.manual_at.clear()
        p.presence_fn = lambda: {"locations": {"alex": {"minutes_ago": 5}}}
        self.assertEqual(p.due("camera.kitchen", KITCHEN), "paused: Alex home")

    def test_sweep_returns_to_where_the_owner_left_it_and_falls_back_home(self):
        p, cam = self.make()
        cam.pos = 200                                         # the owner pointed it at the sofa
        p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(cam.pos, 200)
        self.assertEqual(cam.presets, {})
        cam.pos, cam.save_ok = 200, False                     # the save failed: home preset as before
        p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(cam.calls[-1], "select.kitchen_move_to_preset=Living Room")
        self.assertEqual(cam.pos, 120)

    def test_one_sweep_per_camera_at_a_time(self):
        p, cam = self.make()
        started = []
        import threading
        real_sweep = p._sweep
        p._sweep = lambda e, c, pr=None, reason="scheduled": started.append((e, reason))   # thread records the start
        self.assertTrue(p.run_now("camera.kitchen")["ok"])
        self.assertFalse(p.run_now("camera.kitchen")["ok"])          # second press refused
        self.assertEqual(p.due("camera.kitchen", KITCHEN), "sweeping")
        self.assertEqual(p.sweep("camera.kitchen", KITCHEN)["skipped"], "already sweeping")   # scheduler too
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon:
                t.join(1)
        self.assertEqual(started, [("camera.kitchen", "manual")])     # traced as a manual sweep
        p._sweep = real_sweep

    def test_manual_move_mid_sweep_stops_it_where_the_owner_left_it(self):
        p, cam = self.make()
        clock = [1000.0]
        p.clock = lambda: clock[0]
        presses = []

        def on_press(eid):
            presses.append(eid)
            if len(presses) == 6:                  # the owner grabs the camera during the sweep
                clock[0] += 1
                p.note_manual("camera.kitchen")
        cam.on_press = on_press
        res = p.sweep("camera.kitchen", KITCHEN)
        self.assertTrue(res["interrupted"])
        self.assertEqual(len(presses), 6)                            # no move after the owner's
        self.assertNotIn("select.kitchen_move_to_preset=Living Room", cam.calls)   # and no trip home
        self.assertEqual(cam.angle, 45)
        self.assertFalse(p.state["camera.kitchen"]["busy"])

    def test_per_camera_start_step_and_frame_cap(self):
        p, cam = self.make()
        pans = []
        p.vision_fn = lambda f, where, prof: pans.append(where) or {"seen": []}
        kitchen = dict(KITCHEN, start_deg=90, step_deg=40, max_frames=4)   # the owner's 4-frame kitchen sweep
        res = p.sweep("camera.kitchen", kitchen)
        self.assertEqual(res["frames"], 4)
        self.assertEqual([w.split("pan ")[1] for w in pans], ["90 deg)", "130 deg)", "170 deg)", "210 deg)"])
        st = p.status()["cameras"]["camera.kitchen"]
        self.assertEqual((st["first_pan"], st["step"]), (90, 40))

    def test_refused_move_is_retried_then_reported_not_taken_for_the_end_stop(self):
        p, cam = self.make()
        recs = []
        p.on_sweep = recs.append
        real = cam.press_button
        refused = {"n": 0}

        def flaky(eid):
            if eid.endswith("right") and refused["n"] < 1:     # one refusal: the retry gets through
                refused["n"] += 1
                return {"ok": False, "error": "HTTP Error 500"}
            return real(eid)
        cam.press_button = flaky
        self.assertEqual(p.sweep("camera.kitchen", KITCHEN)["frames"], 8)
        self.assertIn("move_error", recs[-1]["triggers"])
        cam.press_button = lambda eid: {"ok": False, "error": "HTTP Error 500"} if eid.endswith("right") else real(eid)
        p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual((recs[-1]["stopped"], recs[-1]["move_error"]), ("move_failed", "HTTP Error 500"))
        self.assertEqual(recs[-1]["returned"], "start")                # still goes back where it was

    def test_a_camera_parked_at_the_left_end_stop_still_sweeps(self):
        """2026-09-28 live: the Tapo refuses a move past its end stop ("Maximum Pan/Tilt range reached", a bare HTTP 500
        to us). Sweeps left the kitchen camera at the left end, so every later sweep stopped at frame 0 on it."""
        p, cam = self.make()
        recs = []
        p.on_sweep = recs.append
        real = cam.press_button
        cam.pos = 0                                                   # parked at the left end stop

        def tapo(eid):
            if eid.endswith("left") and cam.pos == 0:
                return {"ok": False, "error": "HTTP Error 500: Internal Server Error"}
            return real(eid)
        cam.press_button = tapo
        res = p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(res["frames"], 8)                            # the full sweep, not 0
        self.assertTrue(recs[-1]["left_end_refused"])
        self.assertNotIn("move_error", recs[-1]["triggers"])          # expected, not an error
        self.assertEqual(recs[-1]["stopped"], "end_stop")
        self.assertEqual(recs[-1]["returned"], "start")

    def test_an_unreachable_camera_still_fails_the_sweep(self):
        p, cam = self.make()
        recs = []
        p.on_sweep = recs.append
        cam.press_button = lambda eid: {"ok": False, "error": "HTTP Error 500"}      # every move refused
        self.assertEqual(p.sweep("camera.kitchen", KITCHEN)["frames"], 0)
        self.assertEqual(recs[-1]["stopped"], "move_failed")          # the first right move fails too
        self.assertIn("move_error", recs[-1]["triggers"])

    def test_a_refused_preset_save_is_retried(self):
        p, cam = self.make()
        real = cam.call_service
        n = {"save": 0}

        def flaky(domain, service, data, timeout=4):
            if service == "save_preset" and n["save"] == 0:
                n["save"] += 1
                return {"ok": False, "error": "HTTP Error 500"}
            return real(domain, service, data, timeout)
        cam.call_service = flaky
        cam.pos = 200
        p.sweep("camera.kitchen", KITCHEN)
        self.assertEqual(cam.pos, 200)                                  # the retry saved it, so it went back

    def test_run_once_reads_presence_once(self):
        p, _ = self.make()
        p.run_once()
        self.assertEqual(self.presence_reads, 1)


class TestPatrolRecords(unittest.TestCase):
    """The trace record a sweep leaves (kind patrol; moved here from the agent loop's trace tests: it is Home's)."""
    def make(self):
        t = TestSweep()
        p, cam = t.make()
        recs = []
        p.on_sweep = recs.append
        return p, cam, recs, KITCHEN

    def test_sweep_record(self):
        p, cam, recs, kitchen = self.make()
        p._sweep("camera.kitchen", kitchen, None, "manual") if p._claim("camera.kitchen") else None
        rec = recs[0]
        self.assertEqual((rec["kind"], rec["reason"], rec["outcome"], rec["returned"]), ("patrol", "manual", "done", "start"))
        self.assertEqual(rec["frames"], 8)
        self.assertEqual(rec["pans"][:2], [30, 60])
        self.assertEqual(rec["stopped"], "end_stop")
        self.assertEqual(rec["vision"]["calls"], 8)
        self.assertEqual(rec["triggers"], [])

    def test_return_failed_and_vision_error(self):
        p, cam, recs, kitchen = self.make()
        cam.save_ok = False
        cam.select_option = lambda eid, option, timeout=4: {"ok": False, "error": "camera offline"}
        p.vision_fn = lambda f, w, prof: {"seen": [], "error": "vision down"}
        p.sweep("camera.kitchen", kitchen)
        rec = recs[0]
        self.assertEqual((rec["reason"], rec["returned"]), ("scheduled", "failed"))
        self.assertIn("return_failed", rec["triggers"])
        self.assertIn("save_failed", rec["triggers"])
        self.assertFalse(rec["saved_start"])
        self.assertEqual(rec["home_error"], "camera offline")          # HA's words, for the next debugging session
        self.assertIn("vision_error", rec["triggers"])


if __name__ == "__main__":
    unittest.main()
