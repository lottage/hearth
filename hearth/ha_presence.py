"""
StoneSage -> Home Assistant presence (Phase 3, 2026-09-27; the owner picked this half of "HA Frigate integration": Frigate's
own HA integration is not installed for now).

HA only knew what the wildlife sentry posted (sensor.last_person_sighting / last_pet_sighting), so Frigate and patrol
sightings and the owner's corrections never reached HA. This publishes StoneSage's merged view instead (Frigate + sentry +
patrol, corrections applied: the view behind Computer's presence card and the Residents & Pets cards):

  sensor.computer_seen_<name>   one per resident and pet profile, plus "someone" (a person Frigate could not name):
                                state = when last seen (device_class timestamp, so HA shows "5 minutes ago");
                                attributes camera, source (frigate | sentry | patrol), in_view, activity
  sensor.last_person_sighting   the newest NAMED person, with the attributes the sentry wrote (person, role, camera,
                                is_resident, timestamp) plus source; sensor.last_pet_sighting likewise for pets

StoneSage is their only writer: the sentry stopped posting these two (it still posts sensor.last_subject_detected),
so a sighting the owner rejected can no longer come back through the side door. States set through HA's REST API vanish
when HA restarts, so everything is re-posted every REFRESH_S, and at once when something changes (INTERVAL_S).
"""

import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

INTERVAL_S = 30
REFRESH_S = 600
SOMEONE = "someone"
ICONS = {"person": "mdi:account", "cat": "mdi:cat", "dog": "mdi:dog", "pet": "mdi:paw", SOMEONE: "mdi:account-question"}


def norm_name(name: str) -> str:
    """Same identity key as frigate_presence.norm_name: 'Aunt May' -> 'aunt-may'."""
    return re.sub(r"[^a-z0-9-]", "", name.strip().lower().replace(" ", "-").replace("'", ""))


def entity_id(key: str) -> str:
    return "sensor.computer_seen_" + re.sub(r"[^a-z0-9]+", "_", key).strip("_")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _role(profile: Dict[str, Any]) -> str:
    return "Resident" if "resident" in str(profile.get("alert_level", "")) else (profile.get("role") or "Known")


def build(presence: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The merged presence -> {entity_id: {"state", "attributes"}} for every entity this module owns."""
    locs = presence.get("locations") or {}
    ents = presence.get("known_entities") or {}
    subjects = [(norm_name(p["name"]), p, "person") for p in ents.get("people", []) if p.get("name")]
    subjects += [(norm_name(p["name"]), p, "pet") for p in ents.get("pets", []) if p.get("name")]
    subjects.append((SOMEONE, {"name": "Someone"}, SOMEONE))
    out: Dict[str, Dict[str, Any]] = {}
    newest: Dict[str, Tuple[float, str, Dict[str, Any], Dict[str, Any]]] = {}
    for key, prof, kind in subjects:
        name = prof["name"]
        species = (prof.get("species") or "").lower()
        attrs: Dict[str, Any] = {
            "friendly_name": "Unidentified person last seen" if kind == SOMEONE else f"{name} last seen",
            "device_class": "timestamp", "icon": ICONS.get(species) or ICONS[kind], "subject": kind}
        loc = locs.get(key)
        if not loc or not loc.get("mtime"):
            out[entity_id(key)] = {"state": "unknown", "attributes": attrs}
            continue
        attrs.update(camera=loc.get("camera") or "", source=loc.get("source") or "sentry",
                     in_view=loc.get("minutes_ago") == 0, activity=(loc.get("doing") or "")[:200])
        out[entity_id(key)] = {"state": _iso(loc["mtime"]), "attributes": attrs}
        if kind in ("person", "pet") and loc["mtime"] > newest.get(kind, (0,))[0]:
            newest[kind] = (loc["mtime"], name, prof, loc)
    if "person" in newest:
        t, name, prof, loc = newest["person"]
        role = _role(prof)
        out["sensor.last_person_sighting"] = {"state": f"{name} ({role})", "attributes": {
            "friendly_name": "Last Person Sighting", "camera": loc.get("camera") or "", "person": name, "role": role,
            "is_resident": role == "Resident", "source": loc.get("source") or "sentry", "timestamp": _iso(t)}}
    if "pet" in newest:
        t, name, prof, loc = newest["pet"]
        species = (prof.get("species") or "pet").capitalize()
        out["sensor.last_pet_sighting"] = {"state": f"{name} ({species})", "attributes": {
            "friendly_name": "Last Pet Sighting", "camera": loc.get("camera") or "", "name": name,
            "species": prof.get("species") or "", "breed": prof.get("breed") or "", "is_resident": True,
            "source": loc.get("source") or "sentry", "timestamp": _iso(t)}}
    return out


class HAPresence:
    def __init__(self, presence_fn: Callable[[], Dict[str, Any]],
                 set_state: Callable[[str, str, Dict[str, Any]], Dict[str, Any]],
                 clock: Callable[[], float] = time.time):
        self.presence_fn, self.set_state, self.clock = presence_fn, set_state, clock
        self.sent: Dict[str, str] = {}          # entity_id -> what HA was last given (json), to post only changes
        self.last_full = 0.0
        self.last_tick: Optional[float] = None
        self.last_posted: Dict[str, float] = {}
        self.errors: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None

    def tick(self) -> int:
        """Post what changed (everything every REFRESH_S). Returns how many entities were posted."""
        now = self.clock()
        self.errors.pop("_tick", None)
        full = now - self.last_full >= REFRESH_S
        posted = 0
        for eid, body in build(self.presence_fn() or {}).items():
            sig = json.dumps(body, sort_keys=True)
            if not full and self.sent.get(eid) == sig:
                continue
            res = self.set_state(eid, body["state"], body["attributes"]) or {}
            if res.get("ok"):
                self.sent[eid], self.last_posted[eid] = sig, now
                self.errors.pop(eid, None)
                posted += 1
            else:
                self.sent.pop(eid, None)         # try again next tick
                self.errors[eid] = str(res.get("error"))[:200]
        if full:
            self.last_full = now                 # a failed entity is retried by itself (dropped from `sent`)
        self.last_tick = now
        return posted

    def status(self) -> Dict[str, Any]:
        now = self.clock()
        return {"entities": {e: {"state": json.loads(s)["state"],
                                 "posted_min_ago": round((now - self.last_posted.get(e, now)) / 60, 1)}
                             for e, s in sorted(self.sent.items())},
                "errors": self.errors, "last_tick_s_ago": round(now - self.last_tick) if self.last_tick else None}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:
                    self.errors["_tick"] = f"{type(e).__name__}: {e}"[:200]
                time.sleep(INTERVAL_S)

        self._thread = threading.Thread(target=loop, name="ha-presence", daemon=True)
        self._thread.start()
