"""Offline tests for the Courage tool loop: fake llama server + fake Home Assistant, no network."""

import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage.courage import CourageAgent, CourageDeps, CourageTools, PendingActions  # noqa: E402
from household_fixture import TEST_HOUSEHOLD  # noqa: E402

from agent_fixture import PRESENCE, FakeHA, ScriptedLLM, reply, run, tool_call  # noqa: E402,F401
import agent_fixture  # noqa: E402


def make_agent(llm, ha=None, pending=None, looks=None):
    """The agent as the server wires it with the Home app: Home's on/off reflex injected."""
    from hearth.reflex import resolve
    return agent_fixture.make_agent(llm, ha, pending, looks, reflex=resolve)


class TestCourageAgent(unittest.TestCase):
    def test_is_bella_inside_uses_presence(self):
        llm = ScriptedLLM(tool_call("presence_now", {}), reply("Bella was on the kitchen camera 12 minutes ago."))
        agent, _ = make_agent(llm)
        events = run(agent, "Is Bella inside?")
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "final"])
        self.assertIn('"bella"', events[1]["result"])
        self.assertIn("12 minutes", events[-1]["content"])
        # tool result was fed back with the matching id
        tool_msg = llm.requests[1]["messages"][-1]
        self.assertEqual((tool_msg["role"], tool_msg["tool_call_id"]), ("tool", "c1"))
        # thinking off, native tools on
        self.assertEqual(llm.requests[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(len(llm.requests[0]["tools"]), 9)                  # + correct_sighting (2026-09-27)

    def test_presence_card_right_after_the_persona(self):
        """2026-09-28: the clocked card moved out of the first system message into the second, so the persona + tool
        schemas stay a cacheable prefix (a one-minute clock change used to re-process ~1,260 tokens, ~2.5 s)."""
        llm = ScriptedLLM(reply("Hello."))
        agent, _ = make_agent(llm)
        run(agent, "hi")
        msgs = llm.requests[0]["messages"]
        self.assertNotIn("[Who is where", msgs[0]["content"])          # the cached prefix never changes
        self.assertEqual(msgs[1]["role"], "system")
        card = msgs[1]["content"]
        self.assertIn("[Who is where", card)
        self.assertIn("Bella: seen 12 min ago on Living Room", card)
        self.assertIn("Sam: no recent sighting", card)
        self.assertEqual(msgs[2], {"role": "user", "content": "hi"})

    def test_camera_look_runs_freely(self):
        looks = []
        llm = ScriptedLLM(tool_call("camera_look", {"camera": "living_room"}), reply("Bella's on the couch."))
        agent, _ = make_agent(llm, looks=looks)
        events = run(agent, "what's Bella doing?")
        self.assertEqual(looks, ["camera.living_room_hd_stream"])
        self.assertEqual(events[0]["status"], "Looking at the living room camera…")

    def test_action_waits_for_approval_then_runs_on_yes(self):
        pending = PendingActions()
        ha = FakeHA()
        llm = ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}))
        agent, _ = make_agent(llm, ha=ha, pending=pending)
        events = run(agent, "it's far too bright in the kitchen")  # inferred, so Courage asks
        self.assertEqual([e["type"] for e in events], ["approval_required", "final"])
        self.assertEqual(ha.calls, [], "nothing may run before approval")
        self.assertEqual(events[-1]["content"], "Shall I turn off the Kitchen Light? Say yes to approve.")

        agent.post = ScriptedLLM(reply("Done. The kitchen is dark."))
        events = run(agent, "yes")
        self.assertEqual(ha.calls, [("light", "turn_off", {"entity_id": "light.kitchen"})])
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "final"])
        self.assertIsNone(pending.get("s1"))

    def test_no_cancels_pending_action(self):
        pending = PendingActions()
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom"})),
                              ha=ha, pending=pending)
        run(agent, "the bedroom is pitch dark")
        events = run(agent, "no, leave it")
        self.assertEqual(events, [{"type": "final", "content": "Right. Leaving it alone."}])
        self.assertEqual(ha.calls, [])

    def test_approval_is_per_session(self):
        pending = PendingActions()
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom"})),
                              ha=ha, pending=pending)
        run(agent, "the bedroom is pitch dark", session="phone")
        agent.post = ScriptedLLM(reply("Yes to what, exactly?"))
        run(agent, "yes", session="other-tab")
        self.assertEqual(ha.calls, [])

    def test_disallowed_service_is_refused_without_asking(self):
        ha = FakeHA()
        llm = ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "delete", "entity_id": "light.kitchen"}),
                          reply("I'm not permitted to do that."))
        agent, _ = make_agent(llm, ha=ha)
        events = run(agent, "delete the kitchen light")
        self.assertEqual(events[0]["type"], "tool_result")
        self.assertIn("not on Computer's allowlist", events[0]["result"])
        self.assertEqual(ha.calls, [])

    def test_made_up_entity_is_refused_with_candidates(self):
        ha = FakeHA()
        llm = ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen_light"}),
                          reply("There's no such light."))
        agent, _ = make_agent(llm, ha=ha)
        events = run(agent, "turn off the kitchen light, it is late")
        self.assertEqual(events[0]["type"], "tool_result")
        self.assertIn("no entity 'light.kitchen_light'", events[0]["result"])
        self.assertIn("light.kitchen", events[0]["result"])
        self.assertEqual(ha.calls, [])

    def test_only_presence_looks_are_people_only(self):
        """camera_look questions can be about anything, so only presence_now's stale-sighting look may skip vision."""
        calls = []
        look = lambda eid, name, **kw: calls.append(kw) or "ok"
        tools = CourageTools(CourageDeps(ha_states=None, ha_call=None, presence=lambda: {}, camera_look=look, camera_scan=None,
                                         household=TEST_HOUSEHOLD))
        tools._camera_look("living_room")
        tools._presence_now("bella")
        self.assertEqual(calls, [{}, {"people_only": True}])

    def test_entity_domain_mismatch_refused(self):
        tools = CourageTools(CourageDeps(ha_states=None, ha_call=None, presence=None, camera_look=None, camera_scan=None))
        self.assertIsNotNone(tools.validate("ha_call", {"domain": "light", "service": "turn_on", "entity_id": "lock.front_door"}))

    def test_ha_get_states_filters_by_name(self):
        llm = ScriptedLLM(tool_call("ha_get_states", {"domain": "light", "name_contains": "kitchen"}), reply("Kitchen light is on."))
        agent, _ = make_agent(llm)
        events = run(agent, "is the kitchen light on?")
        result = json.loads(events[1]["result"])
        self.assertEqual([e["entity_id"] for e in result["entities"]], ["light.kitchen"])

    def test_filler_filter_does_not_hide_entities(self):
        agent, _ = make_agent(ScriptedLLM())
        for junk in ("any", "all lights", "lights"):
            res = json.loads(agent.tools.execute("ha_get_states", {"domain": "light", "name_contains": junk}))
            self.assertEqual(res["count"], 2, junk)
        res = json.loads(agent.tools.execute("ha_get_states", {"domain": "light", "name_contains": "garage"}))
        self.assertEqual(res["count"], 2)
        self.assertIn("no light names matched", res["note"])

    def test_llm_down_gives_plain_error(self):
        def boom(url, body, timeout):
            raise ConnectionRefusedError("refused")
        agent, _ = make_agent(boom)
        events = run(agent, "hello")
        self.assertIn("isn't answering", events[-1]["content"])

    def test_history_strips_tool_markup_and_think(self):
        llm = ScriptedLLM(reply("ok"))
        agent, _ = make_agent(llm)
        hist = [{"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "<think>hmm</think>:::TOOL_CALL:::x:::{}:::END_TOOL_CALL:::Bella is in."}]
        run(agent, "and now?", history=hist)
        sent = llm.requests[0]["messages"]
        self.assertEqual(sent[3], {"role": "assistant", "content": "Bella is in."})   # persona, card, "earlier", this

    def test_sse_format(self):
        agent, _ = make_agent(ScriptedLLM(reply("Morning.")))
        chunks = list(agent.sse([{"role": "user", "content": "hi"}], "s"))
        self.assertTrue(all(c.startswith("data: ") and c.endswith("\n\n") for c in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")
        first = json.loads(chunks[0][6:])
        self.assertEqual(first["choices"][0]["delta"]["content"], "Morning.")

    def test_promise_without_tool_call_gets_one_nudge(self):
        llm = ScriptedLLM(reply("I'll check the thermostat for you. One moment."),
                          tool_call("ha_get_states", {"domain": "climate"}), reply("It's 68 in here."))
        agent, _ = make_agent(llm)
        events = run(agent, "is it cold in here?")
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "final"])
        self.assertIn("described a tool call", llm.requests[1]["messages"][-1]["content"])

    def test_only_one_nudge(self):
        llm = ScriptedLLM(reply("Let me check that."), reply("Let me check that again."))
        agent, _ = make_agent(llm)
        events = run(agent, "hmm?")
        self.assertEqual(events, [{"type": "final", "content": "Let me check that again."}])

    def test_step_limit(self):
        llm = ScriptedLLM(*[tool_call("presence_now", {}, cid=f"c{i}") for i in range(5)])
        agent, _ = make_agent(llm)
        events = run(agent, "loop forever")
        self.assertIn("round in circles", events[-1]["content"])

    def test_direct_command_runs_without_asking(self):
        ha = FakeHA()
        llm = ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}),
                          {"choices": [{"message": {"content": "Done."}}], "timings": {"predicted_n": 40, "predicted_ms": 1000}})
        agent, _ = make_agent(llm, ha=ha)
        events = run(agent, "turn off the kitchen light, it is late")
        self.assertEqual(ha.calls, [("light", "turn_off", {"entity_id": "light.kitchen"})])
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "usage", "final"])
        self.assertEqual((events[2]["completion_tokens"], events[2]["tps"]), (40, 40.0))

    def test_direct_word_must_match_the_service(self):
        tools = CourageTools(CourageDeps(ha_states=None, ha_call=None, presence=None, camera_look=None, camera_scan=None))
        on = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}
        self.assertTrue(tools.is_direct_command("ha_call", on, "turn the kitchen light on"))
        self.assertTrue(tools.is_direct_command("ha_call", on, "lamp on"))
        self.assertFalse(tools.is_direct_command("ha_call", on, "it's dark in here"))
        self.assertFalse(tools.is_direct_command("ha_call", on, "is the kitchen light on?"))
        self.assertFalse(tools.is_direct_command("ha_call", {"domain": "climate", "service": "set_temperature",
                                                             "entity_id": "climate.thermostat"}, "it's cold in here"))
        self.assertTrue(tools.is_direct_command("speak", {"message": "dinner"}, "announce dinner is ready"))

    def test_unlock_always_asks(self):
        ha = FakeHA()
        ha.states = lambda d: {"ok": True, "entities": [{"entity_id": "lock.front_door", "friendly_name": "Front Door", "state": "locked"}]}
        agent, _ = make_agent(ScriptedLLM(tool_call("ha_call", {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"})), ha=ha)
        events = run(agent, "unlock the front door")
        self.assertEqual(events[0]["type"], "approval_required")
        self.assertEqual(ha.calls, [])

    def test_find_stale_subject_looks_through_last_camera(self):
        looks = []
        llm = ScriptedLLM(tool_call("presence_now", {"who": "bella"}), reply("Bella is on the couch."))
        agent, _ = make_agent(llm, looks=looks)
        events = run(agent, "where is bella")
        self.assertEqual(looks, ["camera.living_room_hd_stream"])  # 12 min old > STALE_MINUTES
        self.assertIn("looked_now", events[1]["result"])

    def test_find_fresh_subject_does_not_look(self):
        looks = []
        llm = ScriptedLLM(tool_call("presence_now", {"who": "alex"}), reply("Alex was in 3 minutes ago."))
        agent, _ = make_agent(llm, looks=looks)
        run(agent, "where is alex")
        self.assertEqual(looks, [])

    def test_offer_to_check_is_nudged_into_a_call(self):
        llm = ScriptedLLM(reply("Bella was last seen a while ago. Would you like me to check the cameras?"),
                          tool_call("camera_look", {"camera": "living_room"}), reply("She's on the couch."))
        agent, _ = make_agent(llm)
        events = run(agent, "where is bella")
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "final"])

    def test_trailing_offer_is_trimmed(self):
        llm = ScriptedLLM(reply("Bella is in the living room. Would you like me to check anything else?"))
        agent, _ = make_agent(llm)
        self.assertEqual(run(agent, "thanks")[-1]["content"], "Bella is in the living room.")


if __name__ == "__main__":
    unittest.main()


class TestCourageReflex(unittest.TestCase):
    """Bare on/off orders skip the LLM; everything else must fall through to it."""

    def setUp(self):
        from hearth import reflex
        self.reflex = reflex
        self.ents = [
            {"entity_id": "light.living_room_tv_lights", "friendly_name": "Living Room TV Lights", "state": "on", "domain": "light"},
            {"entity_id": "light.driveway_floodlight", "friendly_name": "Driveway Floodlight (Timed)", "state": "off", "domain": "light"},
            {"entity_id": "switch.garden_lights", "friendly_name": "Garden Lights", "state": "off", "domain": "switch"},
        ]

    def test_parse(self):
        p = self.reflex.parse
        self.assertEqual(p("Turn off the TV lights"), {"state": "off", "name": "the tv lights"})
        self.assertEqual(p("garden lights on please"), {"state": "on", "name": "garden lights"})
        self.assertEqual(p("could you switch the floodlight off?"), {"state": "off", "name": "the floodlight"})
        for text in ("is the tv light on?", "it's dark in here", "turn off the lights in ten minutes and lock up", ""):
            self.assertIsNone(p(text), text)

    def test_match_needs_exactly_one(self):
        m = lambda name: self.reflex.match({"state": "off", "name": name}, self.ents)  # noqa: E731
        self.assertEqual(m("tv light")["entity_id"], "light.living_room_tv_lights")
        self.assertEqual(m("driveway floodlight")["entity_id"], "light.driveway_floodlight")
        self.assertIsNone(m("lights"))  # ambiguous
        self.assertIsNone(m("bedroom lamp"))  # unknown

    def test_camera_and_server_switches_are_not_candidates(self):
        states = {"switch": {"ok": True, "entities": [
            {"entity_id": "switch.living_room_privacy", "friendly_name": "Living Room Privacy", "state": "off"},
            {"entity_id": "switch.plug_led", "friendly_name": "Plug LED", "state": "on"},
            {"entity_id": "switch.garden_lights", "friendly_name": "Garden Lights", "state": "off"}]}}
        names = [e["friendly_name"] for e in self.reflex.candidates(lambda d: states.get(d, {"ok": True, "entities": []}))]
        self.assertEqual(names, ["Garden Lights"])

    def test_agent_runs_reflex_without_llm(self):
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(), ha=ha)  # any LLM call would raise
        events = run(agent, "turn off the kitchen light")
        self.assertEqual(ha.calls, [("light", "turn_off", {"entity_id": "light.kitchen"})])
        self.assertEqual(events[-1]["content"], "Kitchen Light off.")

    def test_reflex_already_in_state(self):
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(), ha=ha)
        events = run(agent, "bedroom lamp off")
        self.assertEqual(ha.calls, [])
        self.assertEqual(events[-1]["content"], "The Bedroom Lamp is already off.")

    def test_without_the_home_reflex_injected_the_loop_decides(self):
        # the platform loop has no home imports: an agent that is not given the home reflex sends even a bare order to the model
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(reply("Which light do you mean?")), ha=ha)
        agent.reflex_resolve = None
        events = run(agent, "turn off the kitchen light")
        self.assertEqual(ha.calls, [])
        self.assertEqual(events[-1]["content"], "Which light do you mean?")


class LaggingHA(FakeHA):
    """Home Assistant reports a change about a second AFTER the service call returns (measured 2026-09-29)."""

    def __init__(self, lag_applied=False):
        super().__init__()
        self.lag_applied = lag_applied
        self.pending = {}                      # entity_id -> state it will report once the lag is over

    def call(self, domain, service, data):
        self.calls.append((domain, service, data))
        self.pending[data["entity_id"]] = "on" if service == "turn_on" else "off"
        return {"ok": True}

    def settle(self):
        """The lag is over: HA now reports what the commands left behind."""
        self.settled = dict(self.pending)

    def states(self, domain):
        res = super().states(domain)
        for e in res["entities"]:
            if getattr(self, "settled", {}).get(e["entity_id"]):
                e["state"] = self.settled[e["entity_id"]]
        return res


class TestStaleStateAfterOurOwnCommand(unittest.TestCase):
    """2026-09-29: "turn on the TV lights", then "turn off the TV lights" a second later answered "already off",
    because HA still reported the old state. The reflex stays model-free, but never trusts an "already" that our own
    last command contradicts."""

    def agent(self, ha):
        agent, _ = make_agent(ScriptedLLM(), ha=ha)    # any LLM call would raise: these are reflex turns
        self.now = 1000.0
        agent.tools.clock = lambda: self.now
        return agent

    def test_the_opposite_order_right_after_a_command_is_sent_not_dismissed(self):
        ha = LaggingHA()
        agent = self.agent(ha)
        run(agent, "turn on the bedroom lamp")             # HA (lagging) still says off
        self.now += 1.0
        events = run(agent, "turn off the bedroom lamp")   # HA still says off: "already off" would be stale
        self.assertEqual([c[1] for c in ha.calls], ["turn_on", "turn_off"])
        self.assertEqual(events[-1]["content"], "Bedroom Lamp off.")

    def test_a_genuine_already_is_still_answered(self):
        ha = LaggingHA()
        agent = self.agent(ha)
        events = run(agent, "turn off the bedroom lamp")   # nothing sent by us: HA's state is the truth
        self.assertEqual(ha.calls, [])
        self.assertEqual(events[-1]["content"], "The Bedroom Lamp is already off.")

    def test_after_the_window_a_reading_is_trusted_again(self):
        ha = LaggingHA()
        agent = self.agent(ha)
        run(agent, "turn on the bedroom lamp")
        self.now += 11.0                                   # past RECENT_COMMAND_S; the command evidently did not stick
        events = run(agent, "turn off the bedroom lamp")
        self.assertEqual([c[1] for c in ha.calls], ["turn_on"])
        self.assertIn("already off", events[-1]["content"])

    def test_repeating_the_same_order_is_answered_once_home_assistant_has_caught_up(self):
        ha = LaggingHA()
        agent = self.agent(ha)
        run(agent, "turn on the bedroom lamp")
        ha.settle()
        self.now += 1.0
        events = run(agent, "turn on the bedroom lamp")
        self.assertEqual([c[1] for c in ha.calls], ["turn_on"])
        self.assertIn("already on", events[-1]["content"])

    def test_a_failed_command_is_not_remembered(self):
        ha = LaggingHA()
        ha.call = lambda d, s, data: ha.calls.append((d, s, data)) or {"ok": False, "error": "offline"}
        agent = self.agent(ha)
        run(agent, "turn on the bedroom lamp")
        self.assertFalse(agent.tools.changed_just_now("light.bedroom", "off"))

    def test_a_toggle_leaves_the_state_unknown(self):
        ha = LaggingHA()
        agent = self.agent(ha)
        agent.tools.execute("ha_call", {"domain": "light", "service": "toggle", "entity_id": "light.bedroom"})
        self.assertTrue(agent.tools.changed_just_now("light.bedroom", "off"))
        self.assertTrue(agent.tools.changed_just_now("light.bedroom", "on"))


class TestPushApprovals(unittest.TestCase):
    """Approve from the phone: Yes/No buttons on an HA notification (fake HA, no network)."""

    def setUp(self):
        from stonesage.courage.push_approvals import PushApprovals
        self.sent, self.ran = [], []
        self.pending = PendingActions()
        self.push = PushApprovals("http://ha:8123", "t", "mobile_app_phone", self.pending,
                                  execute=lambda n, a: self.ran.append((n, a)) or '{"ok": true}',
                                  post=lambda path, body: self.sent.append((path, body)))

    def park(self, session="ha:192.0.2.82"):
        return self.pending.put(session, {"name": "ha_call", "args": {"domain": "light", "service": "turn_off",
                                                                       "entity_id": "light.kitchen"}, "summary": "turn off the Kitchen Light"})

    def test_policy(self):
        self.assertTrue(self.push.wants_push("ha:192.0.2.82"))
        self.assertTrue(self.push.wants_push("alexa:3f9a1c0b7d22"))      # an Echo session can end before the yes
        self.assertFalse(self.push.wants_push("web-session-1"))
        self.push.policy = "always"
        self.assertTrue(self.push.wants_push("web-session-1"))

    def test_offer_sends_buttons(self):
        a = self.park()
        self.assertTrue(self.push.offer("ha:192.0.2.82", a))
        path, body = self.sent[0]
        self.assertEqual(path, "/api/services/notify/mobile_app_phone")
        self.assertEqual([b["action"] for b in body["data"]["actions"]], [f"COURAGE_YES_{a['id']}", f"COURAGE_NO_{a['id']}"])

    def test_yes_runs_the_parked_action_once(self):
        a = self.park()
        self.assertEqual(self.push.handle_action(f"COURAGE_YES_{a['id']}"), "Done: turn off the Kitchen Light.")
        self.assertEqual(self.ran, [("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})])
        self.assertIn("expired", self.push.handle_action(f"COURAGE_YES_{a['id']}"))  # second tap does nothing
        self.assertEqual(len(self.ran), 1)
        self.assertNotIn("actions", self.sent[-1][1]["data"])  # outcome replaces the buttons

    def test_no_drops_it(self):
        a = self.park()
        self.assertIn("Left alone", self.push.handle_action(f"COURAGE_NO_{a['id']}"))
        self.assertEqual(self.ran, [])
        self.assertIsNone(self.pending.get("ha:192.0.2.82"))

    def test_foreign_actions_ignored(self):
        self.assertIsNone(self.push.handle_action("SOME_OTHER_APP_ACTION"))

    def test_agent_mentions_the_phone(self):
        ha = FakeHA()
        agent, _ = make_agent(ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})), ha=ha)
        agent.on_approval = lambda sid, action: sid.startswith("ha:")
        events = run(agent, "it's far too bright in the kitchen", session="ha:192.0.2.82")
        self.assertIn("tap Yes on your phone", events[-1]["content"])


class TestLearnedReflexes(unittest.TestCase):
    """A phrasing the loop resolved into one direct on/off order is replayed next time without the LLM."""

    def setUp(self):
        import tempfile
        from hearth.reflex import LearnedReflexes
        self.path = os.path.join(tempfile.mkdtemp(), "reflexes.json")
        self.store = LearnedReflexes(self.path)

    def agent(self, llm, ha):
        agent, _ = make_agent(llm, ha=ha)
        agent.learned = self.store
        return agent

    def test_learn_then_replay_without_llm(self):
        ha = FakeHA()
        llm = ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}), reply("Done."))
        run(self.agent(llm, ha), "kill the kitchen light")          # not a bare on/off sentence: the LLM resolves it
        self.assertEqual(ha.calls, [("light", "turn_off", {"entity_id": "light.kitchen"})])
        self.assertIn("kill kitchen light", self.store.items)
        ha2 = FakeHA()
        ha2.states = lambda d: {"ok": True, "entities": [{"entity_id": "light.kitchen", "friendly_name": "Kitchen Light", "state": "on"}]}
        events = run(self.agent(ScriptedLLM(), ha2), "Please kill the kitchen light!")  # any LLM call would fail
        self.assertEqual(ha2.calls, [("light", "turn_off", {"entity_id": "light.kitchen"})])
        self.assertEqual(events[-1]["content"], "Kitchen Light off.")
        self.assertEqual(self.store.items["kill kitchen light"]["hits"], 1)

    def test_not_learned_from_approvals_or_multi_actions(self):
        ha = FakeHA()
        llm = ScriptedLLM(tool_call("ha_call", {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}))
        run(self.agent(llm, ha), "it's far too bright in here")        # inferred: asks, never learned
        self.assertEqual(self.store.items, {})
        two = {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "ha_call", "arguments": json.dumps({"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"})}},
            {"id": "b", "type": "function", "function": {"name": "ha_call", "arguments": json.dumps({"domain": "light", "service": "turn_off", "entity_id": "light.bedroom"})}}]}}]}
        run(self.agent(ScriptedLLM(two, reply("Both off.")), FakeHA()), "kill everything off")
        self.assertEqual(self.store.items, {})

    def test_refuses_compound_questions_and_data(self):
        args = {"domain": "light", "service": "turn_off", "entity_id": "light.kitchen"}
        self.assertFalse(self.store.learn("kill the lights in ten minutes", args, "x"))
        self.assertFalse(self.store.learn("kill the lights and the fan", args, "x"))
        self.assertFalse(self.store.learn("is the light off", args, "x"))
        self.assertFalse(self.store.learn("warm it up", {"domain": "climate", "service": "set_temperature",
                                                         "entity_id": "climate.x", "data": {"temperature": 72}}, "x"))
        self.assertTrue(self.store.learn("lights out in the kitchen please", args, "Kitchen Light") is False)  # "in" = compound

    def test_forgets_when_device_disappears(self):
        self.store.learn("kill the pantry light", {"domain": "light", "service": "turn_off", "entity_id": "light.pantry"}, "Pantry")
        ha = FakeHA()  # has no light.pantry
        agent = self.agent(ScriptedLLM(reply("There is no pantry light.")), ha)
        run(agent, "kill the pantry light")
        self.assertNotIn("kill pantry light", self.store.items)
        self.assertEqual(ha.calls, [])

    def test_persists(self):
        from hearth.reflex import LearnedReflexes
        self.store.learn("lamp off now", {"domain": "light", "service": "turn_off", "entity_id": "light.bedroom"}, "Bedroom Lamp")
        self.assertIn("lamp off", LearnedReflexes(self.path).items)

class TestCorrectSighting(unittest.TestCase):
    """correct_sighting: the owner saying the name is wrong runs at once; Computer spotting it himself asks first."""

    def make(self, llm):
        agent, _ = make_agent(llm)
        calls = []
        agent.tools.deps.correct_sighting = lambda who, action, name: calls.append((who, action, name)) or {
            "ok": True, "corrected": "Rex on the Living Room, 12 min ago", "now": "filed under Bella",
            "Rex_latest_now": "40 min ago on Driveway"}
        return agent, calls

    def test_johns_correction_runs_at_once(self):
        llm = ScriptedLLM(tool_call("correct_sighting", {"who": "rex", "action": "is_really", "name": "bella"}),
                          reply("Filed under Bella. Rex's latest is now 40 minutes ago on the driveway."))
        agent, calls = self.make(llm)
        events = run(agent, "That wasn't Rex on the kitchen camera, it was Bella")
        self.assertEqual(calls, [("rex", "is_really", "bella")])
        self.assertEqual([e["type"] for e in events], ["tool_call", "tool_result", "final"])
        self.assertIn("40 min ago", llm.requests[1]["messages"][-1]["content"])   # the result reached the model

    def test_an_inferred_correction_asks_first(self):
        llm = ScriptedLLM(tool_call("correct_sighting", {"who": "rex", "action": "wrong"}))
        agent, calls = self.make(llm)
        events = run(agent, "Are you sure the dog in the kitchen is Rex?")
        self.assertEqual(calls, [])
        self.assertEqual(events[0]["type"], "approval_required")
        self.assertIn("hide Rex's latest camera sighting", events[-1]["content"])

    def test_is_really_needs_a_name(self):
        agent, calls = self.make(ScriptedLLM())
        self.assertIn("name", agent.tools.validate("correct_sighting", {"who": "rex", "action": "is_really"}))
        self.assertIsNone(agent.tools.validate("correct_sighting", {"who": "rex", "action": "wrong"}))


class TestQuestionsAreAnswered(unittest.TestCase):
    """2026-09-28: "Is the kitchen light on?" read the state and then offered to turn something on. A question gets an
    answer, never an action or an offer (tests/live/test_courage_real_states.py runs this against the real house)."""

    LIGHT_ON = {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"}

    def test_is_question(self):
        from stonesage.courage.tools import is_question
        for text in ("Is the kitchen light on?", "is the kitchen light on", "What's the thermostat set to?",
                     "Are any lights on right now?", "Should I turn on the heat?", "Any packages today?", "Who is home",
                     "Do you know where Rex is?"):
            self.assertTrue(is_question(text), text)
        for text in ("Turn off the lamp", "Can you turn off the lamp?", "It's cold in here", "Hello?",
                     "Do you mind turning off the light?", "turn on the lights please", "Yes"):
            self.assertFalse(is_question(text), text)

    def test_a_question_is_never_a_direct_order(self):
        agent, _ = make_agent(ScriptedLLM())
        # no question mark (a voice transcript): "...light on" used to match the turn_on order pattern
        self.assertFalse(agent.tools.is_direct_command("ha_call", self.LIGHT_ON, "is the kitchen light on"))
        self.assertTrue(agent.tools.is_direct_command("ha_call", self.LIGHT_ON, "turn the kitchen light on"))

    def test_an_action_after_a_question_is_refused_and_the_loop_answers(self):
        llm = ScriptedLLM(tool_call("ha_get_states", {"domain": "light"}),
                          tool_call("ha_call", self.LIGHT_ON, cid="c2"),
                          reply("The kitchen light is on."))
        agent, ha = make_agent(llm)
        events = run(agent, "Is the kitchen light on?")
        self.assertEqual(ha.calls, [])
        self.assertNotIn("approval_required", [e["type"] for e in events])
        self.assertEqual(events[-1]["content"], "The kitchen light is on.")
        self.assertIn("asked a question", llm.requests[2]["messages"][-1]["content"])   # the model was told why
        self.assertIsNone(agent.pending.get("s1"))

    def test_a_question_without_a_question_mark_does_not_run_an_action_either(self):
        llm = ScriptedLLM(tool_call("ha_call", self.LIGHT_ON), reply("It is off."))
        agent, ha = make_agent(llm)
        events = run(agent, "is the kitchen light on")
        self.assertEqual(ha.calls, [])
        self.assertNotIn("approval_required", [e["type"] for e in events])

    def test_a_statement_still_asks_first_and_an_order_still_runs(self):
        agent, ha = make_agent(ScriptedLLM(tool_call("ha_call", self.LIGHT_ON)))
        events = run(agent, "It's dark in the kitchen")
        self.assertEqual(events[0]["type"], "approval_required")
        self.assertEqual(ha.calls, [])
        agent, ha = make_agent(ScriptedLLM(tool_call("ha_call", dict(self.LIGHT_ON, entity_id="light.bedroom")),
                                           reply("Done.")))
        events = run(agent, "Can you turn the bedroom lamp on?")     # a request, not a question (kitchen is already on)
        self.assertEqual([c[:2] for c in ha.calls], [("light", "turn_on")])
        self.assertNotIn("approval_required", [e["type"] for e in events])

    def test_a_closing_offer_to_act_is_cut_from_an_answer(self):
        llm = ScriptedLLM(tool_call("ha_get_states", {"domain": "light"}),
                          reply("The kitchen light is off. Would you like me to turn it on for you?"))
        agent, _ = make_agent(llm)
        self.assertEqual(run(agent, "Is the kitchen light on?")[-1]["content"], "The kitchen light is off.")

    def test_the_offer_is_not_a_promise_that_gets_nudged(self):
        # "Shall I ..." used to match PROMISE, so the loop demanded a tool call: that is how the light got offered
        llm = ScriptedLLM(reply("The light is off. Shall I turn it on?"))
        agent, _ = make_agent(llm)
        events = run(agent, "Is the light on?")
        self.assertEqual(events[-1]["content"], "The light is off.")
        self.assertEqual(len(llm.requests), 1)

    def test_other_turns_keep_their_offers(self):
        agent, _ = make_agent(ScriptedLLM(reply("It's cold out there. Want me to raise the heat?")))
        self.assertIn("Want me to raise the heat?", run(agent, "It's freezing in here")[-1]["content"])

    def test_a_reply_that_is_only_an_offer_is_not_emptied(self):
        from stonesage.courage.agent import _without_action_offer
        self.assertEqual(_without_action_offer("Shall I turn on the floodlight?"), "Shall I turn on the floodlight?")
        self.assertEqual(_without_action_offer("It's 72. Want me to raise it?"), "It's 72.")


if __name__ == "__main__":
    unittest.main()
