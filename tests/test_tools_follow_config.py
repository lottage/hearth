"""Computer's tools follow the configuration (rc.9): a tool is offered only when its prerequisites exist, the prompt names
only what is offered, a camera name that does not exist comes back as an error that lists the real ones, and a reply that is
a tool call written as JSON is run (known tool) or refused (unknown), never shown to the person."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_fixture  # noqa: E402
from agent_fixture import FakeHA, ScriptedLLM, reply, tool_call  # noqa: E402
from household_fixture import TEST_HOUSEHOLD  # noqa: E402
from hearth.persona import render_persona  # noqa: E402
from stonesage.courage import CourageAgent, CourageDeps, CourageTools, PendingActions  # noqa: E402
from stonesage.courage.agent import fresh_facts, json_tool_calls  # noqa: E402
from stonesage.courage.household import Household  # noqa: E402
from stonesage.courage.prompt import build_presence_card  # noqa: E402
from stonesage.courage.tools import PREREQUISITES  # noqa: E402

CAMERA_TOOLS = {"camera_look", "camera_scan", "correct_sighting"}
HA_PERSONS = {"locations": {}, "home_status": {"alex": {"state": "home", "source_label": "GPS", "since_min": 12}}}


def tools_for(household, presence=None, **kw):
    deps = CourageDeps(ha_states=FakeHA().states, ha_call=FakeHA().call, presence=lambda: presence or {},
                       camera_look=lambda e, n, **k: "a cat", camera_scan=lambda e, n: "nobody", household=household, **kw)
    return CourageTools(deps)


def names(tools):
    return {t["function"]["name"] for t in tools.openai_tools()}


class TestPrerequisites(unittest.TestCase):
    def test_every_tool_has_a_stated_prerequisite(self):
        every = set(tools_for(TEST_HOUSEHOLD).specs)
        self.assertEqual(every - set(PREREQUISITES), set())

    def test_empty_household_offers_no_camera_phone_or_speaker_tools(self):
        got = names(tools_for(Household()))
        self.assertFalse(got & CAMERA_TOOLS)
        self.assertFalse(got & {"notify", "speak", "run_scene", "think_harder", "presence_now"})
        self.assertEqual(got, {"ha_get_states", "ha_call", "memory_search"})

    def test_a_configured_household_keeps_every_tool(self):
        got = names(tools_for(TEST_HOUSEHOLD))
        self.assertEqual(got, {"presence_now", "ha_get_states", "camera_look", "camera_scan", "memory_search", "ha_call",
                               "notify", "speak", "correct_sighting"})

    def test_each_prerequisite_switches_its_tool(self):
        cam = {"door": {"entity": "camera.door", "name": "Door"}}
        self.assertEqual(names(tools_for(Household(cameras=cam))) & CAMERA_TOOLS, {"camera_look", "correct_sighting"})
        self.assertIn("camera_scan", names(tools_for(Household(cameras=cam, ptz_cameras=["door"]))))
        self.assertIn("notify", names(tools_for(Household(phones=["alex"]))))
        self.assertIn("speak", names(tools_for(Household(speakers=["kitchen"]))))
        self.assertIn("presence_now", names(tools_for(Household(people=["Alex"]))))
        self.assertNotIn("ha_call", names(tools_for(Household(), ha_configured=lambda: False)))
        self.assertNotIn("ha_get_states", names(tools_for(Household(), ha_configured=lambda: False)))
        scenes = tools_for(Household(), run_scene=lambda n, p=False: {"ok": True}, scene_names=lambda: [])
        self.assertNotIn("run_scene", names(scenes))
        scenes = tools_for(Household(), run_scene=lambda n, p=False: {"ok": True}, scene_names=lambda: ["movie_night"])
        self.assertIn("run_scene", names(scenes))

    def test_presence_follows_home_assistant_person_states(self):
        self.assertIn("presence_now", names(tools_for(Household(), presence=HA_PERSONS)))
        self.assertNotIn("presence_now", names(tools_for(Household(), presence={"locations": {}, "home_status": {}})))

    def test_the_tool_list_follows_a_household_swapped_live(self):
        tools = tools_for(Household())
        self.assertNotIn("camera_look", names(tools))
        tools.deps.household = TEST_HOUSEHOLD                      # what the setup wizard does after a save
        self.assertIn("camera_look", names(tools))
        self.assertEqual(tools.specs["camera_look"]["parameters"]["properties"]["camera"]["enum"][0], "living_room")

    def test_no_empty_enum_is_sent(self):
        tool = next(t for t in tools_for(Household(), presence=HA_PERSONS).openai_tools() if t["function"]["name"] == "presence_now")
        self.assertNotIn("enum", tool["function"]["parameters"]["properties"]["who"])      # the model may name anyone

    def test_an_unavailable_tool_is_refused_with_its_reason(self):
        err = tools_for(Household()).validate("camera_look", {"camera": "kitchen"})
        self.assertIn("unknown tool", err)
        self.assertIn("at least one camera", err)


class TestCameraArguments(unittest.TestCase):
    def test_unknown_camera_lists_the_real_ones(self):
        err = tools_for(TEST_HOUSEHOLD).validate("camera_look", {"camera": "garage"})
        self.assertIn("unknown camera 'garage'", err)
        for key in TEST_HOUSEHOLD.cameras:
            self.assertIn(key, err)

    def test_scan_lists_only_pan_cameras(self):
        err = tools_for(TEST_HOUSEHOLD).validate("camera_scan", {"camera": "side_yard"})
        self.assertIn("living_room", err)
        self.assertNotIn("side_yard (", err)

    def test_display_names_and_a_camera_suffix_are_understood(self):
        tools = tools_for(TEST_HOUSEHOLD)
        for label in ("Living Room", "living_room_camera", "LIVING ROOM CAMERA", "living_room"):
            args = {"camera": label}
            self.assertIsNone(tools.validate("camera_look", args), label)
            self.assertEqual(args["camera"], "living_room")


class TestPromptNamesOnlyWhatExists(unittest.TestCase):
    def test_persona_without_cameras_phones_or_speakers(self):
        text = render_persona(Household(), ha=True)
        for word in ("camera", "PTZ", "notification", "announcement", "presence_now"):
            self.assertNotIn(word, text)
        self.assertIn("device states", text)

    def test_persona_for_a_full_household_is_unchanged(self):
        text = render_persona(TEST_HOUSEHOLD)
        for word in ("what a camera shows", "PTZ cameras", "notifications, announcements", "presence_now"):
            self.assertIn(word, text)

    def test_fresh_facts_and_card(self):
        self.assertEqual(fresh_facts(["ha_get_states", "presence_now", "camera_look", "memory_search"]),
                         ("For the next message: device states, temperatures, who is where, camera views and household notes "
                          "(cars, preferences, past events) must come from a tool call you make now, not from earlier replies "
                          "or guesses. A greeting or small talk needs no tools."))
        self.assertNotIn("camera", fresh_facts(["ha_get_states", "memory_search"]))
        self.assertEqual(fresh_facts([]), "")
        self.assertNotIn("camera", build_presence_card({}, cameras=False))
        self.assertIn("camera_look", build_presence_card({}))

    def test_the_request_to_the_model_has_no_camera_words_without_cameras(self):
        llm = ScriptedLLM(reply("Fine."))
        agent = CourageAgent(tools_for(Household()), "http://fake:8001/v1", pending=PendingActions(), post=llm,
                             presence_fn=lambda: {})
        agent.persona_fn = lambda: render_persona(Household())
        list(agent.run([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."},
                        {"role": "user", "content": "thanks"}], "s"))
        sent = json.dumps(llm.requests[0]).lower()
        self.assertNotIn("camera", sent)


class TestIsAnyoneHome(unittest.TestCase):
    def agent(self, llm, household):
        return CourageAgent(tools_for(household, presence=HA_PERSONS), "http://fake:8001/v1", pending=PendingActions(), post=llm,
                            presence_fn=lambda: HA_PERSONS)

    def test_empty_household_answers_from_ha_person_states(self):
        llm = ScriptedLLM(tool_call("presence_now", {}), reply("Alex is home."))
        events = list(self.agent(llm, Household()).run([{"role": "user", "content": "Is anyone home?"}], "s"))
        self.assertEqual(events[-1]["content"], "Alex is home.")
        result = next(e["result"] for e in events if e.get("type") == "tool_result")
        self.assertIn("alex", result)
        self.assertNotIn("error", result)
        self.assertIn("Alex: home", json.dumps(llm.requests[0]))          # the card lists the HA person too
        self.assertNotIn("camera_look", json.dumps(llm.requests[0]["tools"]))

    def test_presence_with_a_name_and_no_cameras_does_not_crash(self):
        tools = tools_for(Household(people=["Alex"]), presence={"locations": {}, "home_status": {}})
        out = json.loads(tools.execute("presence_now", {"who": "alex"}))
        self.assertTrue(out["ok"])
        self.assertNotIn("looked_now", out)


CAPTURED = '{"name": "camera_look", "arguments": {"camera": "living_room_camera"}}'


class TestJsonToolCalls(unittest.TestCase):
    NAMES = {"camera_look", "presence_now"}

    def test_shapes(self):
        for text in (CAPTURED,
                     "```json\n" + CAPTURED + "\n```",
                     '{"name": "camera_look", "parameters": {"camera": "x"}}',
                     '{"tool": "camera_look", "arguments": {"camera": "x"}}',
                     '[{"name": "camera_look", "arguments": {"camera": "x"}}]',
                     '<tool_call>\n{"name": "camera_look", "arguments": {"camera": "x"}}\n</tool_call>',
                     '{"name": "camera_look", "arguments": "{\\"camera\\": \\"x\\"}"}'):
            calls, shaped = json_tool_calls(text, self.NAMES)
            self.assertTrue(shaped, text)
            self.assertEqual([c["function"]["name"] for c in calls], ["camera_look"], text)
            self.assertIn("camera", json.loads(calls[0]["function"]["arguments"]))

    def test_two_calls_in_a_list(self):
        calls, _ = json_tool_calls('[{"name": "presence_now", "arguments": {}}, {"name": "camera_look", "arguments": {"camera": "a"}}]',
                                   self.NAMES)
        self.assertEqual(len(calls), 2)

    def test_unknown_tool_is_shaped_but_not_run(self):
        self.assertEqual(json_tool_calls('{"name": "teleport", "arguments": {}}', self.NAMES), ([], True))
        self.assertEqual(json_tool_calls('[{"name": "presence_now", "arguments": {}}, {"name": "teleport", "arguments": {}}]',
                                         self.NAMES), ([], True))

    def test_ordinary_text_and_data_are_not_calls(self):
        for text in ("Alex is home.", '{"temperature": 70}', '{"name": "Alex", "age": 3}', "[1, 2]", "", "{broken"):
            self.assertEqual(json_tool_calls(text, self.NAMES), ([], False), text)


class TestRawToolJsonIsNeverTheAnswer(unittest.TestCase):
    def test_the_captured_call_is_run_as_a_tool_call(self):
        looks = []
        llm = ScriptedLLM(reply(CAPTURED), reply("Nobody, just a cat."))
        agent, _ = agent_fixture.make_agent(llm, looks=looks)
        events = agent_fixture.run(agent, "Is anyone home?")
        self.assertEqual(events[-1]["content"], "Nobody, just a cat.")
        self.assertEqual(looks, ["camera.living_room_hd_stream"])       # the invented "living_room_camera" meant living_room
        self.assertNotIn("camera_look", events[-1]["content"])

    def test_an_unknown_tool_is_nudged_then_answered_in_words(self):
        llm = ScriptedLLM(reply('{"name": "living_room_scanner", "arguments": {}}'), reply("I can not see anyone."))
        agent, _ = agent_fixture.make_agent(llm)
        events = agent_fixture.run(agent, "Is anyone home?")
        self.assertEqual(events[-1]["content"], "I can not see anyone.")
        self.assertIn("does not exist", llm.requests[1]["messages"][-1]["content"])

    def test_unknown_twice_never_shows_the_json(self):
        bad = '{"name": "living_room_scanner", "arguments": {}}'
        llm = ScriptedLLM(reply(bad), reply(bad))
        agent, _ = agent_fixture.make_agent(llm)
        events = agent_fixture.run(agent, "Is anyone home?")
        self.assertNotIn("{", events[-1]["content"])
        self.assertTrue(events[-1]["content"])


if __name__ == "__main__":
    unittest.main()
