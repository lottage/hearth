"""
Commentary: when Computer speaks without being asked (Phase 3; the owner's rules, 2026-09-23: kitchen/living room plus
outdoor cams, at most ~6 a day, activity-based, one per episode, only when someone is home). No clock: 2026-09-27 the owner:
"There is no set day night sleep schedule ... It should be dynamic based on activity and the metrics reported", so the
old quiet hours (22:00-08:00) are gone; see Resting below.

Triggers (config.json commentary):
  arrival   a resident's HA person entity (Life360 / phone GPS) turns home after >= min_away_min away: the "just got
            home" flag. The welcome comes at whichever confirms it first (the owner, 2026-09-27: first sighting since):
            - a door (arrival.doors) used within ARRIVAL_WINDOW_S, either side: Life360 often reports home minutes
              after the front door has opened;
            - a camera sighting of that person by name since the GPS arrival (Frigate face, sentry, patrol);
            - any person on an arrival camera (arrival.cameras, Frigate) while no other resident is home, so it
              can only be them.
            No confirmation within ARRIVAL_WINDOW_S: no greeting (GPS noise, or they never came in).
            Residents arriving together share one greeting.
  cooking / cleaning   while a person has been around an activity camera (activity.cameras: Frigate camera -> room)
            for >= min_in_view_s (Frigate splits someone standing still into short events, median 16 s, so gaps up
            to PRESENCE_GAP_S count as still there), the vision model reads the frame every check_every_s, and must
            name the object that shows it (a pan, a cloth). Two such reads in a row start an episode (one misread
            never speaks); it ends after EPISODE_GAP_S without one.
            No reads while a remark could not be spoken anyway (resting, budget used, nobody home, Echo on DND):
            the GPU is not spent on it.
Resting (instead of a schedule): would the remark wake someone? Only the residents home who are NOT being addressed
count (an arriver or the cook is awake by definition). Each is
  awake    their phone is in use now or was within awake_min, or a camera saw them by name within awake_min
           or they got home (Life360) within awake_min, or their watch counted wear_steps_awake steps since its
           previous sync within wear_awake_min
  asleep   their phone's sleep confidence (Google's sleep API via the HA companion app) >= sleep_confidence, or their
           watch's body battery rose >= 2 % since its previous sync with no steps (Garmin only charges it at rest;
           Garmin Connect reports sleep itself only after waking, so this is the live sign)
  unknown  otherwise (a resident with no phone or watch in HA is judged by the house)
Anyone asleep -> held. Anyone unknown -> held if the house showed no sign of life for idle_min before the event began:
no door, no person on a camera, no Echo voice command, no phone in use (the event's own signs don't count).
Gates, in order: enabled, a resident home, resting, the budget (daily_budget spoken in any 24 h, rolling, so no midnight
reset), the room's cooldown, the room Echo's Do Not Disturb switch. A held-back episode is used up (no remark later for the same one) and still traced.
A remark is one short tool-free coordinator call (persona + the facts + the last few remarks, so it does not repeat
itself). mode "speak" announces it on the room's Echo, "log" only records it (shadow mode); mode may also be a dict per
trigger ({"arrival": "speak", "cooking": "log"}) so a trigger can prove itself in the trace before it talks.
Every candidate is one trace record, kind "commentary": trigger, room, who, facts, outcome (spoken | logged | held |
failed), why it was held, the remark and timings.
"""

import json
import re

from hearth.persona import home_text
from lodestone.site import clock
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

TICK_S = 20                  # how often doors, residents and activity cameras are checked
ARRIVAL_WINDOW_S = 15 * 60   # a door within this long of the GPS arrival (either side) makes it an arrival
EPISODE_GAP_S = 20 * 60      # an activity episode ends this long after its last matching read
PRESENCE_GAP_S = 90          # no person on an activity camera for this long = they left
ACTIVITIES = ("cooking", "cleaning")
RECENT_REMARKS = 5           # the last few remarks go into the prompt so he does not repeat himself
BUDGET_WINDOW_S = 24 * 3600  # daily_budget counts remarks spoken in this rolling window
STIR_KEEP_S = 12 * 3600      # how far back signs of life are kept
SYNC_GAP_S = 300             # watch samples closer than this belong to one sync (steps and body battery land apart)

LATE_MAX_AGE_S = 600       # a read that waited for the vision engine is used only if its frame is younger than this

DEFAULTS: Dict[str, Any] = {
    "enabled": False, "mode": "log", "daily_budget": 6, "room_cooldown_min": 10,
    "residents": [],                                    # [] = the household's people (config.json household)
    "dnd": {},                                          # room -> HA switch (the room Echo's Do Not Disturb)
    "arrival": {"doors": [], "cameras": [], "room": "kitchen", "min_away_min": 30},
    "activity": {"cameras": {}, "check_every_s": 120, "min_in_view_s": 45},
    # phones: resident -> {"interactive": binary_sensor (screen in use), "sleep_confidence": sensor (0-100)}
    # activity_events: HA event entities whose state is the time of the last event (Echo voice commands)
    # wearables: resident -> {"steps": sensor, "body_battery": sensor} (Garmin Connect; updates when the watch syncs)
    "rest": {"phones": {}, "wearables": {}, "activity_events": [], "sleep_confidence": 70, "awake_min": 15,
             "idle_min": 60, "wear_steps_awake": 30, "wear_awake_min": 30, "wear_max_age_min": 120},
}

# 2026-09-27: the first prompt ("A frame from the kitchen. What is the person doing?") called a man reaching across the
# island "cooking" and one putting on a jacket "wiping the counter" (2 of 16 real frames). Asking for the object that
# proves it, and not naming the room, fixed both.
VISION_PROMPT = (
    "A frame from the home's {where} camera. Is the main person cooking or cleaning right now? Judge only from what "
    "is visibly in their hands or right in front of them. cooking = handling food, a pot, a pan, a knife or a "
    "cutting board; cleaning = holding a cloth, sponge, broom, mop or vacuum, or washing dishes at the sink. "
    "Anything else (sitting, standing, walking, carrying things, dressing, eating, on the phone), or if unsure: other. "
    'Reply with JSON only: {{"activity": "cooking" | "cleaning" | "other", '
    '"evidence": "what they are holding or doing that shows it, or none"}}')

# 2026-09-27 dry runs: with free-form facts Qwen3 named the wrong person ("Someone cleaning" -> one resident by name; a
# joint arrival -> both names run together) and invented senses ("smell like a funeral", "that tired look"). The facts now
# say exactly whom to address (see talk_to), and the no-senses rule is repeated next to them.
PERSONA = (
    "You are Computer, the computer in the attic of {home}: dry, British, a little sarcastic, "
    "warm underneath. Nobody asked you anything: you are about to say one line out loud through the {room} Echo. "
    "Say one short sentence (under 25 words) to the people in 'Talk to', in the second person and using their names "
    "when it gives them: a dry, affectionate aside. Don't ask them to do anything or offer help; no emojis, quotes or stage directions.")
RULES = ("Use only these facts. You only see a still camera picture: you cannot smell, hear, taste, read moods or "
         "know how anything feels, so never mention any of those.")


# Invented senses / moods ("the house feels quiet without you", "I can smell..."): such a remark is written again once.
# tests/live/test_commentary_live.py grades with the same pattern.
INVENTED = re.compile(r"\b(smell\w*|scent|aroma|sniff\w*|hear\w*|sound\w*|noise|tast\w*|flavou?r|look(s|ing)? (tired|"
                      r"happy|sad|content|exhausted|stressed)|mood|feel(s|ing)?(?! like))\b", re.I)


def talk_to(who: List[str]) -> str:
    """Whom the remark addresses, spelled out: the model otherwise picks a name itself."""
    if not who:
        return "whoever is there (you don't know who, so use no name)"
    return _names(who) + (" (say both names)" if len(who) > 1 else "")


def _cfg_from(full: Dict[str, Any]) -> Dict[str, Any]:
    """The commentary settings from the whole config; residents default to the household's people."""
    out = _cfg(full.get("commentary"))
    if not out.get("residents"):
        out["residents"] = list((full.get("household") or {}).get("people") or [])
    return out


def _cfg(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw or {}
    out = {**DEFAULTS, **raw}
    for k in ("arrival", "activity", "rest"):
        out[k] = {**DEFAULTS[k], **(raw.get(k) or {})}
    return out


def mode_for(cfg: Dict[str, Any], trigger: str) -> str:
    """"speak" or "log" for this trigger: mode is one word for all, or a dict per trigger (missing = log)."""
    mode = cfg.get("mode")
    return (mode.get(trigger) or "log") if isinstance(mode, dict) else (mode or "log")


def _ts(iso: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_activity(text: str) -> Tuple[str, str]:
    """The vision model's JSON -> (activity, evidence); unreadable, or an activity with no evidence, is ("other", "")."""
    s, e = (text or "").find("{"), (text or "").rfind("}")
    try:
        d = json.loads(text[s:e + 1]) if s >= 0 and e > s else {}
    except ValueError:
        d = {}
    act = str(d.get("activity") or "").strip().lower()
    evidence = str(d.get("evidence") or d.get("detail") or "").strip()[:120]
    echoed = len(evidence) > 24 and evidence.lower() in VISION_PROMPT.lower()   # seen 2026-09-27; "a pan" is fine
    if act not in ACTIVITIES or evidence.lower() in ("", "none", "n/a", "unknown") or echoed:
        return "other", ""
    return act, evidence


def clean_remark(text: str) -> str:
    """First line of the model's answer, without quotes or a speaker tag, capped for the Echo."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if line.lower().startswith("computer:"):
        line = line[9:].strip()
    return line.strip('"“”\' ')[:240]


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def merge_samples(steps: List[tuple], battery: List[tuple]) -> List[Dict[str, float]]:
    """Two HA histories [(t, state)] -> watch samples [{t, steps, bb}] (each value carried forward), oldest first."""
    events = sorted([(t, "steps", _num(v)) for t, v in steps] + [(t, "bb", _num(v)) for t, v in battery])
    cur: Dict[str, Optional[float]] = {"steps": None, "bb": None}
    out: List[Dict[str, float]] = []
    for t, key, v in events:
        if v is None:
            continue
        cur[key] = v
        if cur["steps"] is not None and cur["bb"] is not None:
            out.append({"t": t, "steps": cur["steps"], "bb": cur["bb"]})
    return out


def _names(names: List[str]) -> str:
    names = [n.capitalize() for n in names]
    return " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]


def _hours(seconds: float) -> str:
    return f"{round(seconds / 60)} minutes" if seconds < 5400 else f"{seconds / 3600:.1f} hours"


class Commentary:
    def __init__(self, cfg_fn: Callable[[], Dict[str, Any]], ha_state: Callable[[str], Optional[Dict[str, Any]]],
                 in_view: Callable[[str], List[Dict[str, Any]]], frame: Callable[[str], Optional[bytes]],
                 vision: Callable[[bytes, str], str], llm: Callable[[List[Dict[str, str]]], str],
                 speak: Callable[[str, str], Dict[str, Any]], trace: Optional[Callable[[Dict[str, Any]], None]] = None,
                 state_path: Optional[str] = None, clock: Callable[[], float] = time.time,
                 seen_since: Optional[Callable[[str, float], Optional[str]]] = None,
                 history: Optional[Callable[[List[str], float], List[Tuple[float, str]]]] = None,
                 ha_history: Optional[Callable[[List[str], float], Dict[str, List[tuple]]]] = None):
        """seen_since(name, t): the camera that saw this resident by name at or after t, else None.
        history(cameras, t): (time, camera) of people on those cameras since t, read once at start so a restart does
        not look like hours of silence (2026-09-27: right after a deploy it said "house quiet 531 min" 30 min after
        Frigate had seen someone in the kitchen)."""
        self.cfg_fn, self.ha_state, self.in_view, self.frame = cfg_fn, ha_state, in_view, frame
        self.vision, self.llm, self.speak, self.trace = vision, llm, speak, trace
        self.seen_since, self.history, self.ha_history = seen_since, history, ha_history
        self._backfilled = False
        self.wear: Dict[str, List[Dict[str, float]]] = {}   # resident -> watch samples {t, steps, bb}, oldest first
        self._wear_backfilled: set = set()
        self.present: Dict[str, Dict[str, Any]] = {}     # activity camera -> {since, last, people}
        self.state_path, self.clock = state_path, clock
        self.people: Dict[str, Dict[str, Any]] = {}      # resident -> {state, changed, left_at}
        self.pending: Dict[str, Dict[str, Any]] = {}     # resident -> {at, away_s}: home per GPS, no door yet
        self.doors: Dict[str, float] = {}                # door entity -> last_changed seen
        self.door_uses: List[float] = []                 # recent door use times
        self.reads: Dict[str, List[Tuple[float, str]]] = {}   # camera -> recent (time, activity) reads
        self.last_read: Dict[str, float] = {}
        self.read_ctx: Dict[str, Any] = {}               # the camera / room / who of the read in progress (the vision service queues by it)
        self.episodes: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (camera, activity) -> {start, last, spoken}
        self.state: Dict[str, Any] = {"spoken_at": [], "logged_at": [], "room_last": {}, "recent": []}
        self.stirs: List[Tuple[float, str]] = []         # (time, source): signs someone in the house is awake
        self.started = clock()
        self._views: Dict[str, List[Dict[str, Any]]] = {}  # in_view per camera, asked once per tick
        self.last_tick: Optional[float] = None
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # ------------------------------------------------------------ state ----
    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state.update(json.load(f))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        except OSError:
            pass

    def _in_window(self, key: str) -> List[float]:
        now = self.clock()
        return [t for t in self.state.get(key) or [] if now - t < BUDGET_WINDOW_S]

    def spoken_24h(self) -> int:
        return len(self._in_window("spoken_at"))

    def _view(self, camera: str) -> List[Dict[str, Any]]:
        if camera not in self._views:
            try:
                self._views[camera] = list(self.in_view(camera) or [])
            except Exception as e:
                self.last_error = f"in_view {camera}: {e}"[:300]
                self._views[camera] = []
        return self._views[camera]

    # ------------------------------------------------------------ gates ----
    def _residents_home(self, cfg: Dict[str, Any]) -> List[str]:
        return [n for n in cfg["residents"] if (self.people.get(n.lower()) or {}).get("state") == "home"]

    # ------------------------------------------------------------ resting ----
    def _stir(self, t: Optional[float], source: str) -> None:
        """Record a sign of life (at most one entry per source and time)."""
        if t is None or t > self.clock() + 60:
            return
        if not any(src == source and abs(tt - t) < 1 for tt, src in self.stirs[-50:]):
            self.stirs.append((t, source))

    def _observe(self, cfg: Dict[str, Any]) -> None:
        """Collect this tick's signs of life: people on cameras, doors, Echo voice commands, phones in use."""
        now = self.clock()
        cams = set(cfg["arrival"].get("cameras") or []) | set((cfg["activity"].get("cameras") or {}))
        for cam in sorted(cams):
            if any(o.get("label") == "person" for o in self._view(cam)):
                self._stir(now, f"camera:{cam}")
        for t in self.door_uses:
            self._stir(t, "door")
        for ent in cfg["rest"].get("activity_events") or []:
            self._stir(_ts((self.ha_state(ent) or {}).get("state")), f"voice:{ent.split('.', 1)[-1]}")
        for name, phone in (cfg["rest"].get("phones") or {}).items():
            if (self.people.get(name) or {}).get("state") != "home":
                continue                    # a phone in use away from home says nothing about the house (seen live)
            st = self.ha_state(phone.get("interactive", "")) if phone.get("interactive") else None
            if st and st.get("state") == "on":
                self._stir(now, f"phone:{name}")
            elif st:
                self._stir(_ts(st.get("last_changed")), f"phone:{name}")   # when the screen went off
        self.stirs = sorted(x for x in self.stirs if now - x[0] <= STIR_KEEP_S)[-2000:]

    def _read_wearables(self, cfg: Dict[str, Any]) -> None:
        """New watch samples (and, once per resident, the last 6 h from HA's history, so a restart is not blind)."""
        now = self.clock()
        for name, w in (cfg["rest"].get("wearables") or {}).items():
            if not w.get("steps") or not w.get("body_battery"):
                continue
            ids = [w["steps"], w["body_battery"]]
            if name not in self._wear_backfilled and self.ha_history:
                try:
                    hist = self.ha_history(ids, now - 6 * 3600)
                    self.wear[name] = merge_samples(hist.get(ids[0], []), hist.get(ids[1], []))
                    self._wear_backfilled.add(name)
                except Exception as e:
                    self.last_error = f"watch history: {e}"[:300]
            st, sb = self.ha_state(ids[0]) or {}, self.ha_state(ids[1]) or {}
            steps, bb = _num(st.get("state")), _num(sb.get("state"))
            t = max(_ts(st.get("last_changed")) or 0, _ts(sb.get("last_changed")) or 0)
            samples = self.wear.setdefault(name, [])
            if steps is None or bb is None or not t or (samples and t <= samples[-1]["t"]):
                continue
            samples.append({"t": t, "steps": steps, "bb": bb})
            del samples[:-40]
            if (self.people.get(name) or {}).get("state") == "home" and self._wear_change(samples)[0] >= \
                    float(cfg["rest"]["wear_steps_awake"]):
                self._stir(t, f"watch:{name}")          # walking about at home is a sign of life

    @staticmethod
    def _wear_change(samples: List[Dict[str, float]]) -> Tuple[float, float]:
        """(steps, body battery) gained since the previous sync (>= SYNC_GAP_S before the newest sample)."""
        if len(samples) < 2:
            return 0.0, 0.0
        b = samples[-1]
        a = next((x for x in reversed(samples[:-1]) if b["t"] - x["t"] >= SYNC_GAP_S), None)
        if a is None:
            return 0.0, 0.0
        steps = b["steps"] - a["steps"] if b["steps"] >= a["steps"] else b["steps"]   # the count resets daily
        return steps, b["bb"] - a["bb"]

    def wearable_state(self, cfg: Dict[str, Any], name: str) -> Tuple[str, str]:
        """("awake" | "asleep" | "unknown", why) from the resident's watch, if they have one in config."""
        rest, now = cfg["rest"], self.clock()
        samples = self.wear.get(name) or []
        if len(samples) < 2:
            return "unknown", ""
        age = now - samples[-1]["t"]
        if age > float(rest["wear_max_age_min"]) * 60:
            return "unknown", f"watch data {round(age / 60)} min old"
        steps, bb = self._wear_change(samples)
        at = clock(datetime.fromtimestamp(samples[-1]["t"]))
        if steps >= float(rest["wear_steps_awake"]) and age <= float(rest["wear_awake_min"]) * 60:
            return "awake", f"watch: {int(steps)} steps by {at}"
        if bb >= 2 and steps <= 10:
            return "asleep", f"watch: body battery +{bb:.0f}% with no steps by {at}"
        return "unknown", ""

    def resident_state(self, cfg: Dict[str, Any], name: str) -> Tuple[str, str]:
        """("awake" | "asleep" | "unknown", why) for one resident, from their phone and the cameras."""
        now, rest = self.clock(), cfg["rest"]
        awake_s = float(rest["awake_min"]) * 60
        phone = (rest.get("phones") or {}).get(name) or {}
        if phone.get("interactive"):
            st = self.ha_state(phone["interactive"]) or {}
            if st.get("state") == "on":
                return "awake", "using their phone"
            off = _ts(st.get("last_changed"))
            if off and now - off <= awake_s:
                return "awake", f"phone used {round((now - off) / 60)} min ago"
        gps = self.people.get(name) or {}
        if gps.get("state") == "home" and gps.get("left_at") and now - gps["changed"] <= awake_s:
            return "awake", f"got home {round((now - gps['changed']) / 60)} min ago"   # Life360: accurate (the owner)
        if self.seen_since:
            try:
                cam = self.seen_since(name, now - awake_s)
            except Exception:
                cam = None
            if cam:
                return "awake", f"seen on {cam}"
        watch, watch_why = self.wearable_state(cfg, name)
        if watch == "awake":
            return watch, watch_why
        if phone.get("sleep_confidence"):
            try:
                conf = float((self.ha_state(phone["sleep_confidence"]) or {}).get("state"))
            except (TypeError, ValueError):
                conf = None
            if conf is not None and conf >= float(rest["sleep_confidence"]):
                return "asleep", f"phone sleep confidence {conf:.0f}%"
        if watch == "asleep":
            return watch, watch_why
        return "unknown", ""

    def last_stir(self, before: float) -> Optional[float]:
        return max((t for t, _ in self.stirs if t < before), default=None)

    def resting(self, cfg: Dict[str, Any], addressed: List[str], since: Optional[float]) -> Optional[str]:
        """Why speaking now might wake someone, or None. since: when the event's own signs began (they don't count)."""
        now = self.clock()
        since = now if since is None else since
        unknown = []
        for n in self._residents_home(cfg):
            key = n.lower()
            if key in addressed:
                continue
            state, why = self.resident_state(cfg, key)
            if state == "asleep":
                return f"{n} may be asleep ({why})"
            if state == "unknown":
                unknown.append(n)
        if not unknown:
            return None
        idle_s = float(cfg["rest"]["idle_min"]) * 60
        last = self.last_stir(since)
        quiet_for = since - (last if last is not None else self.started)
        if quiet_for >= idle_s:
            return f"house quiet {round(quiet_for / 60)} min before this; {_names(unknown)} may be asleep"
        return None

    def household(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Is anyone awake who might want the house brain? For the idle gate (idle_gate.py), same signals as resting:
        awake evidence anywhere (phone in use, watch steps, just got home, seen) -> not quiet; a resident home with no
        evidence -> quiet only if the house showed no sign of life for idle_min; away with no evidence -> no objection."""
        cfg = cfg or _cfg_from(self.cfg_fn())
        now = self.clock()
        residents, reasons = {}, []
        for n in cfg["residents"]:
            key = n.lower()
            home = (self.people.get(key) or {}).get("state") == "home"
            state, why = self.resident_state(cfg, key)
            residents[key] = {"home": home, "state": state, "why": why}
            if state == "awake":
                reasons.append(f"{n} is awake ({why})")
        unknown_home = [n for n in cfg["residents"] if residents[n.lower()]["home"] and residents[n.lower()]["state"] == "unknown"]
        last = self.last_stir(now + 1)
        quiet_for = now - (last if last is not None else self.started)
        if unknown_home and quiet_for < float(cfg["rest"]["idle_min"]) * 60:
            reasons.append(f"house active {round(quiet_for / 60)} min ago and {_names(unknown_home)} may be up")
        return {"quiet": not reasons, "reasons": reasons, "residents": residents,
                "house_quiet_min": round(quiet_for / 60, 1)}

    def gate(self, cfg: Dict[str, Any], room: str, arriving: bool = False, addressed: Optional[List[str]] = None,
             since: Optional[float] = None) -> Optional[str]:
        """Why a remark in `room` cannot be spoken now, or None. arriving: the arrival itself proves someone is home.
        addressed: who it is said to (awake); since: when the event's own signs began."""
        now = self.clock()
        if not cfg["enabled"]:
            return "disabled"
        if not arriving and not self._residents_home(cfg):
            return "nobody_home"
        why = self.resting(cfg, [a.lower() for a in addressed or []], since)
        if why:
            return "resting: " + why
        if self.spoken_24h() >= int(cfg["daily_budget"]):
            return "daily_budget"
        last = (self.state.get("room_last") or {}).get(room)
        if last and now - last < float(cfg["room_cooldown_min"]) * 60:
            return "room_cooldown"
        dnd = (cfg.get("dnd") or {}).get(room)
        if dnd and ((self.ha_state(dnd) or {}).get("state") == "on"):
            return "do_not_disturb"
        return None

    # ---------------------------------------------------------- arrivals ----
    def _check_people(self, cfg: Dict[str, Any]) -> None:
        now = self.clock()
        min_away = float(cfg["arrival"]["min_away_min"]) * 60
        for name in cfg["residents"]:
            key = name.lower()
            st = self.ha_state(f"person.{key}")
            state = (st or {}).get("state")
            if state in (None, "unknown", "unavailable"):
                continue
            changed = _ts(st.get("last_changed")) or now
            prev = self.people.get(key)
            rec = {"state": "home" if state == "home" else "away", "changed": changed,
                   "left_at": (prev or {}).get("left_at")}
            if rec["state"] == "away":
                rec["left_at"] = changed
            elif prev and prev["state"] == "away" and rec["left_at"] and changed - rec["left_at"] >= min_away:
                self.pending[key] = {"at": changed, "away_s": changed - rec["left_at"]}   # home per GPS
            self.people[key] = rec

    def _check_doors(self, cfg: Dict[str, Any]) -> None:
        now = self.clock()
        for door in cfg["arrival"]["doors"]:
            changed = _ts((self.ha_state(door) or {}).get("last_changed"))
            if changed is None:
                continue
            seen = self.doors.get(door)
            if seen is not None and changed > seen:
                self.door_uses.append(changed)      # opened or closed since the last look: someone used it
            if seen is None or changed > seen:
                self._stir(changed, "door")         # the first look's last use is a past sign of life too
            self.doors[door] = changed
        self.door_uses = [t for t in self.door_uses if now - t <= ARRIVAL_WINDOW_S]

    def _confirm(self, cfg: Dict[str, Any], key: str, p: Dict[str, Any]) -> Optional[str]:
        """How we know a GPS arrival has actually come in, or None yet: 'door', or 'seen on <camera>'.
        A door sets p["evidence_at"] (the arrival's own signs start there, for the resting check)."""
        doors = [t for t in self.door_uses if abs(t - p["at"]) <= ARRIVAL_WINDOW_S]
        if doors:
            p["evidence_at"] = min(doors)
            return "door"
        if self.seen_since:
            try:
                cam = self.seen_since(key, p["at"] - 120)    # a named sighting since they got home (GPS lags a bit)
            except Exception:
                cam = None
            if cam:
                return f"seen on {cam}"
        others_home = [n for n, s in self.people.items() if n != key and n not in self.pending and s["state"] == "home"]
        if not others_home:                                   # an unnamed person can only be them
            for camera in cfg["arrival"].get("cameras") or []:
                if any(o.get("label") == "person" for o in self._view(camera)):
                    return f"seen on {camera.replace('_', ' ')}"
        return None

    def _arrivals(self, cfg: Dict[str, Any]) -> None:
        now = self.clock()
        ready: Dict[str, str] = {}
        expired = []
        for key, p in self.pending.items():
            how = self._confirm(cfg, key, p)
            if how:
                ready[key] = how
            elif now - p["at"] > ARRIVAL_WINDOW_S:
                expired.append(key)
        for key in expired:
            self.pending.pop(key)
            self._record({"trigger": "arrival", "room": cfg["arrival"]["room"], "who": [key], "outcome": "held",
                          "held": "not_confirmed",
                          "facts": f"{key.capitalize()} home per GPS; no door or camera sighting within 15 min"})
        if not ready:
            return
        who = sorted(ready)
        away = max(self.pending[k]["away_s"] for k in who)
        since = min(min(self.pending[k]["at"], self.pending[k].get("evidence_at", now)) for k in who) - 60
        for k in who:
            self.pending.pop(k)     # door uses stay: a partner whose GPS catches up a minute later matches the same
        # door, and the room cooldown keeps it to one greeting
        hour = f"{datetime.fromtimestamp(now):%A} {clock(datetime.fromtimestamp(now))}"
        facts = f"Event: they just came home, after about {_hours(away)} away. It is {hour}."
        self.remark(cfg, "arrival", cfg["arrival"]["room"], who, facts, arriving=True, since=since,
                    confirmed_by=sorted(set(ready.values())))

    # ---------------------------------------------------------- activity ----
    def _activity(self, cfg: Dict[str, Any]) -> None:
        now = self.clock()
        acfg = cfg["activity"]
        for key in [k for k, ep in self.episodes.items() if now - ep["last"] > EPISODE_GAP_S]:
            self.episodes.pop(key)
        for camera, room in (acfg.get("cameras") or {}).items():
            people = [o for o in self._view(camera) if o.get("label") == "person"]
            pr = self.present.get(camera)
            if people:
                pr = self.present.setdefault(camera, {"since": now - max(o.get("for_s") or 0 for o in people)})
                pr.update(last=now, people=people)
            elif pr and now - pr["last"] > PRESENCE_GAP_S:
                self.present.pop(camera)
                pr = None
            if not pr or now - pr["since"] < float(acfg["min_in_view_s"]):
                continue                    # nobody around, or just walking through
            people = pr["people"]
            if now - self.last_read.get(camera, 0) < float(acfg["check_every_s"]):
                continue
            who = sorted({o["name"] for o in people if o.get("name") and o["name"] != "someone"})
            since = pr["since"] - 60
            active = [a for (c, a) in self.episodes if c == camera]
            if not active and self.gate(cfg, room, addressed=who, since=since):
                continue                    # no remark possible: don't spend the GPU (episodes still get extended)
            self.last_read[camera] = now
            jpeg = self.frame(camera)
            if not jpeg:
                continue
            t0 = time.time()
            self.read_ctx = {"camera": camera, "room": room, "who": who}     # what the vision service needs to queue this frame
            try:
                act, detail = parse_activity(self.vision(jpeg, VISION_PROMPT.format(where=camera.replace("_", " "))))
            except Exception as e:
                self.last_error = f"vision: {e}"
                continue
            vision_ms = round((time.time() - t0) * 1000)
            reads = [r for r in self.reads.get(camera, []) if now - r[0] <= EPISODE_GAP_S] + [(now, act)]
            self.reads[camera] = reads[-10:]
            if act not in ACTIVITIES:
                continue
            ep = self.episodes.get((camera, act))
            if ep:
                ep["last"] = now
                continue
            if len(reads) < 2 or reads[-2][1] != act:
                continue                    # two reads in a row must agree: one misread never speaks
            self.episodes[(camera, act)] = {"start": now, "last": now}
            facts = (f"Event: started {act} in the {room}; the camera shows: {detail or act}. "
                     f"It is {datetime.fromtimestamp(now):%A} {clock(datetime.fromtimestamp(now))}.")
            self.remark(cfg, act, room, who, facts, since=since, vision_ms=vision_ms)

    # ------------------------------------------------- a read that waited for the vision engine ----
    def deliver_late(self, item: Dict[str, Any], text: str, frame: bytes = b"") -> Dict[str, Any]:
        """The vision engine was lent out (the garden) when this frame was read; the queue (stonesage/vision_queue.py) read it afterwards.
        A late read never speaks: the moment has gone. Older than commentary.late_max_age_s (600) it is skipped (traced as held); a fresh
        one counts as a read for the next two-reads-agree check. Both leave a trace record, marked late."""
        cfg = self.cfg_fn() or {}
        limit = float((cfg.get("commentary") or {}).get("late_max_age_s", LATE_MAX_AGE_S))
        age = self.clock() - float(item.get("at") or self.clock())
        meta = item.get("meta") or {}
        rec: Dict[str, Any] = {"trigger": "activity", "room": meta.get("room", ""), "who": meta.get("who") or [], "late": True,
                               "late_age_s": round(age), "facts": "late vision read of the %s camera" % item.get("camera", "")}
        if age > limit:
            rec.update(outcome="held", held="late read is stale (%d min old)" % round(age / 60))
            return self._record(rec)
        act, detail = parse_activity(text)
        camera = item.get("camera") or ""
        if act in ACTIVITIES:
            self.reads[camera] = (self.reads.get(camera, []) + [(float(item["at"]), act)])[-10:]
        rec.update(outcome="logged", activity=act, evidence=detail)
        return self._record(rec)

    # ------------------------------------------------------------ remark ----
    def compose(self, room: str, who: List[str], facts: str) -> str:
        recent = self.state.get("recent") or []
        user = f"Talk to: {talk_to(who)}\n{facts}\n{RULES}"
        if recent:
            user += "\nYour last remarks (say something different): " + " | ".join(recent)
        msgs = [{"role": "system", "content": PERSONA.format(room=room, home=home_text(_cfg_from(self.cfg_fn())["residents"]))}, {"role": "user", "content": user}]
        text = clean_remark(self.llm(msgs))
        if INVENTED.search(text):           # one more try; the eval saw this in ~1 of 20 lines
            text = clean_remark(self.llm(msgs)) or text
        return text

    def remark(self, cfg: Dict[str, Any], trigger: str, room: str, who: List[str], facts: str,
               arriving: bool = False, dry: bool = False, since: Optional[float] = None,
               **timings: Any) -> Dict[str, Any]:
        """Gate, compose and (mode speak) announce one remark; every call is one trace record."""
        rec: Dict[str, Any] = {"trigger": trigger, "room": room, "who": who, "facts": facts, **timings}
        held = None if dry else self.gate(cfg, room, arriving, addressed=who, since=since)
        if held:
            rec.update(outcome="held", held=held)
            return self._record(rec, write=not dry)
        t0 = time.time()
        try:
            text = self.compose(room, who, facts)
        except Exception as e:
            rec.update(outcome="failed", error=f"llm: {e}"[:200], triggers=["llm_error"])
            return self._record(rec, write=not dry)
        rec.update(remark=text, llm_ms=round((time.time() - t0) * 1000))
        if not text:
            rec.update(outcome="failed", error="empty remark", triggers=["empty_answer"])
            return self._record(rec, write=not dry)
        if dry or mode_for(cfg, trigger) != "speak":
            rec["outcome"] = "dry_run" if dry else "logged"
        else:
            t0 = time.time()
            res = self.speak(text, room) or {}
            rec["speak_ms"] = round((time.time() - t0) * 1000)
            if res.get("ok") is False:
                rec.update(outcome="failed", error=f"speak: {res.get('error')}"[:200], triggers=["speak_error"])
                return self._record(rec, write=not dry)
            rec["outcome"] = "spoken"
        if not dry:
            with self._lock:
                if rec["outcome"] == "spoken":  # only what was said uses the budget and the room's cooldown: a
                    # shadow-mode cooking line must not hold back a real welcome home
                    self.state["spoken_at"] = self._in_window("spoken_at") + [self.clock()]
                    self.state.setdefault("room_last", {})[room] = self.clock()
                    self.state["recent"] = (self.state.get("recent") or [])[-(RECENT_REMARKS - 1):] + [text]
                else:
                    self.state["logged_at"] = self._in_window("logged_at") + [self.clock()]
                self._save()
        return self._record(rec, write=not dry)

    def _record(self, rec: Dict[str, Any], write: bool = True) -> Dict[str, Any]:
        rec = {"kind": "commentary", "at": round(self.clock(), 3), **rec}
        rec.setdefault("triggers", [])
        if self.trace and write:            # dry runs (the test route, the live eval) stay out of the trace
            try:
                self.trace(rec)
            except Exception:
                pass
        return rec

    # -------------------------------------------------------------- loop ----
    def tick(self) -> None:
        cfg = _cfg_from(self.cfg_fn())
        self._views = {}
        if not self._backfilled and self.history:
            cams = sorted(set(cfg["arrival"].get("cameras") or []) | set(cfg["activity"].get("cameras") or {}))
            try:
                for t, cam in self.history(cams, self.clock() - STIR_KEEP_S):
                    self._stir(t, f"camera:{cam}")
                self._backfilled = True
            except Exception as e:
                self.last_error = f"history: {e}"[:300]    # tried again next tick
        self._check_people(cfg)
        self._check_doors(cfg)
        self._read_wearables(cfg)
        self._observe(cfg)
        self.last_tick = self.clock()
        if not cfg["enabled"]:
            self.pending.clear()        # baselines stay current, so switching it on does not greet a stale arrival
            return
        self._arrivals(cfg)
        self._activity(cfg)

    def status(self) -> Dict[str, Any]:
        cfg = _cfg_from(self.cfg_fn())
        now = self.clock()
        last = self.last_stir(now + 1)
        sources: Dict[str, float] = {}
        for t, src in self.stirs:
            sources[src] = t
        residents = {}
        for k, v in self.people.items():
            state, why = self.resident_state(cfg, k) if v["state"] == "home" else ("away", "")
            residents[k] = {"gps": v["state"], "state": state, "why": why}
        watches = {}
        for k, samples in self.wear.items():
            steps, bb = self._wear_change(samples)
            state, why = self.wearable_state(cfg, k)
            watches[k] = {"samples": len(samples), "steps": samples[-1]["steps"] if samples else None,
                          "body_battery": samples[-1]["bb"] if samples else None,
                          "last_sample_min_ago": round((now - samples[-1]["t"]) / 60, 1) if samples else None,
                          "since_previous_sync": {"steps": steps, "body_battery": bb}, "reads_as": state, "why": why}
        return {"enabled": cfg["enabled"], "mode": cfg["mode"], "daily_budget": cfg["daily_budget"],
                "watches": watches,
                "spoken_24h": self.spoken_24h(), "logged_24h": len(self._in_window("logged_at")),
                "residents": residents,
                "resting_now": self.resting(cfg, [], None) if self._residents_home(cfg) else "nobody home",
                "last_sign_of_life_min_ago": round((now - last) / 60, 1) if last else None,
                "signs_of_life_min_ago": {s: round((now - t) / 60, 1) for s, t in sorted(sources.items())},
                "waiting_for_door": sorted(self.pending), "recent_remarks": self.state.get("recent") or [],
                "episodes": [{"camera": c, "activity": a, "for_min": round((now - ep["start"]) / 60, 1)}
                             for (c, a), ep in self.episodes.items()],
                "present_min": {c: round((now - p["since"]) / 60, 1) for c, p in self.present.items()},
                "reads": {c: [{"ago_s": round(now - t), "activity": a} for t, a in r[-4:]] for c, r in self.reads.items()},
                "last_tick_s_ago": round(now - self.last_tick) if self.last_tick else None,
                "last_error": self.last_error}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:300]
                time.sleep(TICK_S)

        self._thread = threading.Thread(target=loop, name="commentary", daemon=True)
        self._thread.start()
