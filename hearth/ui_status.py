"""What the Home app tells the cockpit page about itself (the platform's `ui_features` hook, stonesage/ui_features.py).

Only facts about the configuration, read from config.json as it is on disk (never the example file, whose zones and
addresses belong to nobody): whether Home Assistant has an address and a token, who and what the household is, which cameras
and irrigation zones are set up, and a few suggested questions made from the household's own names. The page shows a panel
only when its part is here; the readings themselves (a thermostat, a washer) come from Home Assistant when the page asks,
and a panel with no entity found stays hidden.

    {"setup_complete": true, "ha": {"url_set": true, "token_set": true},
     "people": ["Alex", "Sam"], "pets": [{"name": "Bella", "species": "cat"}],
     "cameras": [{"key": "kitchen", "name": "Kitchen"}], "frigate": false,
     "irrigation": {"zones": [{"key": "front", "name": "Front Zone"}], "rain_delay": true, "battery": true},
     "questions": ["Who's home?", "Is Alex home?", "Where is Bella?"]}
"""

from typing import Any, Dict, List

from hearth.household import household_from_config

MAX_QUESTIONS = 5


def questions(people: List[str], pets: Dict[str, str], cameras: Dict[str, Dict[str, str]]) -> List[str]:
    """Suggested questions from the household's own names (generic ones when there are none)."""
    out = ["Who's home?"]
    out += [f"Is {p} home?" for p in people[:2]]
    out += [f"Where is {p}?" for p in list(pets)[:2]]
    if cameras:
        first = next(iter(cameras.values()))
        out.append(f"What is happening on the {first.get('name') or 'main'} camera?")
    return out[:MAX_QUESTIONS]


def ui_features(cfg: Dict[str, Any]) -> Dict[str, Any]:
    from hearth import setup_wizard
    status = setup_wizard.setup_status(cfg)
    hh = household_from_config(cfg)
    irrigation = cfg.get("irrigation") or cfg.get("garden") or {}
    zones = irrigation.get("zones") or {}
    frigate = cfg.get("frigate") or {}
    return {
        "setup_complete": status["complete"],
        "ha": {"url_set": status["homeassistant"]["url_set"], "token_set": status["homeassistant"]["token_set"]},
        "people": list(hh.people),
        "pets": [{"name": n, "species": s} for n, s in hh.pets.items()],
        "cameras": [{"key": k, "name": c.get("name", k)} for k, c in hh.cameras.items()],
        "frigate": bool(frigate.get("url")),
        "irrigation": {
            "zones": [{"key": k, "name": (z or {}).get("name", k)} for k, z in zones.items()] if isinstance(zones, dict) else [],
            "rain_delay": bool(irrigation.get("rain_delay_switch")),
            "battery": bool(irrigation.get("battery_sensor")),
        },
        "questions": questions(list(hh.people), dict(hh.pets), dict(hh.cameras)),
    }
