"""Offline tests for the commentary engine (when Computer speaks unprompted): arrivals from GPS + a door in either order,
activity episodes from two agreeing vision reads, and the gates (quiet hours, nobody home, budget, cooldown, DND)."""

import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from hearth import commentary as cm  # noqa: E402

NOON = datetime(2026, 9, 27, 12, 0).timestamp()   # local time: outside the default quiet hours
DOOR = "binary_sensor.front_door"
DND = "switch.kitchen_dnd"
PHONE_ON = "binary_sensor.alex_phone_interactive"
SLEEP = "sensor.alex_phone_sleep_confidence"
VOICE = "event.kitchen_echo_voice_event"
STEPS = "sensor.garmin_connect_steps"
BB = "sensor.garmin_connect_body_battery"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()



def settings(commentary_cfg):
    """The commentary settings as the app sees them: residents come from the household (config.json)."""
    return cm._cfg_from({"household": {"people": ["Alex", "Sam"]}, "commentary": commentary_cfg})

class Rig:
    """A Commentary on a fake clock, fake HA, fake Frigate and fake models; records what was spoken."""

    def __init__(self, **cfg):
        self.now = NOON
        self.ha = {}
        self.people_in_view = []
        self.activity = "other"
        self.spoken, self.traces, self.vision_calls = [], [], 0
        self.cfg = {"enabled": True, "mode": "speak", "dnd": {"kitchen": DND},
                    "arrival": {"doors": [DOOR], "cameras": ["living_room"], "room": "kitchen", "min_away_min": 30},
                    "activity": {"cameras": {"living_room": "kitchen"}, "check_every_s": 120, "min_in_view_s": 45},
                    **cfg}
        self.c = cm.Commentary(lambda: {"household": {"people": ["Alex", "Sam"]}, "commentary": self.cfg}, self.ha.get, lambda cam: self.people_in_view,
                               lambda cam: b"jpeg", self._vision, lambda msgs: "Ah, the wanderer returns.",
                               self._speak, trace=self.traces.append, clock=lambda: self.now)

    def _vision(self, jpeg, prompt):
        self.vision_calls += 1
        return '{"activity": "%s", "detail": "stirring a pot"}' % self.activity

    def _speak(self, text, room):
        self.spoken.append((text, room))
        return {"ok": True}

    def person(self, name, state, at):
        self.ha[f"person.{name}"] = {"state": state, "last_changed": iso(at)}

    def door(self, at):
        self.ha[DOOR] = {"state": "off", "last_changed": iso(at)}

    def tick(self, advance=20):
        self.now += advance
        self.c.tick()


class TestArrival(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.r.person("alex", "not_home", NOON - 3 * 3600)
        self.r.person("sam", "home", NOON - 5 * 3600)
        self.r.door(NOON - 600)                        # Sam was up and about 10 min ago
        self.r.tick()                                   # baseline: nobody greeted for what was already true

    def test_gps_then_door_greets_once(self):
        self.r.person("alex", "home", self.r.now)
        self.r.tick()
        self.assertEqual(self.r.spoken, [])            # home per GPS, still outside
        self.r.door(self.r.now)
        self.r.tick()
        self.assertEqual(len(self.r.spoken), 1)
        rec = self.r.traces[-1]
        self.assertEqual((rec["kind"], rec["trigger"], rec["outcome"], rec["who"]), ("commentary", "arrival", "spoken", ["alex"]))
        self.assertIn("3.0 hours", rec["facts"])
        self.r.tick()
        self.assertEqual(len(self.r.spoken), 1)

    def test_door_before_late_gps_still_greets(self):
        self.r.door(self.r.now)
        self.r.tick()
        self.r.tick(300)                                # Life360 catches up five minutes later
        self.r.person("alex", "home", self.r.now)
        self.r.tick()
        self.assertEqual(len(self.r.spoken), 1)

    def test_short_trip_is_not_an_arrival(self):
        self.r.person("sam", "not_home", self.r.now)
        self.r.tick(600)
        self.r.person("sam", "home", self.r.now)
        self.r.door(self.r.now)
        self.r.tick()
        self.assertEqual(self.r.spoken, [])

    def test_no_door_no_sighting_expires_quietly(self):
        self.r.person("alex", "home", self.r.now)
        self.r.tick()
        self.r.people_in_view = [{"label": "person", "for_s": 5}]   # Sam is home: an unnamed person proves nothing
        self.r.tick(cm.ARRIVAL_WINDOW_S + 60)
        self.assertEqual(self.r.spoken, [])
        self.assertEqual(self.r.traces[-1]["held"], "not_confirmed")

    def test_first_named_sighting_since_gps_welcomes(self):
        sightings = {}
        self.r.c.seen_since = lambda name, t: sightings.get(name) if sightings.get(name + "_at", 0) >= t else None
        self.r.person("alex", "home", self.r.now)
        self.r.tick()
        self.assertEqual(self.r.spoken, [])
        sightings.update(alex="driveway", alex_at=self.r.now + 30)
        self.r.tick(60)
        self.assertEqual(len(self.r.spoken), 1)
        self.assertEqual(self.r.traces[-1]["confirmed_by"], ["seen on driveway"])

    def test_unnamed_person_counts_when_nobody_else_is_home(self):
        self.r.person("sam", "not_home", self.r.now - 7200)
        self.r.tick()
        self.r.person("alex", "home", self.r.now)
        self.r.tick()
        self.r.people_in_view = [{"label": "person", "for_s": 3}]
        self.r.tick()
        self.assertEqual(len(self.r.spoken), 1)
        self.assertEqual(self.r.traces[-1]["confirmed_by"], ["seen on living room"])


class TestResting(unittest.TestCase):
    """No clock (the owner, 2026-09-27): whether a remark could wake someone comes from phones and signs of life."""

    def setUp(self):
        self.r = Rig(rest={"phones": {"alex": {"interactive": PHONE_ON, "sleep_confidence": SLEEP}},
                           "activity_events": [VOICE], "idle_min": 60})
        self.r.person("alex", "home", NOON - 5 * 3600)
        self.r.person("sam", "not_home", NOON - 3 * 3600)
        self.r.ha[PHONE_ON] = {"state": "off", "last_changed": iso(NOON - 4 * 3600)}
        self.r.ha[SLEEP] = {"state": "90"}
        self.r.door(NOON - 3 * 3600)
        self.r.tick()

    def arrive(self, name="sam"):
        self.r.person(name, "home", self.r.now)
        self.r.door(self.r.now)
        self.r.tick()

    def test_partner_asleep_holds_the_welcome(self):
        self.arrive()
        self.assertEqual(self.r.spoken, [])
        self.assertIn("Alex may be asleep (phone sleep confidence 90%)", self.r.traces[-1]["held"])

    def test_partner_on_their_phone_is_awake_at_any_hour(self):
        self.r.now = datetime(2026, 9, 28, 3, 10).timestamp()   # 3 a.m. is fine when the house is up
        self.r.ha[PHONE_ON] = {"state": "on", "last_changed": iso(self.r.now)}
        self.arrive()
        self.assertEqual(len(self.r.spoken), 1)

    def test_quiet_house_holds_when_nobody_is_known_awake(self):
        self.r.ha[SLEEP] = {"state": "10"}                     # phone says awake-ish, but unused for 4 h
        self.r.tick(2 * 3600)                                  # two hours with no sign of life
        self.arrive()
        self.assertEqual(self.r.spoken, [])
        self.assertIn("house quiet", self.r.traces[-1]["held"])

    def test_a_recent_voice_command_counts_as_life(self):
        self.r.ha[SLEEP] = {"state": "10"}
        self.r.tick(2 * 3600)
        self.r.ha[VOICE] = {"state": iso(self.r.now - 300)}     # "Alexa, ..." five minutes ago
        self.r.tick()
        self.arrive()
        self.assertEqual(len(self.r.spoken), 1)

    def test_a_phone_used_away_from_home_is_not_life_in_the_house(self):
        self.r.person("alex", "not_home", self.r.now - 3 * 3600)
        self.r.tick(2 * 3600)
        self.r.ha[PHONE_ON] = {"state": "on", "last_changed": iso(self.r.now)}
        self.r.tick()
        self.assertFalse([s for t, s in self.r.c.stirs if s.startswith("phone:") and t > self.r.now - 3600])

    def test_partner_who_just_got_home_is_awake(self):
        self.r.ha[SLEEP] = {"state": "10"}
        self.r.person("alex", "not_home", self.r.now)
        self.r.tick(2 * 3600)                                  # both out, house silent for two hours
        self.r.person("sam", "home", self.r.now)          # in through the garage: GPS only, no door, no camera
        self.r.tick()
        self.r.tick(600)
        cfg = settings(self.r.cfg)
        self.assertEqual(self.r.c.resident_state(cfg, "sam"), ("awake", "got home 10 min ago"))
        self.assertIsNone(self.r.c.resting(cfg, ["alex"], self.r.now))   # so a welcome for Alex now would play
        self.r.tick(600)
        self.assertEqual(self.r.c.resident_state(cfg, "sam")[0], "unknown")


class TestWatch(unittest.TestCase):
    """Alex's Garmin (Connect via HA): steps since the previous sync = awake; body battery charging, no steps = asleep."""

    def setUp(self):
        self.r = Rig(rest={"phones": {"alex": {"interactive": PHONE_ON, "sleep_confidence": SLEEP}},
                           "wearables": {"alex": {"steps": STEPS, "body_battery": BB}}, "idle_min": 60})
        self.r.person("alex", "home", NOON - 5 * 3600)
        self.r.person("sam", "not_home", NOON - 3 * 3600)
        self.r.ha[PHONE_ON] = {"state": "off", "last_changed": iso(NOON - 4 * 3600)}
        self.r.ha[SLEEP] = {"state": "10"}
        self.r.door(NOON - 3 * 3600)
        self.watch(3000, 40, NOON - 1800)
        self.r.tick()

    def watch(self, steps, bb, at, bb_at=None):
        self.r.ha[STEPS] = {"state": str(steps), "last_changed": iso(at)}
        self.r.ha[BB] = {"state": str(bb), "last_changed": iso(bb_at or at)}

    def state(self):
        return self.r.c.resident_state(settings(self.r.cfg), "alex")

    def test_steps_since_the_last_sync_mean_awake(self):
        self.watch(3400, 38, self.r.now)
        self.r.tick()
        self.assertEqual(self.state()[0], "awake")
        self.assertIn("400 steps", self.state()[1])

    def test_body_battery_charging_without_steps_means_asleep(self):
        self.watch(3000, 46, self.r.now)
        self.r.tick()
        self.assertEqual(self.state(), ("asleep", f"watch: body battery +6% with no steps by "
                                                  f"{datetime.fromtimestamp(self.r.now).strftime('%H:%M')}"))
        self.r.person("sam", "home", self.r.now)
        self.r.door(self.r.now)
        self.r.tick()
        self.assertEqual(self.r.spoken, [])
        self.assertIn("Alex may be asleep (watch: body battery", self.r.traces[-1]["held"])

    def test_steps_and_battery_landing_a_second_apart_are_one_sync(self):
        self.watch(3400, 40, self.r.now)                   # steps arrive first...
        self.r.tick()
        self.watch(3400, 43, self.r.now, bb_at=self.r.now + 1)   # ...battery a second later: not "no steps"
        self.r.tick(1)
        self.assertEqual(self.state()[0], "awake")

    def test_stale_watch_data_says_nothing(self):
        self.watch(3000, 46, self.r.now - 3 * 3600)
        self.r.c.wear["alex"] = [{"t": self.r.now - 4 * 3600, "steps": 3000, "bb": 40},
                                   {"t": self.r.now - 3 * 3600, "steps": 3000, "bb": 46}]
        self.assertEqual(self.state()[0], "unknown")

    def test_a_restart_reads_the_watch_history(self):
        hist = {STEPS: [(NOON - 3600, "2900"), (NOON - 600, "2900")], BB: [(NOON - 3600, "30"), (NOON - 600, "37")]}
        c = cm.Commentary(lambda: {"household": {"people": ["Alex", "Sam"]}, "commentary": self.r.cfg}, self.r.ha.get, lambda cam: [], lambda cam: None,
                          None, None, None, clock=lambda: self.r.now, ha_history=lambda ids, t: hist)
        self.r.ha.pop(STEPS)
        c.tick()
        self.assertEqual(c.wearable_state(settings(self.r.cfg), "alex")[0], "asleep")
        self.assertEqual(cm.merge_samples([(1, "10")], [(2, "5"), (3, "x")]), [{"t": 2, "steps": 10.0, "bb": 5.0}])


class TestRestart(unittest.TestCase):
    setUp = TestResting.setUp
    arrive = TestResting.arrive

    def test_a_restart_remembers_earlier_sightings(self):
        c = cm.Commentary(lambda: {"household": {"people": ["Alex", "Sam"]}, "commentary": self.r.cfg}, self.r.ha.get, lambda cam: [], lambda cam: None,
                          None, None, None, clock=lambda: self.r.now,
                          history=lambda cams, t: [(self.r.now - 1800, cams[0])])
        c.tick()
        self.assertEqual(c.last_stir(self.r.now), self.r.now - 1800)

    def test_the_arrivals_own_door_does_not_count_as_life(self):
        self.r.ha[SLEEP] = {"state": "10"}
        self.r.tick(2 * 3600)
        self.r.door(self.r.now)                                # the arriver opens the door before GPS catches up
        self.r.tick()
        self.r.tick(120)
        self.arrive()
        self.assertEqual(self.r.spoken, [])


class TestActivity(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.r.person("alex", "home", NOON - 3600)
        self.r.people_in_view = [{"label": "person", "name": "alex", "for_s": 120}]

    def test_two_agreeing_reads_start_one_episode(self):
        self.r.activity = "cooking"
        self.r.tick()
        self.assertEqual(self.r.spoken, [])            # one read is not enough
        for _ in range(8):
            self.r.tick(120)
        self.assertEqual(len(self.r.spoken), 1)        # one remark per episode, however long it runs
        rec = next(t for t in self.r.traces if t.get("outcome") == "spoken")
        self.assertEqual((rec["trigger"], rec["who"]), ("cooking", ["alex"]))
        self.assertIn("stirring a pot", rec["facts"])

    def test_reads_must_agree_in_a_row(self):
        for act in ("cooking", "other", "cooking", "other", "cooking"):
            self.r.activity = act
            self.r.tick(120)
        self.assertEqual(self.r.spoken, [])

    def test_reads_are_spaced_and_need_a_person(self):
        self.r.activity = "cooking"
        self.r.tick()
        self.r.tick(20)
        self.assertEqual(self.r.vision_calls, 1)       # check_every_s
        self.r.people_in_view = []
        self.r.tick(cm.PRESENCE_GAP_S + 10)            # left
        self.r.people_in_view = [{"label": "person", "for_s": 10}]
        self.r.tick(200)
        self.assertEqual(self.r.vision_calls, 1)       # just walked in: not yet
        self.r.tick(40)
        self.assertEqual(self.r.vision_calls, 2)       # around for 50 s now

    def test_short_frigate_events_still_count_as_present(self):
        self.r.activity = "cooking"
        for i in range(12):                            # Frigate: 15 s events with gaps, never one long one
            self.r.people_in_view = [{"label": "person", "name": "alex", "for_s": 15}] if i % 3 == 0 else []
            self.r.tick(20)
        self.assertEqual(len(self.r.spoken), 1)

    def test_per_trigger_mode(self):
        self.assertEqual(cm.mode_for({"mode": {"arrival": "speak"}}, "arrival"), "speak")
        self.assertEqual(cm.mode_for({"mode": {"arrival": "speak"}}, "cooking"), "log")
        self.assertEqual(cm.mode_for({"mode": "speak"}, "cooking"), "speak")

    def test_other_never_speaks(self):
        for _ in range(5):
            self.r.tick(120)
        self.assertEqual(self.r.spoken, [])
        self.assertEqual(self.r.vision_calls, 5)

    def test_no_vision_when_it_could_not_speak(self):
        self.r.ha[DND] = {"state": "on"}
        self.r.activity = "cleaning"
        for _ in range(4):
            self.r.tick(120)
        self.assertEqual((self.r.vision_calls, self.r.spoken), (0, []))

    def test_nobody_home_by_gps(self):
        self.r.person("alex", "not_home", NOON - 3600)
        self.r.activity = "cooking"
        for _ in range(3):
            self.r.tick(120)
        self.assertEqual(self.r.spoken, [])

    def test_new_episode_after_the_gap(self):
        self.r.activity = "cleaning"
        self.r.tick()
        self.r.tick(120)
        self.assertEqual(len(self.r.spoken), 1)
        self.r.activity = "other"
        self.r.tick(cm.EPISODE_GAP_S + 60)
        self.r.activity = "cleaning"
        self.r.tick(120)
        self.r.tick(120)
        self.assertEqual(len(self.r.spoken), 2)


class TestGates(unittest.TestCase):
    def test_daily_budget_and_cooldown(self):
        r = Rig(daily_budget=2, room_cooldown_min=10)
        r.person("alex", "home", NOON - 3600)
        cfg = settings(r.cfg)
        self.assertEqual(r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")["held"], "nobody_home")   # not looked yet
        r.c._check_people(cfg)
        self.assertEqual(r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")["outcome"], "spoken")
        self.assertEqual(r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")["held"], "room_cooldown")
        r.now += 601
        r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")
        r.now += 601
        self.assertEqual(r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")["held"], "daily_budget")
        r.now += 86400
        self.assertEqual(r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")["outcome"], "spoken")   # a rolling 24 h, no midnight

    def test_log_mode_and_dry_run_never_speak(self):
        r = Rig(mode="log")
        r.person("alex", "home", NOON - 3600)
        r.c._check_people(settings(r.cfg))
        self.assertEqual(r.c.remark(settings(r.cfg), "cooking", "kitchen", [], "x")["outcome"], "logged")
        self.assertEqual(r.c.remark(settings(r.cfg), "cooking", "kitchen", [], "x", dry=True)["outcome"], "dry_run")
        self.assertEqual(r.spoken, [])
        self.assertEqual([t["outcome"] for t in r.traces], ["logged"])   # dry runs are not traced

    def test_shadow_remark_does_not_hold_back_a_spoken_one(self):
        r = Rig(mode={"arrival": "speak", "cooking": "log"})
        r.person("alex", "home", NOON - 3600)
        cfg = settings(r.cfg)
        r.c._check_people(cfg)
        self.assertEqual(r.c.remark(cfg, "cooking", "kitchen", ["alex"], "x")["outcome"], "logged")
        self.assertEqual(r.c.remark(cfg, "arrival", "kitchen", ["sam"], "x", arriving=True)["outcome"], "spoken")
        self.assertEqual((r.c.status()["spoken_24h"], r.c.status()["logged_24h"]), (1, 1))

    def test_disabled_keeps_baselines_but_says_nothing(self):
        r = Rig(enabled=False)
        r.person("alex", "not_home", NOON - 3 * 3600)
        r.door(NOON - 3600)
        r.tick()
        r.person("alex", "home", r.now)
        r.door(r.now)
        r.tick()
        self.assertEqual((r.spoken, r.traces), ([], []))
        r.cfg["enabled"] = True
        r.tick()
        self.assertEqual(r.spoken, [])                 # switching on does not greet a stale arrival

    def test_parsing(self):
        self.assertEqual(cm.parse_activity('```json\n{"activity": "Cooking", "detail": "chopping"}\n```'), ("cooking", "chopping"))
        self.assertEqual(cm.parse_activity("no idea"), ("other", ""))
        self.assertEqual(cm.parse_activity('{"activity": "dancing"}')[0], "other")
        self.assertEqual(cm.parse_activity('{"activity": "cleaning", "evidence": "none"}'), ("other", ""))
        self.assertEqual(cm.parse_activity('{"activity": "cleaning", "evidence": "a sponge"}'), ("cleaning", "a sponge"))
        self.assertEqual(cm.clean_remark('Computer: "Welcome back."\nMore.'), "Welcome back.")


class TestHouseholdForTheIdleGate(unittest.TestCase):
    """What the platform's idle gate asks Home (hook "household"): may the GPUs be lent out now?"""
    def test_household_from_the_commentary_signals(self):
        r = Rig(rest={"phones": {"alex": {"interactive": PHONE_ON, "sleep_confidence": SLEEP}}, "idle_min": 60})
        r.person("alex", "home", NOON - 5 * 3600)
        r.person("sam", "not_home", NOON - 3 * 3600)
        r.ha[PHONE_ON] = {"state": "on", "last_changed": "2026-01-01T00:00:00+00:00"}
        r.ha[SLEEP] = {"state": "5"}
        r.tick()
        h = r.c.household()
        self.assertFalse(h["quiet"])
        self.assertIn("Alex is awake (using their phone)", h["reasons"])
        r.ha[PHONE_ON] = {"state": "off", "last_changed": "2026-01-01T00:00:00+00:00"}
        r.ha[SLEEP] = {"state": "92"}
        self.assertTrue(r.c.household()["quiet"])          # asleep at home; Sam away with no evidence: no objection


if __name__ == "__main__":
    unittest.main()
