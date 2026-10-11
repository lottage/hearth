"""Fakes for the agent loop's tests (platform layer): a scripted llama server, a fake Home Assistant, and an agent
built on the test household. Home's tests add Home's reflex (tests/test_courage_agent.py)."""

import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage.courage import CourageAgent, CourageDeps, CourageTools, PendingActions  # noqa: E402
from household_fixture import TEST_HOUSEHOLD  # noqa: E402

PRESENCE = {"locations": {"bella": {"last_seen": "01:23 PM", "minutes_ago": 12.0, "camera": "Living Room"},
                          "alex": {"last_seen": "01:43 PM", "minutes_ago": 3.0}},
            "recent_sightings": [{"entity": "Bella", "timestamp": "01:23 PM"}]}


class FakeHA:
    def __init__(self):
        self.calls = []

    def states(self, domain):
        ents = [{"entity_id": "light.kitchen", "friendly_name": "Kitchen Light", "state": "on", "attributes": {"brightness": 200}},
                {"entity_id": "light.bedroom", "friendly_name": "Bedroom Lamp", "state": "off", "attributes": {}}]
        return {"ok": True, "entities": [e for e in ents if e["entity_id"].startswith(f"{domain}.")]}

    def call(self, domain, service, data):
        self.calls.append((domain, service, data))
        return {"ok": True}


def tool_call(name, args, cid="c1"):
    return {"choices": [{"message": {"content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


def reply(text):
    return {"choices": [{"message": {"content": text}}]}


class ScriptedLLM:
    """Returns queued responses and records every request body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, body, timeout):
        self.requests.append(copy.deepcopy(body))  # the agent keeps appending to its message list
        if not self.responses:
            raise AssertionError("LLM called more times than scripted")
        return self.responses.pop(0)


def make_agent(llm, ha=None, pending=None, looks=None, reflex=None):
    ha = ha or FakeHA()
    looks = looks if looks is not None else []
    deps = CourageDeps(
        ha_states=ha.states, ha_call=ha.call, presence=lambda: PRESENCE,
        camera_look=lambda eid, name, **kw: looks.append(eid) or f"{name}: a black and white cat on the couch.",
        camera_scan=lambda eid, name: f"{name}: scanned 3 presets, nobody there.", household=TEST_HOUSEHOLD)
    agent = CourageAgent(CourageTools(deps), "http://fake:8001/v1", presence_fn=lambda: PRESENCE,
                         pending=pending or PendingActions(), post=llm)
    agent.reflex_resolve = reflex         # an app's on/off reflex (Home passes its own); None = none
    return agent, ha


def run(agent, text, session="s1", history=None):
    return list(agent.run((history or []) + [{"role": "user", "content": text}], session))
