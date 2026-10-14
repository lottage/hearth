"""Scenes (hearth/scenes.py): an HA script read step by step, diffed against the room, run only where needed, verified and
reported truthfully. Offline: a fake room stands in for Home Assistant. Entity ids are placeholders (the real ones live in
Home Assistant and config.json, never here)."""

import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from hearth import scenes as sc  # noqa: E402
from agent_fixture import ScriptedLLM, reply, run, tool_call  # noqa: E402
import agent_fixture  # noqa: E402

LAMP_A, LAMP_B = "switch.example_lamp_a", "switch.example_lamp_b"
BACKLIGHT, FIRE = "light.example_tv_backlight", "light.example_fireplace"
STAT, TV, SCRIPT = "climate.example_thermostat", "remote.example_tv", "script.example_movie"

SCRIPT_CFG = {"alias": "Example movie", "sequence": [
    {"action": "switch.turn_off", "target": {"entity_id": LAMP_A}},
    {"delay": {"seconds": 1}},
    {"action": "switch.turn_off", "target": {"entity_id": LAMP_B}},
    {"action": "light.turn_on", "target": {"entity_id": BACKLIGHT}, "data": {"brightness_pct": 100}},
    {"action": "light.turn_on", "target": {"entity_id": FIRE}, "data": {"effect": "TV time", "brightness_pct": 30}, "continue_on_error": True},
    {"choose": [
        {"conditions": [{"condition": "state", "entity_id": STAT, "state": "cool"}],
         "sequence": [{"action": "climate.set_temperature", "target": {"entity_id": STAT}, "data": {"temperature": 70}}]},
        {"conditions": [{"condition": "state", "entity_id": STAT, "state": "heat"}],
         "sequence": [{"action": "climate.set_temperature", "target": {"entity_id": STAT}, "data": {"temperature": 70}}]},
        {"conditions": [{"condition": "state", "entity_id": STAT, "state": "heat_cool"}],
         "sequence": [{"action": "climate.set_temperature", "target": {"entity_id": STAT}, "data": {"target_temp_low": 68, "target_temp_high": 72}}]}]},
    {"action": "remote.turn_on", "target": {"entity_id": TV}, "continue_on_error": True},
]}
CONFIG = {"scenes": {"movie_night": {"script": SCRIPT, "phrases": ["movie time", "cinema mode"], "confirm": True, "title": "Movie night"}}}


def ent(eid, state, name=None, **attrs):
    return {"entity_id": eid, "state": state, "friendly_name": name or eid.split(".")[1], "attributes": dict(attrs, friendly_name=name or eid)}


class Room:
    """Home Assistant as far as scenes can tell: states in, services applied, a few misbehaving devices."""

    def __init__(self, entities):
        self.s = {e["entity_id"]: e for e in entities}
        self.calls, self.ignore, self.fail = [], set(), set()

    def get_states(self, domain=None):
        ents = [copy.deepcopy(e) for e in self.s.values() if not domain or e["entity_id"].startswith(domain + ".")]
        return {"ok": True, "entities": ents}

    def call(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        eid = data.get("entity_id")
        if eid in self.fail:
            return {"ok": False, "error": "HTTP 500"}
        e = self.s.get(eid)
        if e is None or eid in self.ignore or e["state"] == "unavailable":
            return {"ok": True}
        a = e["attributes"]
        if service == "turn_off":
            e["state"] = "off"
        elif service == "turn_on":
            e["state"] = "on"
            for k, v in data.items():
                if k == "brightness_pct":
                    a["brightness"] = round(v * 2.55)
                elif k != "entity_id":
                    a[k] = v
        elif service == "set_temperature":
            a.update({k: v for k, v in data.items() if k != "entity_id"})
        return {"ok": True}


def room(**over):
    base = [ent(LAMP_A, "off", "Lamp A", assumed_state=True), ent(LAMP_B, "on", "Lamp B", assumed_state=True),
            ent(BACKLIGHT, "on", "TV Lights", brightness=51), ent(FIRE, "on", "Fireplace", effect="Cozy", brightness=200),
            ent(STAT, "heat_cool", "Thermostat", target_temp_low=66, target_temp_high=70, temperature=None),
            ent(TV, "off", "TV")]
    r = Room(base)
    for k, v in over.items():
        r.s[k]["state"] = v
    return r


def engine(r, cfg=None, script=None, spoken=None, **kw):
    spoken = spoken if spoken is not None else []
    sleeps = kw.pop("sleeps", [])
    return sc.Scenes(lambda: cfg if cfg is not None else CONFIG, r.get_states, r.call,
                     lambda oid: copy.deepcopy(script if script is not None else SCRIPT_CFG),
                     speak=lambda m, room_: spoken.append((m, room_)) or {"ok": True}, sleep=sleeps.append, **kw)


def actions(r):
    return [(d, s, c["entity_id"]) for d, s, c in r.calls]


class TestDiff(unittest.TestCase):
    def test_only_what_needs_changing_is_called(self):
        r = room()
        r.s[BACKLIGHT]["attributes"]["brightness"] = 255             # already at 100 %
        res = engine(r).run("movie_night")
        self.assertEqual(actions(r), [("switch", "turn_off", LAMP_A), ("switch", "turn_off", LAMP_B),
                                      ("light", "turn_on", FIRE), ("climate", "set_temperature", STAT)])
        by = {i["entity_id"]: i for i in res["items"]}
        self.assertEqual(by[BACKLIGHT]["action"], "skip")
        self.assertEqual(by[FIRE]["result"], "done")

    def test_light_attribute_checks(self):
        def step(**d):
            return {"domain": "light", "service": "turn_on", "data": d}
        e = ent("light.x", "on", brightness=128, effect="Cozy", color_temp_kelvin=3000)
        self.assertTrue(sc._satisfied(step(brightness_pct=50), e))          # 128/255 = 50.2 %
        self.assertFalse(sc._satisfied(step(brightness_pct=100), e))
        self.assertTrue(sc._satisfied(step(effect="cozy"), e))
        self.assertFalse(sc._satisfied(step(effect="TV time"), e))
        self.assertTrue(sc._satisfied(step(color_temp_kelvin=3050), e))
        self.assertFalse(sc._satisfied(step(color_temp_kelvin=4000), e))
        self.assertFalse(sc._satisfied(step(brightness_pct=50), ent("light.x", "off", brightness=128)))
        self.assertTrue(sc._satisfied({"domain": "light", "service": "turn_off", "data": {}}, ent("light.x", "off")))

    def test_thermostat_range_and_branch(self):
        r = room()
        r.s[STAT]["attributes"].update(target_temp_low=68, target_temp_high=72)
        res = engine(r).plan("movie_night")
        self.assertEqual({i["entity_id"]: i["action"] for i in res["items"]}[STAT], "skip")
        r.s[STAT]["attributes"]["target_temp_high"] = 74                      # 2 degrees off: change it
        self.assertEqual({i["entity_id"]: i["action"] for i in engine(r).plan("movie_night")["items"]}[STAT], "run")
        r.s[STAT]["state"] = "heat"                                           # the heat branch: a single temperature
        r.s[STAT]["attributes"]["temperature"] = 70
        p = {i["entity_id"]: i for i in engine(r).plan("movie_night")["items"]}[STAT]
        self.assertEqual((p["action"], p["data"]), ("skip", {"temperature": 70}))
        r.s[STAT]["state"] = "off"                                            # no branch for off: the thermostat is left alone
        self.assertNotIn(STAT, {i["entity_id"] for i in engine(r).plan("movie_night")["items"]})

    def test_tv_is_reported_never_powered_on(self):
        r = room()
        res = engine(r).run("movie_night")
        self.assertNotIn(("remote", "turn_on", TV), actions(r))
        self.assertIn("TV is off", res["summary"])

    def test_off_step_skipped_when_already_off(self):
        cfg = {"sequence": [{"action": "light.turn_off", "target": {"entity_id": [BACKLIGHT, FIRE]}}]}
        r = room(**{BACKLIGHT: "off"})
        engine(r, script=cfg).run("movie_night")
        self.assertEqual(actions(r), [("light", "turn_off", FIRE)])


class TestAssumedAndVerify(unittest.TestCase):
    def test_assumed_switches_are_always_sent_and_reported_as_sent(self):
        r = room(**{LAMP_B: "off"})                                           # the state says off: the lamp may still be lit
        sleeps = []
        res = engine(r, sleeps=sleeps).run("movie_night")
        self.assertIn(("switch", "turn_off", LAMP_A), actions(r))
        self.assertIn(("switch", "turn_off", LAMP_B), actions(r))
        self.assertEqual({i["entity_id"]: i["result"] for i in res["items"] if i["assumed"]}, {LAMP_A: "sent", LAMP_B: "sent"})
        self.assertIn("sent; I can't confirm those", res["summary"])
        self.assertIn(1.0, sleeps)                                           # a gap so the Echo hears both

    def test_device_that_did_not_take_it_is_reported(self):
        r = room()
        r.ignore.add(FIRE)
        res = engine(r).run("movie_night")
        self.assertFalse(res["ok"])
        self.assertIn("Fireplace didn't change", res["summary"])

    def test_offline_device_is_said_plainly_and_not_called(self):
        r = room()
        r.s[FIRE]["state"] = "unavailable"
        res = engine(r).run("movie_night")
        self.assertNotIn(("light", "turn_on", FIRE), actions(r))
        self.assertTrue(res["summary"].startswith("Movie night is set, but Fireplace is offline."))
        self.assertTrue(res["ok"])

    def test_call_refused_by_ha(self):
        r = room()
        r.fail.add(BACKLIGHT)
        res = engine(r).run("movie_night")
        self.assertFalse(res["ok"])
        self.assertIn("Home Assistant refused TV Lights", res["summary"])

    def test_goes_offline_after_the_call(self):
        r = room()
        orig = r.call

        def call(d, s, data):
            out = orig(d, s, data)
            if data.get("entity_id") == FIRE:
                r.s[FIRE]["state"] = "unavailable"
            return out
        res = sc.Scenes(lambda: CONFIG, r.get_states, call, lambda oid: copy.deepcopy(SCRIPT_CFG), sleep=lambda s: None).run("movie_night")
        self.assertIn("Fireplace is offline", res["summary"])

    def test_nothing_to_do(self):
        r = room()
        r.s[LAMP_A]["attributes"].pop("assumed_state")
        r.s[LAMP_B]["attributes"].pop("assumed_state")
        r.s[LAMP_B]["state"] = "off"
        r.s[BACKLIGHT]["attributes"]["brightness"] = 255
        r.s[FIRE]["attributes"].update(effect="TV time", brightness=77)
        r.s[STAT]["attributes"].update(target_temp_low=68, target_temp_high=72)
        res = engine(r).run("movie_night")
        self.assertEqual(r.calls, [])
        self.assertTrue(res["summary"].startswith("Movie night was already set."))


class TestPlanAndSummary(unittest.TestCase):
    def test_plan_only_calls_nothing(self):
        r = room()
        res = engine(r).run("movie_night", plan_only=True)
        self.assertEqual(r.calls, [])
        self.assertTrue(res["plan_only"])
        self.assertIn("would change", res["summary"])
        self.assertNotIn("announced", res)

    def test_summary_text(self):
        r = room()
        r.s[BACKLIGHT]["attributes"]["brightness"] = 255
        res = engine(r).run("movie_night")
        self.assertEqual(res["summary"],
                         "Movie night is set. Fireplace on TV time at 30 %; Thermostat set to 68-72; "
                         "Lamp A and Lamp B off (sent; I can't confirm those). TV Lights already on at 100 %. TV is off.")

    def test_confirm_speaks_once_on_run_only(self):
        spoken = []
        r = room()
        e = engine(r, spoken=spoken)
        e.run("movie_night")
        self.assertEqual(len(spoken), 1)
        e.run("movie_night", announce=False)
        self.assertEqual(len(spoken), 1)

    def test_unreadable_script_runs_whole_and_says_so(self):
        cfg = {"sequence": [{"repeat": {"count": 2, "sequence": []}}]}
        r = room()
        res = engine(r, script=cfg).run("movie_night")
        self.assertEqual(actions(r), [("script", "turn_on", SCRIPT)])
        self.assertEqual(res["mode"], "whole")
        self.assertIn("ran all of it and can't check", res["summary"])

    def test_template_and_device_targets_are_not_interpreted(self):
        for step in ({"action": "light.turn_on", "target": {"entity_id": BACKLIGHT}, "data": {"brightness_pct": "{{ 5 }}"}},
                     {"action": "light.turn_on", "target": {"device_id": "abc"}},
                     {"action": "scene.turn_on", "target": {"entity_id": "scene.example"}}):
            r = room()
            self.assertEqual(engine(r, script={"sequence": [step]}).plan("movie_night")["mode"], "whole")

    def test_unlock_is_refused_not_run(self):
        r = room()
        res = engine(r, script={"sequence": [{"action": "lock.unlock", "target": {"entity_id": "lock.example_door"}}]}).run("movie_night")
        self.assertFalse(res["ok"])
        self.assertTrue(res["refused"])
        self.assertEqual(r.calls, [])

    def test_server_plug_never_switched_off(self):
        r = room()
        r.s["switch.example_server_plug"] = ent("switch.example_server_plug", "on")
        res = engine(r, script={"sequence": [{"action": "switch.turn_off", "target": {"entity_id": "switch.example_server_plug"}}]},
                     infra_words=lambda: ("server",)).run("movie_night")
        self.assertTrue(res.get("refused"))
        self.assertEqual(r.calls, [])

    def test_missing_scene_or_script(self):
        r = room()
        self.assertFalse(engine(r).run("nope")["ok"])
        e = sc.Scenes(lambda: CONFIG, r.get_states, r.call, lambda oid: None)
        self.assertIn("couldn't read", e.run("movie_night")["error"])


class TestPhrasesAndConfig(unittest.TestCase):
    def setUp(self):
        self.e = engine(room())

    def test_exact_phrases_match(self):
        for text in ("movie night", "Movie night!", "Hey Computer, please start movie night", "can you run the movie night scene",
                     "movie time", "turn on cinema mode", "activate movie night mode"):
            self.assertEqual(self.e.match(text), "movie_night", text)

    def test_other_sentences_do_not(self):
        for text in ("what is movie night", "turn off the movie night lights and lock up", "movie", "good night", "",
                     "I would like a movie night on saturday"):
            self.assertIsNone(self.e.match(text), text)

    def test_config_validation(self):
        ok = {"a": {"script": "script.a", "phrases": ["x y"], "confirm": True}}
        self.assertEqual(sc.config_errors(ok), [])
        self.assertEqual(sc.config_errors(None), [])
        self.assertTrue(sc.config_errors({"a": {"phrases": []}}))
        self.assertTrue(sc.config_errors({"a": {"script": "light.a"}}))
        self.assertTrue(sc.config_errors({"a": {"script": "script.a", "confirm": "yes"}}))
        self.assertTrue(sc.config_errors({"a": {"script": "script.a", "phrases": [""]}}))
        self.assertTrue(any("both" in m for m in sc.config_errors({"a": {"script": "script.a", "phrases": ["go"]},
                                                                   "b": {"script": "script.b", "phrases": ["Go!"]}})))
        self.assertTrue(sc.config_errors("nope"))


def courage_agent(r, e, llm):
    from stonesage.courage import CourageAgent, CourageDeps, CourageTools, PendingActions
    from household_fixture import TEST_HOUSEHOLD
    deps = CourageDeps(ha_states=r.get_states, ha_call=r.call, presence=lambda: agent_fixture.PRESENCE,
                       camera_look=lambda *a, **k: "", camera_scan=lambda *a: "", household=TEST_HOUSEHOLD,
                       run_scene=lambda name, plan_only=False: e.run(name, plan_only=plan_only),
                       scene_names=e.names, scene_match=e.match)
    return CourageAgent(CourageTools(deps), "http://fake:8001/v1", presence_fn=lambda: agent_fixture.PRESENCE,
                        pending=PendingActions(), post=llm)


class TestComputer(unittest.TestCase):
    def test_exact_phrase_runs_without_the_model(self):
        r = room()
        agent = courage_agent(r, engine(r), ScriptedLLM())              # no responses: any model call would raise
        events = run(agent, "movie night")
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "final"])
        self.assertIn("Movie night is set", events[-1]["content"])
        self.assertIn(("switch", "turn_off", LAMP_B), actions(r))

    def test_question_about_a_scene_is_not_run(self):
        r = room()
        agent = courage_agent(r, engine(r), ScriptedLLM(reply("It dims the lights.")))
        run(agent, "what does movie night do")
        self.assertEqual(r.calls, [])

    def test_scene_named_outright_is_an_order_and_a_guess_is_not(self):
        r = room()
        t = courage_agent(r, engine(r), ScriptedLLM()).tools
        args = {"name": "movie_night"}
        self.assertTrue(t.is_direct_command("run_scene", args, "start cinema mode please"))
        self.assertFalse(t.is_direct_command("run_scene", args, "is movie night on"))
        self.assertFalse(t.is_direct_command("run_scene", args, "I think I will watch a film"))
        self.assertFalse(t.is_direct_command("run_scene", {"name": "other"}, "movie night"))

    def test_inferred_scene_asks_first_then_runs_on_yes(self):
        r = room()
        llm = ScriptedLLM(tool_call("run_scene", {"name": "movie_night"}))
        agent = courage_agent(r, engine(r), llm)
        events = run(agent, "I think I'll watch a film tonight")
        self.assertEqual(events[-1]["content"], "Shall I set up movie night? Say yes to approve.")
        self.assertEqual(r.calls, [])
        agent.post = ScriptedLLM(reply("All set."))
        run(agent, "yes")
        self.assertTrue(r.calls)

    def test_unknown_scene_refused(self):
        r = room()
        agent = courage_agent(r, engine(r), ScriptedLLM())
        self.assertIn("no scene called", agent.tools.validate("run_scene", {"name": "nope"}))

    def test_tool_not_offered_without_scenes(self):
        agent, _ = agent_fixture.make_agent(ScriptedLLM(reply("hi")))
        self.assertNotIn("run_scene", [t["function"]["name"] for t in agent.tools.openai_tools()])


if __name__ == "__main__":
    unittest.main()
