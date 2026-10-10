"""The household the Courage tests run against (the platform has none of its own since docs/LAYERS.md M2: the Home app
supplies it from config.json). Shaped like the homelab's, so the tests' names and cameras keep their meaning."""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "StoneSage", "backend"))

from stonesage.courage.household import Household  # noqa: E402

TEST_HOUSEHOLD = Household(
    people=["Alex", "Sam"],
    owner_pronouns=("he", "him"),
    pets={"Bella": "cat", "Rex": "dog"},
    cameras={
        "kitchen_living_room": {"entity": "camera.kitchen_living_room_hd_stream", "name": "Kitchen/Living Room"},
        "driveway_front_door": {"entity": "camera.driveway_front_door_hd_stream_direct", "name": "Driveway/Front Door"},
        "side_yard": {"entity": "camera.side_yard_hd_stream_direct", "name": "Side Yard"},
        "back_yard": {"entity": "camera.back_yard_hd_stream_direct", "name": "Back Yard"},
    },
    ptz_cameras=["kitchen_living_room", "driveway_front_door"],
    default_camera="kitchen_living_room",
    phones=["alex"],
    speakers=["kitchen", "bathroom", "everywhere"],
)


def live_household():
    """(household, persona) as the server builds them from config.json, for the live evals: they measure Computer as
    deployed. Falls back to TEST_HOUSEHOLD when the config has no household block."""
    from hearth.household import household_from_config
    from hearth.persona import render_persona
    path = os.environ.get("STONESAGE_CONFIG_FILE") or os.path.join(ROOT, "StoneSage", "backend", "config.json")
    try:
        cfg = json.load(open(path, encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    hh = household_from_config(cfg) if cfg.get("household") else TEST_HOUSEHOLD
    return hh, render_persona(hh)


def with_persona(agent, persona):
    """Give a test-built agent the persona the server gives it (soul.py's persona.md starts as Home's render)."""
    agent.persona_fn = lambda: persona
    return agent
