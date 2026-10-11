"""
Frigate presence: who and what is in view, from Frigate's own event stream (Phase 3).

Frigate detects objects on the iGPU and pushes every tracked object as an "events" message
(new / update / end) over its websocket (:5000/ws, the same payloads it publishes to MQTT). This module
keeps the latest sighting per identity in memory and hands Courage a `locations` dict in the same shape
as harness/core/home_presence_hub.py, so the presence card and presence_now pick it up unchanged.

Identity: a Frigate face/recognition sub_label wins (a resident's name); otherwise config.json
frigate.identities maps a label to a household member (the one cat, the one dog). A person
without a sub_label is "someone": seen, but not claimed to be any resident.
An event without end_time is an object in view right now.
"""

import asyncio
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("StoneSage.FrigatePresence")

UNKNOWN_PERSON = "someone"


def norm_name(name: str) -> str:
    """Identity key used everywhere (presence locations, sentry snapshot prefix, UI cards): 'Aunt May' -> 'aunt-may'.
    Same rule as prefix() in deploy/cluster-bridge/wildlife_admin.py."""
    return re.sub(r"[^a-z0-9-]", "", name.strip().lower().replace(" ", "-").replace("'", ""))


def _sub_label_name(sub_label: Any) -> Optional[str]:
    """Frigate sends sub_label as "Alex" or ["Alex", 0.93] depending on version and source."""
    if isinstance(sub_label, (list, tuple)) and sub_label:
        sub_label = sub_label[0]
    return norm_name(sub_label) if isinstance(sub_label, str) and sub_label.strip() else None


def _score(ev: Dict[str, Any]) -> float:
    data = ev.get("data") or {}
    for v in (ev.get("top_score"), data.get("top_score"), ev.get("score"), data.get("score")):
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def camera_label(camera: str) -> str:
    """'living_room' -> 'kitchen living room' (Courage's _camera_key matches on the first word)."""
    return camera.replace("_", " ")


HISTORY_PER_IDENTITY = 10   # a rejected sighting falls back to the next one of these


class FrigatePresence:
    def __init__(self, url: str, identities: Optional[Dict[str, str]] = None, min_score: float = 0.7,
                 clock: Callable[[], float] = time.time):
        self.url = url.rstrip("/")
        self.identities = {k.lower(): v.lower() for k, v in (identities or {}).items()}
        self.min_score = min_score
        self.clock = clock
        self.latest: Dict[str, Dict[str, Any]] = {}    # identity -> newest sighting (= history[identity][0])
        self.history: Dict[str, List[Dict[str, Any]]] = {}  # identity -> recent sightings, newest first
        self.rejected: set = set()                     # event ids the owner marked wrong: never shown again
        self.active: Dict[str, Dict[str, Any]] = {}    # event id -> sighting, while the object is in view
        self.connected = False
        self.last_message_at: Optional[float] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ ingest ----
    def identify(self, ev: Dict[str, Any]) -> Optional[str]:
        name = _sub_label_name(ev.get("sub_label"))
        if name:
            return name
        label = (ev.get("label") or "").lower()
        if label in self.identities:
            return self.identities[label]
        return UNKNOWN_PERSON if label == "person" else None

    def ingest(self, ev: Dict[str, Any]) -> Optional[str]:
        """One Frigate event (REST record or the websocket 'after' object). Returns the identity it updated."""
        if not ev or not ev.get("id") or ev.get("false_positive") or ev["id"] in self.rejected:
            return None
        name = self.identify(ev)
        if name is None or _score(ev) < self.min_score:
            return None
        ended = ev.get("end_time")
        seen_at = ended or ev.get("frame_time") or ev.get("start_time") or self.clock()
        rec = {"name": name, "camera": ev.get("camera") or "", "label": ev.get("label"), "event_id": ev["id"],
               "seen_at": float(seen_at), "in_view": not ended, "score": round(_score(ev), 2),
               "has_snapshot": bool(ev.get("has_snapshot"))}
        with self._lock:
            if ended:
                self.active.pop(ev["id"], None)
            else:
                self.active[ev["id"]] = rec
            self._file(rec)
        return name

    def _file(self, rec: Dict[str, Any]) -> None:
        """Put a sighting into its identity's history (replacing an older record of the same event) and
        make the newest one `latest`. Caller holds the lock."""
        hist = [r for r in self.history.get(rec["name"], []) if r["event_id"] != rec["event_id"]]
        hist.append(rec)
        hist.sort(key=lambda r: r["seen_at"], reverse=True)
        self.history[rec["name"]] = hist[:HISTORY_PER_IDENTITY]
        self.latest[rec["name"]] = self.history[rec["name"]][0]

    def _unfile(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Take an event out of every history; the identity falls back to its next most recent sighting.
        Returns the record removed. Caller holds the lock."""
        found = None
        for name, hist in list(self.history.items()):
            keep = [r for r in hist if r["event_id"] != event_id]
            if len(keep) != len(hist):
                found = next(r for r in hist if r["event_id"] == event_id)
                if keep:
                    self.history[name], self.latest[name] = keep, keep[0]
                else:
                    self.history.pop(name, None)
                    self.latest.pop(name, None)
        return found

    # ------------------------------------------------------------ output ----
    def locations(self) -> Dict[str, Dict[str, Any]]:
        """Home-presence-hub-shaped locations: {name: {minutes_ago, camera, doing, source, ...}}."""
        now = self.clock()
        with self._lock:
            in_view = {r["name"]: r for r in self.active.values()}
            latest = dict(self.latest)
        out = {}
        for name, rec in latest.items():
            live = in_view.get(name)
            r = live or rec
            out[name] = {
                "minutes_ago": 0.0 if live else round(max(0.0, now - r["seen_at"]) / 60.0, 1),
                "mtime": now if live else r["seen_at"],
                "camera": camera_label(r["camera"]),
                "doing": "in view now" if live else f"passed the {camera_label(r['camera'])} camera",
                "source": "frigate",
                "event_id": r["event_id"],
                "snapshot_url": f"{self.url}/api/events/{r['event_id']}/snapshot.jpg" if r["has_snapshot"] else None,
            }
        return out

    def apply_correction(self, event_id: str, name: Optional[str]) -> None:
        """Mirror a correction the owner made in the UI: name=None drops the sighting (rejected: the identity shows its
        next most recent sighting, and the event never comes back), else re-files it under that identity. Frigate
        gets the same correction through its API; this keeps memory in step."""
        with self._lock:
            rec = self._unfile(event_id)
            active = self.active.get(event_id)
            if name is None:
                self.rejected.add(event_id)
                self.active.pop(event_id, None)
                return
            new_name = norm_name(name)
            if rec is not None:
                self._file(dict(rec, name=new_name))
            if active is not None:
                self.active[event_id] = dict(active, name=new_name)

    # ------------------------------------------------------- camera look ----
    def in_view_now(self, camera: str) -> List[Dict[str, Any]]:
        """Objects Frigate is tracking on this camera right now, asked at call time (~0.1 s) rather than
        trusted from memory, where a dropped websocket could leave an event open forever."""
        with urllib.request.urlopen(f"{self.url}/api/events?camera={camera}&in_progress=1&limit=20", timeout=3) as r:
            events = json.load(r)
        out = []
        for ev in events:
            if ev.get("end_time") or ev.get("false_positive") or _score(ev) < self.min_score:
                continue
            out.append({"label": ev.get("label"), "name": self.identify(ev), "score": round(_score(ev), 2),
                        "for_s": int(max(0.0, self.clock() - (ev.get("start_time") or self.clock())))})
        return out

    def latest_frame(self, camera: str, height: int = 448) -> bytes:
        """Frigate's newest decoded frame for the camera (~0.1 s, local), instead of an HA snapshot (~1.5 s)."""
        with urllib.request.urlopen(f"{self.url}/api/{camera}/latest.jpg?h={height}", timeout=3) as r:
            return r.read()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            active = [{"name": r["name"], "camera": r["camera"], "label": r["label"]} for r in self.active.values()]
        return {"url": self.url, "connected": self.connected, "last_message_at": self.last_message_at,
                "in_view": active, "locations": self.locations()}

    # ------------------------------------------------------------ feeds ----
    def bootstrap(self, limit: int = 100) -> int:
        """Replay recent events from the REST API so a StoneSage restart does not forget who was seen."""
        with urllib.request.urlopen(f"{self.url}/api/events?limit={limit}", timeout=5) as r:
            events: List[Dict[str, Any]] = json.load(r)
        for ev in sorted(events, key=lambda e: e.get("start_time") or 0):
            self.ingest(ev)
        return len(events)

    def handle_message(self, raw: str) -> Optional[str]:
        """One websocket frame: {"topic": ..., "payload": ...}; only 'events' matter here."""
        self.last_message_at = self.clock()
        msg = json.loads(raw)
        if msg.get("topic") != "events":
            return None
        payload = msg.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        return self.ingest((payload or {}).get("after") or {})

    async def _listen(self) -> None:
        import websockets  # available on the StoneSage host (websockets 10.x)
        url = self.url.replace("http", "ws", 1) + "/ws"
        backoff = 5
        while True:
            try:
                async with websockets.connect(url, max_size=None, ping_interval=30) as ws:
                    self.connected, backoff = True, 5
                    logger.info(f"listening to Frigate events at {url}")
                    async for raw in ws:
                        try:
                            self.handle_message(raw)
                        except Exception as e:
                            logger.debug(f"skipped Frigate message: {e}")
            except Exception as e:
                logger.warning(f"Frigate event stream: {e}; retrying in {backoff}s")
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            logger.info(f"Frigate presence: replayed {self.bootstrap()} recent events")
        except Exception as e:
            logger.warning(f"Frigate presence bootstrap failed: {e}")
        self._thread = threading.Thread(target=lambda: asyncio.run(self._listen()), name="frigate-presence", daemon=True)
        self._thread.start()


EVENT_ID = re.compile(r"^[0-9]+\.[0-9]+-[a-z0-9]+$")


def go2rtc_stream_names(go2rtc_url: str) -> set:
    """Stream names go2rtc has (raises when go2rtc is unreachable)."""
    with urllib.request.urlopen(f"{go2rtc_url.rstrip('/')}/api/streams", timeout=3) as r:
        return set(json.load(r) or {})


def live_cameras(go2rtc_url: str, frigate_cameras: List[str], streams: Optional[set] = None) -> List[Dict[str, str]]:
    """Cameras the StoneSage LIVE tab can play: each Frigate camera, on its go2rtc sub stream (H.264, plays in
    every browser) when one exists, else the main stream. streams: go2rtc's names if the caller has them."""
    if streams is None:
        streams = go2rtc_stream_names(go2rtc_url)
    out = []
    for cam in frigate_cameras:
        if cam in streams:
            out.append({"id": cam, "name": camera_label(cam),
                        "stream": f"{cam}_sub" if f"{cam}_sub" in streams else cam})
    return out


def webrtc_answer(go2rtc_url: str, stream: str, offer: Dict[str, Any]) -> Dict[str, Any]:
    """Relay a browser's SDP offer to go2rtc and return its answer. Only signalling passes through StoneSage;
    the video goes straight from go2rtc (:8555) to the browser, so HTTPS pages can play it."""
    req = urllib.request.Request(f"{go2rtc_url.rstrip('/')}/api/webrtc?src={urllib.parse.quote(stream)}",
                                 data=json.dumps({"type": "offer", "sdp": offer.get("sdp", "")}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def describe_in_view(objects: List[Dict[str, Any]]) -> str:
    """'Rex (dog, in view 40 s), a person (not identified, 5 s)' for Courage; household names where known."""
    parts = []
    for o in objects:
        who = o.get("name")
        if who and who != UNKNOWN_PERSON:
            parts.append(f"{who.capitalize()} ({o.get('label')}, in view {o.get('for_s', 0)} s)")
        else:
            parts.append(f"a {o.get('label')} (not identified, in view {o.get('for_s', 0)} s)")
    return ", ".join(parts)


def merge_locations(hub: Dict[str, Dict[str, Any]], frigate: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per identity, keep whichever source saw them most recently (Frigate is seconds old; the hub's
    VLM-read snapshots can be minutes old but know who a person is)."""
    out = dict(hub or {})
    for name, loc in (frigate or {}).items():
        cur = out.get(name)
        if cur is None or (loc.get("minutes_ago") or 0) <= (cur.get("minutes_ago") if cur.get("minutes_ago") is not None else 1e9):
            out[name] = loc
    return out
