"""Scenes: "movie night" checked against the room, only what needs changing changed, verified, and said truthfully.

The Home Assistant script stays the ONE definition of a scene (its owner edits it in Home Assistant). config.json holds only
the thin mapping, household data that never goes in code:

    "scenes": {
      "movie_night": {"script": "script.movie_night",          the HA script that defines it
                      "phrases": ["movie night", "movie time"], exact sentences that run it (no model needed)
                      "confirm": true,                          say the result on an Echo (the default speaker)
                      "title": "Movie night", "speaker": "kitchen"}    optional
    }

run(name) reads the script's config through HA's config API every time, works out each step's expected end state (service +
target + data; `choose` branches are decided by their simple state conditions against the current states, like a thermostat
branch per hvac_mode), skips what is already in that state, calls the remaining steps with the same service and data, waits
a moment, reads the states again and verifies every device it meant to change. Assumed-state devices (a template switch that
speaks to an Echo) can't be read back: they are always sent, and reported as "sent". A device that is offline is reported
plainly. Anything it cannot interpret (repeat, templates, device targets, unknown services...) makes it run the WHOLE script
and say so. Powering a TV on is never done here (the TV's state is only reported); unlocks, covers and alarm panels are
refused (they always need a yes), as are server plugs turned off.

One function serves Computer's run_scene tool and reflex, the routes POST /api/scenes/<name>/run and /plan, and the dashboard.
"""

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

OFFLINE = ("unavailable",)
BLOCKED_DOMAINS = {"lock", "cover", "alarm_control_panel", "valve", "siren"}
POWER_DOMAINS = {"remote", "media_player"}          # turn_on is reported, never sent
ONOFF_DOMAINS = {"light", "switch", "fan", "input_boolean", "remote", "media_player"}
IGNORED_ATTRS = {"transition", "flash", "entity_id"}
STEP_KEYS = {"action", "service", "target", "data", "alias", "continue_on_error", "enabled", "delay", "choose", "default"}
FILLER_PREFIX = re.compile(r"^(?:(?:hey|ok|okay|please|computer|can you|could you|would you|will you|go ahead and|"
                           r"i want|i would like|let'?s|lets|start|run|activate|begin|put on|turn on|switch on|set up|setup|"
                           r"do|enable|launch|the)\s+)+")
FILLER_SUFFIX = re.compile(r"(?:\s+(?:please|scene|mode|now|for me))+$")


class Uninterpretable(Exception):
    """A script step the planner can't turn into an expected end state: the whole script is run instead."""


class Refused(Exception):
    """A step that must never run silently (always needs a yes)."""


# --------------------------------------------------------------------------------------------- config
def config_errors(block: Any) -> List[str]:
    """Problems in config.json "scenes" (empty list = fine)."""
    if block in (None, {}):
        return []
    if not isinstance(block, dict):
        return ["scenes must be an object of scene name -> {script, phrases, confirm}"]
    errors, seen = [], {}
    for name, sc in block.items():
        if not isinstance(sc, dict):
            errors.append(f"scene '{name}': must be an object")
            continue
        script = sc.get("script")
        if not isinstance(script, str) or not re.fullmatch(r"script\.[a-z0-9_]+", script):
            errors.append(f"scene '{name}': script must be a Home Assistant script entity id like script.my_scene")
        phrases = sc.get("phrases", [])
        if not isinstance(phrases, list) or not all(isinstance(p, str) and norm_phrase(p) for p in phrases):
            errors.append(f"scene '{name}': phrases must be a list of non-empty sentences")
            phrases = []
        if "confirm" in sc and not isinstance(sc["confirm"], bool):
            errors.append(f"scene '{name}': confirm must be true or false")
        for p in phrases:
            other = seen.setdefault(norm_phrase(p), name)
            if other != name:
                errors.append(f"phrase '{p}' is used by both '{other}' and '{name}'")
    return errors


def norm_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower())).strip()


def core_phrase(text: str) -> str:
    """'Hey computer, please start movie night' -> 'movie night' (what is left once politeness and command words go)."""
    t = norm_phrase(text)
    for _ in range(3):
        t = FILLER_SUFFIX.sub("", FILLER_PREFIX.sub("", t)).strip()
    return t


# --------------------------------------------------------------------------------------------- reading the script
def _ids(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return [v.strip() for v in value if v.strip()]
    raise Uninterpretable("entity ids written as a template or object")


def _plain(value: Any) -> bool:
    """No Jinja anywhere in the data."""
    if isinstance(value, str):
        return "{{" not in value and "{%" not in value
    if isinstance(value, dict):
        return all(_plain(v) for v in value.values())
    if isinstance(value, list):
        return all(_plain(v) for v in value)
    return True


def _state_of(states: Dict[str, Dict[str, Any]], entity_id: str) -> Optional[str]:
    return (states.get(entity_id) or {}).get("state")


def _condition_true(cond: Any, states: Dict[str, Dict[str, Any]]) -> bool:
    if not isinstance(cond, dict) or cond.get("condition") != "state" or set(cond) - {"condition", "entity_id", "state", "alias"}:
        raise Uninterpretable("a condition that is not a plain state check")
    wanted = cond.get("state")
    wanted = [str(w) for w in wanted] if isinstance(wanted, list) else [str(wanted)]
    ents = _ids(cond.get("entity_id"))
    if not ents:
        raise Uninterpretable("a state condition without an entity")
    return all(_state_of(states, e) in wanted for e in ents)


def _flatten(steps: Any, states: Dict[str, Dict[str, Any]], out: List[Dict[str, Any]]) -> None:
    if not isinstance(steps, list):
        raise Uninterpretable("a sequence that is not a list")
    for step in steps:
        if not isinstance(step, dict) or set(step) - STEP_KEYS:
            raise Uninterpretable("a step type I don't read (" + ", ".join(sorted(set(step) - STEP_KEYS)[:2] if isinstance(step, dict) else ["?"]) + ")")
        if step.get("enabled") is False:
            continue
        if "delay" in step and len(set(step) - {"delay", "alias", "enabled"}) == 0:
            continue                                                 # timing only; calls are spaced by the planner itself
        if "choose" in step:
            if not isinstance(step["choose"], list):
                raise Uninterpretable("choose is not a list")
            for branch in step["choose"]:
                conds = branch.get("conditions") if isinstance(branch, dict) else None
                if not isinstance(conds, list) or not conds:
                    raise Uninterpretable("a choose branch without plain conditions")
                if all(_condition_true(c, states) for c in conds):
                    _flatten(branch.get("sequence") or [], states, out)
                    break
            else:
                _flatten(step.get("default") or [], states, out)
            continue
        action = step.get("action") or step.get("service")
        if not isinstance(action, str) or "." not in action or not _plain(step.get("data") or {}):
            raise Uninterpretable("a step with a template or no service")
        target = step.get("target") or {}
        if not isinstance(target, dict) or set(target) - {"entity_id"}:
            raise Uninterpretable("a step aimed at a device or area")
        data = dict(step.get("data") or {})
        ents = _ids(target.get("entity_id")) or _ids(data.pop("entity_id", None))
        if not ents:
            raise Uninterpretable("a step without an entity")
        for e in ents:
            out.append({"entity_id": e, "action": action, "data": dict(data), "continue_on_error": bool(step.get("continue_on_error"))})


def read_steps(script_cfg: Dict[str, Any], states: Dict[str, Dict[str, Any]], infra: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    """The script's steps, one per entity, with `choose` decided by the current states. Raises Uninterpretable or Refused."""
    if not isinstance(script_cfg, dict):
        raise Uninterpretable("no script config")
    flat: List[Dict[str, Any]] = []
    _flatten(script_cfg.get("sequence"), states, flat)
    for s in flat:
        domain, service = s["action"].split(".", 1)
        s["domain"], s["service"] = domain, service
        eid = s["entity_id"]
        if domain in BLOCKED_DOMAINS or (service not in ("turn_off", "close_cover", "lock") and
                                         any(w in eid.lower() for w in ("garage", "gate", "deadbolt", "door_lock", "doorlock"))):
            raise Refused(f"{eid} always needs a yes")
        if domain == "switch" and service in ("turn_off", "toggle") and any(w in eid.lower() for w in infra):
            raise Refused(f"{eid} powers server infrastructure")
        supported = ((domain in ONOFF_DOMAINS and service in ("turn_on", "turn_off")) or
                     (domain == "climate" and service in ("set_temperature", "set_hvac_mode")) or
                     (domain in ("select", "input_select") and service == "select_option"))
        if not supported or not eid.startswith(domain + "."):
            raise Uninterpretable(f"{s['action']} on {eid}")
    return flat


# --------------------------------------------------------------------------------------------- comparing with the room
def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _attrs_match(wanted: Dict[str, Any], attrs: Dict[str, Any]) -> bool:
    for k, w in wanted.items():
        if k in IGNORED_ATTRS:
            continue
        if k == "brightness_pct":
            b = _num(attrs.get("brightness"))
            if b is None or abs(b / 255 * 100 - float(w)) > 3:
                return False
        elif k == "brightness":
            b = _num(attrs.get("brightness"))
            if b is None or abs(b - float(w)) > 3:
                return False
        elif k == "effect":
            if str(attrs.get("effect") or "").lower() != str(w).lower():
                return False
        elif k == "color_temp_kelvin":
            c = _num(attrs.get("color_temp_kelvin"))
            if c is None or abs(c - float(w)) > 100:
                return False
        elif k in ("rgb_color", "hs_color", "xy_color"):
            a = attrs.get(k)
            tol = 6 if k == "rgb_color" else 2 if k == "hs_color" else 0.02
            if not isinstance(a, (list, tuple)) or len(a) != len(w) or any(abs(float(x) - float(y)) > tol for x, y in zip(a, w)):
                return False
        elif k in ("temperature", "target_temp_low", "target_temp_high"):
            a = _num(attrs.get(k))
            if a is None or abs(a - float(w)) >= 0.5:
                return False
        elif attrs.get(k) != w:
            return False
    return True


def _satisfied(step: Dict[str, Any], ent: Optional[Dict[str, Any]]) -> bool:
    """Is this entity already in the step's end state?"""
    if not ent:
        return False
    state, attrs = ent.get("state"), ent.get("attributes") or {}
    domain, service, data = step["domain"], step["service"], step["data"]
    if service == "turn_off":
        return state in ("off", "standby") if domain in POWER_DOMAINS else state == "off"
    if service == "turn_on":
        on = (state not in ("off", "standby", "unavailable", "unknown")) if domain in POWER_DOMAINS else state == "on"
        return on and _attrs_match(data, attrs)
    if service == "set_hvac_mode":
        return state == data.get("hvac_mode")
    if service == "select_option":
        return state == data.get("option")
    if service == "set_temperature":
        return _attrs_match({k: v for k, v in data.items() if k in ("temperature", "target_temp_low", "target_temp_high")}, attrs) \
            and "hvac_mode" not in data
    return False


def _fmt_num(v: Any) -> str:
    n = _num(v)
    return str(int(n)) if n is not None and n == int(n) else str(v)


def describe(step: Dict[str, Any]) -> str:
    """The step's end state in a few words: 'off', 'on TV time at 30 %', 'set to 68-72'."""
    domain, service, d = step["domain"], step["service"], step["data"]
    if service == "turn_off":
        return "off"
    if service == "turn_on":
        bits = ["on"]
        if d.get("effect"):
            bits.append(str(d["effect"]))
        pct = d.get("brightness_pct", round(float(d["brightness"]) / 2.55) if _num(d.get("brightness")) is not None else None)
        if pct is not None:
            bits.append(f"at {_fmt_num(pct)} %")
        return " ".join(bits)
    if service == "set_hvac_mode":
        return f"in {d.get('hvac_mode')} mode"
    if service == "select_option":
        return f"set to {d.get('option')}"
    if "target_temp_low" in d or "target_temp_high" in d:
        return f"set to {_fmt_num(d.get('target_temp_low'))}-{_fmt_num(d.get('target_temp_high'))}"
    return f"set to {_fmt_num(d.get('temperature'))}"


def _join(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _group(rows: List[Dict[str, Any]]) -> List[Tuple[List[str], str]]:
    groups: Dict[str, List[str]] = {}
    for r in rows:
        groups.setdefault(r["desc"], []).append(r["label"])
    return [(names, desc) for desc, names in groups.items()]


# --------------------------------------------------------------------------------------------- the engine
class Scenes:
    def __init__(self, load_config: Callable[[], Dict[str, Any]], get_states: Callable[[Optional[str]], Dict[str, Any]],
                 call_service: Callable[..., Dict[str, Any]], get_script_config: Callable[[str], Optional[Dict[str, Any]]],
                 speak: Optional[Callable[[str, str], Dict[str, Any]]] = None, infra_words: Callable[[], Tuple[str, ...]] = lambda: (),
                 sleep: Callable[[float], None] = time.sleep, settle_s: float = 2.0, assumed_gap_s: float = 1.0):
        self.load_config, self.get_states, self.call_service = load_config, get_states, call_service
        self.get_script_config, self.speak, self.infra_words = get_script_config, speak, infra_words
        self.sleep, self.settle_s, self.assumed_gap_s = sleep, settle_s, assumed_gap_s

    # ---- names and phrases ------------------------------------------------
    def block(self) -> Dict[str, Dict[str, Any]]:
        b = (self.load_config() or {}).get("scenes")
        return b if isinstance(b, dict) else {}

    def names(self) -> List[str]:
        return [n for n, sc in self.block().items() if isinstance(sc, dict) and sc.get("script")]

    def match(self, text: str) -> Optional[str]:
        """The scene a sentence names exactly ('Hey Computer, start movie night' -> movie_night), else None."""
        core = core_phrase(text)
        if not core:
            return None
        for name, sc in self.block().items():
            if not isinstance(sc, dict):
                continue
            phrases = [name.replace("_", " ")] + list(sc.get("phrases") or [])
            if any(core_phrase(p) == core for p in phrases if isinstance(p, str)):
                return name
        return None

    def title(self, name: str) -> str:
        t = (self.block().get(name) or {}).get("title") or name.replace("_", " ")
        return t[:1].upper() + t[1:]

    # ---- state reading ------------------------------------------------------
    def _states(self) -> Dict[str, Dict[str, Any]]:
        res = self.get_states(None) or {}
        if not res.get("ok", True):
            raise RuntimeError(res.get("error") or "Home Assistant can't be read")
        return {e["entity_id"]: e for e in res.get("entities") or [] if e.get("entity_id")}

    @staticmethod
    def _label(eid: str, ent: Optional[Dict[str, Any]]) -> str:
        return ((ent or {}).get("friendly_name") or ((ent or {}).get("attributes") or {}).get("friendly_name")
                or eid.split(".", 1)[-1].replace("_", " ").title())

    # ---- plan ---------------------------------------------------------------
    def plan(self, name: str) -> Dict[str, Any]:
        """What running the scene would do now. Calls nothing."""
        sc = self.block().get(name)
        if not isinstance(sc, dict) or not sc.get("script"):
            return {"ok": False, "scene": name, "error": f"no scene called '{name}'"}
        errs = config_errors({name: sc})
        if errs:
            return {"ok": False, "scene": name, "error": "; ".join(errs)}
        try:
            states = self._states()
        except Exception as e:
            return {"ok": False, "scene": name, "error": str(e)}
        script_id = sc["script"]
        cfg = self.get_script_config(script_id.split(".", 1)[1])
        if not cfg:
            return {"ok": False, "scene": name, "error": f"couldn't read {script_id} from Home Assistant"}
        plan: Dict[str, Any] = {"ok": True, "scene": name, "title": self.title(name), "script": script_id, "mode": "steps", "items": []}
        try:
            steps = read_steps(cfg, states, infra=tuple(str(w).lower() for w in self.infra_words() or ()))
        except Refused as e:
            return {"ok": False, "scene": name, "refused": True, "error": f"{self.title(name)} includes something that always needs a yes ({e}), so I won't run it on my own"}
        except Uninterpretable as e:
            plan.update(mode="whole", why=str(e))
            return plan
        seen = set()
        for s in steps:
            eid = s["entity_id"]
            ent = states.get(eid)
            key = (eid, s["action"], repr(sorted(s["data"].items())))
            if key in seen:
                continue
            seen.add(key)
            item = {"entity_id": eid, "label": self._label(eid, ent), "domain": s["domain"], "service": s["service"],
                    "data": s["data"], "desc": describe(s), "step": s,
                    "assumed": bool((ent or {}).get("attributes", {}).get("assumed_state")),
                    "now": (ent or {}).get("state")}
            if ent is None or ent.get("state") == "unavailable":
                item["action"] = "offline"
            elif s["domain"] in POWER_DOMAINS and s["service"] == "turn_on":
                item["action"] = "report"                    # the owner's call: don't power the TV on from a scene; just say what it is
            elif item["assumed"]:
                item["action"] = "run"                       # the state it shows says nothing about the lamp: always send
            elif _satisfied(s, ent):
                item["action"] = "skip"
            else:
                item["action"] = "run"
            plan["items"].append(item)
        return plan

    # ---- run ----------------------------------------------------------------
    def run(self, name: str, plan_only: bool = False, announce: Optional[bool] = None) -> Dict[str, Any]:
        plan = self.plan(name)
        if not plan.get("ok"):
            return plan
        sc = self.block()[name]
        if plan["mode"] == "whole":
            return self._finish(plan, self._run_whole(plan, sc, plan_only), sc, plan_only, announce)
        items = plan["items"]
        if plan_only:
            return self._finish(plan, {"plan_only": True}, sc, True, announce)
        out = {"plan_only": False}
        prev_assumed, call_failed = False, {}
        for it in items:
            if it["action"] != "run":
                continue
            if it["assumed"] and prev_assumed and self.assumed_gap_s:
                self.sleep(self.assumed_gap_s)           # one Echo hears one command at a time
            payload = dict(it["data"], entity_id=it["entity_id"])
            try:
                res = self.call_service(it["domain"], it["service"], payload) or {}
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            prev_assumed = it["assumed"]
            if not res.get("ok"):
                call_failed[it["entity_id"]] = res.get("error") or "refused"
        todo = [it for it in items if it["action"] == "run"]
        if todo:
            self.sleep(self.settle_s)
        for attempt in range(2):
            try:
                states = self._states()
            except Exception:
                states = {}
            waiting = False
            for it in todo:
                if it["entity_id"] in call_failed:
                    it["result"], it["error"] = "failed", call_failed[it["entity_id"]]
                    continue
                ent = states.get(it["entity_id"])
                if ent is None or ent.get("state") == "unavailable":
                    it["result"] = "offline"
                elif it["assumed"]:
                    it["result"] = "sent"
                elif _satisfied(it["step"], ent):
                    it["result"] = "done"
                else:
                    it["result"] = "didnt_take"
                    waiting = True
            if not waiting or attempt == 1:
                break
            self.sleep(self.settle_s)
        return self._finish(plan, out, sc, False, announce)

    def _run_whole(self, plan: Dict[str, Any], sc: Dict[str, Any], plan_only: bool) -> Dict[str, Any]:
        if plan_only:
            return {"plan_only": True}
        res = self.call_service("script", "turn_on", {"entity_id": plan["script"]}) or {}
        return {"plan_only": False, "whole_ok": bool(res.get("ok")), "whole_error": res.get("error")}

    # ---- the answer ----------------------------------------------------------
    def _finish(self, plan: Dict[str, Any], out: Dict[str, Any], sc: Dict[str, Any], plan_only: bool, announce: Optional[bool]) -> Dict[str, Any]:
        res = {"ok": True, "scene": plan["scene"], "title": plan["title"], "plan_only": plan_only, "mode": plan["mode"]}
        if plan["mode"] == "whole":
            res["why"] = plan.get("why", "")
            if plan_only:
                res["summary"] = f"I can't read {plan['title']} step by step ({res['why']}), so I'd run the whole script."
            elif out.get("whole_ok"):
                res["summary"] = f"{plan['title']} ran. I couldn't read its steps one by one ({res['why']}), so I ran all of it and can't check the result."
            else:
                res.update(ok=False)
                res["summary"] = f"Home Assistant refused to run {plan['title']}."
        else:
            res["items"] = [{k: it.get(k) for k in ("entity_id", "label", "action", "desc", "now", "assumed", "result", "error")} for it in plan["items"]]
            res["summary"] = self.summarize(plan["title"], plan["items"], plan_only)
            res["ok"] = not any(it.get("result") in ("failed", "didnt_take") for it in plan["items"])
        speak = sc.get("confirm") is True if announce is None else announce
        if speak and not plan_only and self.speak:
            try:
                res["announced"] = bool((self.speak(res["summary"], str(sc.get("speaker") or "")) or {}).get("ok"))
            except Exception:
                res["announced"] = False
        return res

    @staticmethod
    def summarize(title: str, items: List[Dict[str, Any]], plan_only: bool = False) -> str:
        def rows(pred):
            return [i for i in items if pred(i)]
        if plan_only:
            run, skip = rows(lambda i: i["action"] == "run"), rows(lambda i: i["action"] == "skip")
            off, rep = rows(lambda i: i["action"] == "offline"), rows(lambda i: i["action"] == "report")
            parts = [f"{_join(n)} {d}" for n, d in _group(run)]
            text = f"{title} would change: " + "; ".join(parts) + "." if parts else f"{title} has nothing to change."
            if skip:
                text += " Already right: " + "; ".join(f"{_join(n)} {d}" for n, d in _group(skip)) + "."
            if off:
                text += f" Offline: {_join([i['label'] for i in off])}."
            if rep:
                text += " Left alone: " + "; ".join(f"{i['label']} is {i['now']}" for i in rep) + "."
            return text
        done = rows(lambda i: i.get("result") == "done")
        sent = rows(lambda i: i.get("result") == "sent")
        failed = rows(lambda i: i.get("result") == "failed")
        didnt = rows(lambda i: i.get("result") == "didnt_take")
        offline = rows(lambda i: i["action"] == "offline" or i.get("result") == "offline")
        skip = rows(lambda i: i["action"] == "skip")
        report = rows(lambda i: i["action"] == "report")
        problems = []
        if offline:
            problems.append(f"{_join([i['label'] for i in offline])} {'is' if len(offline) == 1 else 'are'} offline")
        if failed:
            problems.append(f"Home Assistant refused {_join([i['label'] for i in failed])}")
        if didnt:
            problems.append(f"{_join([i['label'] for i in didnt])} didn't change")
        head = f"{title} is set" + (", but " + " and ".join(problems) if problems else "") + "."
        if not (done or sent or failed or didnt) and not problems:
            head = f"{title} was already set."
        bits = [f"{_join(n)} {d}" for n, d in _group(done)]
        if sent:
            bits += [f"{_join(n)} {d} (sent; I can't confirm {'that' if len(n) == 1 else 'those'})" for n, d in _group(sent)]
        text = head + (" " + "; ".join(bits) + "." if bits else "")
        if skip:
            text += " " + "; ".join(f"{_join(n)} already {d}" for n, d in _group(skip)) + "."
        if report:
            text += " " + "; ".join(f"{i['label']} is {i['now']}" for i in report) + "."
        return text
