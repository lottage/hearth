"""
PTZ patrol: the PTZ cameras sweep their whole pan range every so often and report who and what they saw.

Per camera (config.json patrol.cameras, keyed by HA camera entity):
  ptz           HA entity prefix (button.<ptz>_move_left/right, number.<ptz>_movement_angle, select.<ptz>_move_to_preset)
  frames        "frigate:<camera>" (Frigate's latest frame; that camera streams anyway) or "go2rtc:<stream>"
  home_preset   fallback return point: a sweep goes back to wherever the camera pointed before it (saved as a temporary
                Tapo preset, RETURN_PRESET, deleted afterwards) and only uses home_preset if that save or return fails
  quiet_when_home  skip sweeps while a resident is home (off since 2026-09-25: the owner finds the motors quiet enough)
  battery_sensor   battery-gated schedule (solar driveway): see interval_for()
  start_deg, step_deg, max_frames   where the first frame is taken (degrees right of the left end stop), the step
                   between frames and the frame cap; each falls back to the patrol-wide value (the owner, 2026-09-25:
                   kitchen 4 frames at 90..210, driveway 5 frames at 70..230, picked from full 30-degree sweeps)

A sweep: pan to the left end stop, turn right to start_deg, then step right by step_deg taking a frame each time until
the picture stops changing (right end stop reached) or max_frames, then return to where it pointed before. Each frame
is checked by the vision model for known profiles (for a Frigate camera only when Frigate sees someone, to spare the GPU). Sightings go into Courage's
presence like Frigate's (source "patrol"). A camera the owner moved by hand in the last few minutes is left alone, and
a sweep in progress stops before its next move when the owner or Courage's camera_scan takes the camera (note_manual).
One sweep per camera at a time: the scheduler and "Sweep now" both claim the camera first.
"""

import io
import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

MANUAL_HOLD_S = 300          # a hand-moved camera is not swept for this long
LEFT_END_MOVES = 3           # 3 x 120 deg left: past the end stop of any Tapo pan range
DEFAULT_ANGLE = 15           # Tapo's movement angle, restored when HA does not report the camera's own value
PRESET_TIMEOUT_S = 20        # saving / going to the return preset waits for the camera: a sleeping solar one takes > 4 s
                             # (2026-09-25: the first scheduled 5-frame driveway sweep fell back home after HA's 4 s)
# Temporary Tapo preset holding the pre-sweep position. HA's preset list refreshes on a save but not on a delete, so the
# name can linger in HA after the sweep: camera_ui and camera_scan hide it (is_temp_preset).
RETURN_PRESET = "StoneSage return"


def is_temp_preset(name: str) -> bool:
    return (name or "").strip() == RETURN_PRESET


class Interrupted(Exception):
    """Someone else took the camera mid-sweep."""


class MoveFailed(Exception):
    """HA / the camera refused a PTZ move twice. Without this a refused press looked like the right end stop
    (the next frame is unchanged): 2026-09-25 a kitchen sweep 'ended' at 90 deg after one frame."""


def _key(name: str) -> str:
    """Identity key, same rule as frigate_presence.norm_name: 'Aunt May' -> 'aunt-may'."""
    return re.sub(r"[^a-z0-9-]", "", (name or "").strip().lower().replace(" ", "-").replace("'", ""))


def frames_differ(a: Optional[bytes], b: Optional[bytes], threshold: float = 3.0) -> bool:
    """Mean absolute difference (0-255 scale) of small grayscale thumbnails. After a pan step the view shifts a lot;
    at the end stop the camera does not move and consecutive frames are nearly identical."""
    if not a or not b:
        return True
    try:
        from PIL import Image, ImageChops, ImageStat
        ta = Image.open(io.BytesIO(a)).convert("L").resize((96, 54))
        tb = Image.open(io.BytesIO(b)).convert("L").resize((96, 54))
        return ImageStat.Stat(ImageChops.difference(ta, tb)).mean[0] > threshold
    except Exception:
        return True


def interval_for(cam: Dict[str, Any], base_min: float, battery: Optional[float], daylight: bool,
                 residents_home: List[str]) -> Optional[float]:
    """Minutes until this camera's next sweep, or None to skip it for now (the owner's rules, 2026-09-25)."""
    if cam.get("quiet_when_home") and residents_home:
        return None                     # indoor motor noise: not while a resident is home
    if cam.get("battery_sensor"):
        if battery is None or battery < 30:
            return None                 # unknown or low: let the panel catch up
        if daylight and battery >= 60:
            return base_min
        return 60.0
    return base_min


def residents_home(people: List[str], ha_person_state: Callable[[str], Optional[str]],
                   minutes_since_seen: Dict[str, Optional[float]], seen_window_min: float = 90) -> List[str]:
    """Who counts as home. An HA person entity (phone GPS) is trusted when it has a state; someone without one
    (no tracker in HA) counts as home if a camera saw them within seen_window_min."""
    home = []
    for name in people:
        state = ha_person_state(name)
        if state in ("home", "not_home") or (state and state not in ("unknown", "unavailable")):
            if state == "home":
                home.append(name)
            continue
        seen = minutes_since_seen.get(name.lower())
        if seen is not None and seen <= seen_window_min:
            home.append(name)
    return home


class Patrol:
    def __init__(self, cfg_fn: Callable[[], Dict[str, Any]], ha: Any, frame_fn: Callable[[str], Optional[bytes]],
                 vision_fn: Callable[[bytes, str, List[Dict[str, str]]], Dict[str, Any]],
                 frigate_in_view: Callable[[str], List[Dict[str, Any]]], presence_fn: Callable[[], Dict[str, Any]],
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 state_path: Optional[str] = None):
        self.cfg_fn, self.ha, self.frame_fn, self.vision_fn = cfg_fn, ha, frame_fn, vision_fn
        self.frigate_in_view, self.presence_fn = frigate_in_view, presence_fn
        self.clock, self.sleep = clock, sleep
        self.state_path = state_path
        self.state: Dict[str, Dict[str, Any]] = {}     # entity -> {last, next, frames, skip, busy, interval_s}
        self._load_state()
        self.sightings: Dict[str, Dict[str, Any]] = {} # identity -> newest patrol sighting
        self.frames: Dict[str, List[bytes]] = {}       # entity -> jpegs of the last sweep
        self.manual_at: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.on_sweep: Optional[Callable[[Dict[str, Any]], None]] = None   # the trace (courage/trace.py): one record per sweep
        # "Sweep now" is a person-started use of the vision engine: this runs first, on the sweep's thread, and may take the engine back from
        # background work that holds it (the garden). Frames read while it is still lent out are deferred (see _look / deliver_late).
        self.before_manual: Optional[Callable[[], Any]] = None
        self._tl = threading.local()                    # what the vision function needs to queue the frame being read

    # -------------------------------------------------------------- config ----
    def _pcfg(self) -> Dict[str, Any]:
        return self.cfg_fn().get("patrol") or {}

    def _load_state(self) -> None:
        """Last sweep times survive a StoneSage restart (otherwise every deploy wakes the solar camera for a sweep)."""
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as f:
                for entity, last in (json.load(f) or {}).items():
                    self.state[entity] = {"last": float(last)}
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({e: s["last"] for e, s in self.state.items() if s.get("last")}, f)
            import os
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def note_manual(self, entity: str, wait_s: float = 0) -> None:
        """Someone else is steering this camera (the owner's PTZ buttons, Courage's camera_scan): a sweep in progress stops
        before its next move and no sweep starts for MANUAL_HOLD_S. wait_s: block (real time) until a running sweep
        has let go, so the caller's first move is not undone by the sweep's last one."""
        self.manual_at[entity] = self.clock()
        deadline = time.monotonic() + wait_s
        while self.state.get(entity, {}).get("busy") and time.monotonic() < deadline:
            time.sleep(0.2)

    def _claim(self, entity: str) -> bool:
        """Mark a camera busy; False if a sweep already holds it (scheduler and "Sweep now" must not overlap)."""
        with self._lock:
            st = self.state.setdefault(entity, {})
            if st.get("busy"):
                return False
            st["busy"] = True
            return True

    def _twice(self, call: Callable[[], Dict[str, Any]], wait: float) -> Dict[str, Any]:
        """A camera command, retried once after `wait` s: the Tapo cameras sometimes answer HA with a 500 while busy
        (seen 2026-09-25 on the kitchen C260 for presses and preset saves; the retry went through)."""
        res = call() or {}
        if res.get("ok", True):
            return res
        self.sleep(wait)
        return call() or {}

    def _ha_state(self, entity_id: str) -> Optional[str]:
        return (self.ha.get_state(entity_id) or {}).get("state")

    # ------------------------------------------------------------- schedule ----
    def due(self, entity: str, cam: Dict[str, Any], presence: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """None if this camera should sweep now, else the reason it waits (shown in the UI).
        presence: one presence read shared by the whole run (read here when not given)."""
        pcfg = self._pcfg()
        st = self.state.setdefault(entity, {})
        now = self.clock()
        if st.get("busy"):
            return "sweeping"
        if now - self.manual_at.get(entity, 0) < MANUAL_HOLD_S:
            return "moved by hand recently"
        people = pcfg.get("residents") or list((self.cfg_fn().get("household") or {}).get("people") or [])
        if presence is None:
            presence = self.presence_fn() or {}
        seen = {n: (v or {}).get("minutes_ago") for n, v in (presence.get("locations") or {}).items()}
        home = residents_home(people, lambda n: self._ha_state(f"person.{n.lower()}"), seen)
        battery = None
        if cam.get("battery_sensor"):
            try:
                battery = float(self._ha_state(cam["battery_sensor"]))
            except (TypeError, ValueError):
                battery = None
        daylight = self._ha_state("sun.sun") == "above_horizon"
        minutes = interval_for(cam, float(pcfg.get("interval_min", 15)), battery, daylight, home)
        if minutes is None:
            reason = f"paused: {', '.join(home)} home" if cam.get("quiet_when_home") and home else \
                     f"paused: battery {battery if battery is not None else '?'}%"
            st.update(skip=reason, next=None)
            return reason
        nxt = st.get("last", 0) + minutes * 60
        st.update(skip=None, next=nxt, interval_s=minutes * 60)
        return None if now >= nxt else "waiting"

    # ---------------------------------------------------------------- sweep ----
    def sweep(self, entity: str, cam: Dict[str, Any], presence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Claim the camera and sweep it (in this thread). Refused while another sweep holds the camera."""
        if not self._claim(entity):
            return {"frames": 0, "seen": [], "skipped": "already sweeping"}
        return self._sweep(entity, cam, presence)

    def _sweep(self, entity: str, cam: Dict[str, Any], presence: Optional[Dict[str, Any]] = None,
               reason: str = "scheduled") -> Dict[str, Any]:
        """The sweep itself; the caller has claimed the camera (busy is cleared here)."""
        st = self.state.setdefault(entity, {})
        frames: List[bytes] = []
        seen_log: List[Dict[str, Any]] = []
        interrupted = False
        pcfg = self._pcfg()
        ptz = cam["ptz"]
        step = int(cam.get("step_deg", pcfg.get("step_deg", 30)))
        max_frames = int(cam.get("max_frames", pcfg.get("max_frames", 9)))
        settle = float(pcfg.get("settle_s", 3))
        # No frame at the left end stop by default (one step in): there the driveway looks into the house wall and
        # the kitchen at the ceiling. start_deg overrides it per camera.
        start = int(cam.get("start_deg", int(cam.get("skip_steps", pcfg.get("skip_steps", 1))) * step))
        started = self.clock()
        angle_eid = f"number.{ptz}_movement_angle"
        angle_before: float = DEFAULT_ANGLE

        def move(button: str) -> None:
            for attempt in (1, 2):
                if self.manual_at.get(entity, 0) >= started:
                    raise Interrupted()
                res = self.ha.press_button(f"button.{ptz}_move_{button}") or {}
                if res.get("ok", True):
                    return
                stats["move_errors"] = stats.get("move_errors", 0) + 1
                stats["move_error"] = str(res.get("error"))[:200]
                if attempt == 1:
                    self.sleep(settle)                       # the camera may still be busy with the last move
            raise MoveFailed()

        saved = False
        stats: Dict[str, Any] = {"vision_calls": 0, "vision_ms": 0.0, "vision_errors": 0, "deferred": 0}
        stopped, returned, error = "max_frames", "none", None
        try:
            profiles = self._profiles(presence if presence is not None else (self.presence_fn() or {}))
            try:
                angle_before = float(self._ha_state(angle_eid))  # the owner's step size, put back afterwards
            except (TypeError, ValueError):
                pass
            # Where the camera points now (the owner's last position, or wherever it was left) is where it goes back to
            res = self._twice(lambda: self.ha.call_service("tapo_control", "save_preset",
                                                           {"entity_id": entity, "name": RETURN_PRESET},
                                                           timeout=PRESET_TIMEOUT_S), 2)
            saved = bool(res.get("ok", True))
            if not saved:
                stats["save_error"] = str(res.get("error"))[:200]
            self.ha.call_service("number", "set_value", {"entity_id": angle_eid, "value": 120})
            for _ in range(LEFT_END_MOVES):
                errors_before, error_before = stats.get("move_errors", 0), stats.get("move_error")
                try:
                    move("left")
                except MoveFailed:
                    stats["move_errors"] = errors_before                  # expected here: not a move_error
                    if error_before is None:
                        stats.pop("move_error", None)
                    # Already at the left end stop: the camera refuses with "Maximum Pan/Tilt range reached
                    # (MOTOR_LOCKED_ROTOR)", which reaches us only as a bare HTTP 500. Reaching the end stop is the
                    # point of these moves, so carry on. 2026-09-28: sweeps left the camera parked there, and every
                    # later sweep stopped at frame 0 on this (3 of 3 after the return-preset fix). A camera that is
                    # truly unreachable fails the next (right) move too, and that still ends the sweep.
                    stats["left_end_refused"] = True
                    break
                self.sleep(settle + 1)
            left = start
            while left > 0:                                  # to the first frame, in moves of <= 120 deg
                chunk = min(120, left)
                self.ha.call_service("number", "set_value", {"entity_id": angle_eid, "value": chunk})
                move("right")
                self.sleep(settle)
                left -= chunk
            self.ha.call_service("number", "set_value", {"entity_id": angle_eid, "value": step})
            prev = None
            for i in range(max_frames):
                pan = start + i * step
                self.sleep(settle)
                frame = self.frame_fn(cam["frames"])
                if frame is None:
                    stopped = "frame_missing"
                    break
                if i > 0 and not frames_differ(prev, frame):
                    stopped = "end_stop"
                    break                                    # did not move: right end stop
                frames.append(frame)
                prev = frame
                names = self._look(cam, frame, pan, profiles, stats, entity)
                if names:
                    seen_log.append({"pan": pan, "seen": names})
                    self._record(entity, cam, names, len(frames) - 1, pan)
                if i < max_frames - 1:
                    move("right")
        except Interrupted:
            interrupted = True                               # the owner or Courage has the camera: no trip home either
        except MoveFailed:
            stopped = "move_failed"                          # keep the frames so far, then go back as usual
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:200]
            raise
        finally:
            try:
                value = int(angle_before) if float(angle_before).is_integer() else angle_before
                self.ha.call_service("number", "set_value", {"entity_id": angle_eid, "value": value})
                if not interrupted:
                    back = (self._twice(lambda: self.ha.select_option(f"select.{ptz}_move_to_preset", RETURN_PRESET,
                                                                       timeout=PRESET_TIMEOUT_S), settle)
                            if saved else {"ok": False})
                    returned = "start"
                    if not (back or {}).get("ok", True):
                        returned = "failed"
                        stats["return_error"] = str((back or {}).get("error") or "not saved")[:200]
                        if cam.get("home_preset"):
                            home = self._twice(lambda: self.ha.select_option(f"select.{ptz}_move_to_preset",
                                                                             cam["home_preset"], timeout=PRESET_TIMEOUT_S),
                                               settle)
                            returned = "home" if (home or {}).get("ok", True) else "failed"
                            if returned == "failed":
                                stats["home_error"] = str((home or {}).get("error"))[:200]
                # Even when the save looked failed: HA's client gives up after 4 s, the camera may still have saved it
                self.ha.call_service("tapo_control", "delete_preset", {"entity_id": entity, "preset": RETURN_PRESET})
            finally:
                with self._lock:
                    self.frames[entity] = frames
                last = self.clock()
                st.update(busy=False, last=last, frames=len(frames), seen=seen_log, first_pan=start, step=step,
                          interrupted=interrupted, next=last + st["interval_s"] if st.get("interval_s") else None)
                self._save_state()
                stats["saved"] = saved
                self._trace(entity, cam, reason, started, last, frames, start, step, stopped, interrupted, returned,
                            error, seen_log, stats)
        return {"frames": len(frames), "seen": seen_log, "interrupted": interrupted}

    def _trace(self, entity, cam, reason, started, ended, frames, start, step, stopped, interrupted, returned, error,
               seen_log, stats) -> None:
        """One trace record per sweep (Engine Console -> COURAGE TRACE, kind 'patrol'). Triggers recorded only."""
        if not self.on_sweep:
            return
        triggers = []
        if not frames:
            triggers.append("no_frames")
        if stopped == "frame_missing":
            triggers.append("frame_error")
        if stats["vision_errors"]:
            triggers.append("vision_error")
        if stats.get("move_errors"):
            triggers.append("move_error")                    # a PTZ press was refused (retried once)
        if not stats.get("saved", True):
            triggers.append("save_failed")                   # could not remember the starting position
        if returned == "failed":
            triggers.append("return_failed")
        if interrupted:
            triggers.append("interrupted")
        outcome = "error" if error else "interrupted" if interrupted else "done" if frames else "no_frames"
        rec = {"kind": "patrol", "at": round(started, 3), "camera": cam.get("name", entity), "entity": entity,
               "reason": reason, "outcome": outcome, "ms": round((ended - started) * 1000), "frames": len(frames),
               "pans": [start + i * step for i in range(len(frames))], "stopped": "interrupted" if interrupted else stopped,
               "returned": returned, "saved_start": stats.get("saved", True), "seen": seen_log,
               "vision": {"calls": stats["vision_calls"], "ms": round(stats["vision_ms"]), "errors": stats["vision_errors"],
                          "deferred": stats.get("deferred", 0)},
               "triggers": triggers}
        if error:
            rec["error"] = error
        if stats.get("left_end_refused"):
            rec["left_end_refused"] = True                   # was already at the left end stop (normal)
        for k in ("save_error", "return_error", "home_error", "move_error"):   # HA's own words when a camera command failed
            if stats.get(k):
                rec[k] = stats[k]
        try:
            self.on_sweep(rec)
        except Exception:
            pass  # tracing must never break the patrol

    def context(self) -> Dict[str, Any]:
        """The frame being read on this thread: {entity, pan, camera, profiles}; for a vision function that has to queue it."""
        return dict(getattr(self._tl, "ctx", None) or {})

    def _look(self, cam: Dict[str, Any], frame: bytes, pan: int, profiles: List[Dict[str, str]],
              stats: Optional[Dict[str, Any]] = None, entity: str = "") -> List[str]:
        """Known profiles in this frame. On a Frigate camera the VLM only runs when Frigate is tracking someone. While the vision engine is
        lent out the vision function queues the frame and answers {"deferred": ...}: the names then come from Frigate's own detections
        (its face / pet identities) straight away, and the queue's late read adds whatever Frigate could not say (deliver_late)."""
        kind, _, name = cam["frames"].partition(":")
        in_view: List[Dict[str, Any]] = []
        if kind == "frigate":
            try:
                in_view = self.frigate_in_view(name) or []
                if not in_view:
                    return []
            except Exception:
                pass  # Frigate unreachable: look anyway
        self._tl.ctx = {"entity": entity, "pan": pan, "camera": cam.get("name", entity), "profiles": [p["name"] for p in profiles]}
        t = time.time()
        res = self.vision_fn(frame, f"{cam.get('name', '')} (pan {pan} deg)", profiles) or {}
        if res.get("deferred"):
            if stats is not None:
                stats["deferred"] = stats.get("deferred", 0) + 1
            valid = {p["name"].lower(): p["name"] for p in profiles}
            return [valid[o["name"].lower()] for o in in_view if isinstance(o.get("name"), str) and o["name"].lower() in valid]
        if stats is not None:
            stats["vision_calls"] += 1
            stats["vision_ms"] += (time.time() - t) * 1000
            stats["vision_errors"] += 1 if res.get("error") else 0
        valid = {p["name"].lower(): p["name"] for p in profiles}
        return [valid[n.lower()] for n in res.get("seen", []) if isinstance(n, str) and n.lower() in valid]

    def _record(self, entity: str, cam: Dict[str, Any], names: List[str], frame_idx: int, pan: int,
                seen_at: Optional[float] = None, late: bool = False) -> None:
        now = self.clock() if seen_at is None else seen_at
        with self._lock:
            for n in names:
                cur = self.sightings.get(_key(n))
                if late and cur is not None and cur["seen_at"] >= now:
                    continue                                  # a late read never replaces a newer sighting
                self.sightings[_key(n)] = {"seen_at": now, "entity": entity, "camera": cam.get("name", entity),
                                           "frame": frame_idx, "pan": pan, **({"late": True} if late else {})}

    def deliver_late(self, item: Dict[str, Any], text: str, frame: bytes) -> bool:
        """A frame the vision engine could not read during the sweep (lent out) was read afterwards (stonesage/vision_queue.py). Names from
        the answer become sightings dated when the frame was taken, never over a newer one; the frame is kept so the card can show it."""
        meta = item.get("meta") or {}
        entity = meta.get("entity") or ""
        cam = (self._pcfg().get("cameras") or {}).get(entity) or {}
        valid = {n.lower(): n for n in meta.get("profiles") or []}
        names = [valid[n.lower()] for n in parse_vision_json(text).get("seen", []) if isinstance(n, str) and n.lower() in valid]
        if not names or not entity:
            return False
        with self._lock:
            kept = self.frames.setdefault(entity, [])
            kept.append(frame)
            idx = len(kept) - 1
        self._record(entity, cam, names, idx, int(meta.get("pan") or 0), seen_at=float(item["at"]), late=True)
        return True

    # ----------------------------------------------------------- corrections ----
    def correct(self, ref: str, shown: str, action: str, name: str = "") -> Dict[str, Any]:
        """A correction from a Residents & Pets card. ref = '<entity>|<frame>' of the sighting shown as `shown`.
        Reject drops it; relabel re-files it under `name` (newest sighting per identity still wins). Returns the
        frame bytes too, so the caller can teach Frigate's face library when `name` is a person."""
        entity, _, idx = (ref or "").rpartition("|")
        if not entity or not idx.isdigit():
            return {"ok": False, "error": "bad patrol sighting ref"}
        old = _key(shown)
        with self._lock:
            rec = self.sightings.get(old)
            if not rec or rec["entity"] != entity or rec["frame"] != int(idx):
                return {"ok": False, "error": "that sighting has been replaced by a newer sweep"}
            del self.sightings[old]
            if action == "relabel":
                new = _key(name)
                cur = self.sightings.get(new)
                if cur is None or rec["seen_at"] >= cur["seen_at"]:
                    self.sightings[new] = rec
        self._log_correction({"source": "patrol", "entity": entity, "pan": rec["pan"], "shown": shown,
                              "action": action, "name": name or None})
        return {"ok": True, "name": name or None, "frame": self.frame(entity, int(idx))}

    def _log_correction(self, entry: Dict[str, Any]) -> None:
        """Append-only record next to the state file (tuning data for the patrol's vision prompt later)."""
        if not self.state_path:
            return
        import os
        entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with open(os.path.join(os.path.dirname(self.state_path), "patrol_corrections.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    @staticmethod
    def _profiles(presence: Dict[str, Any]) -> List[Dict[str, str]]:
        """Known profiles for the vision prompt: [{name, traits}] (traits = looks, or the species for a pet)."""
        ents = presence.get("known_entities") or {}
        return [{"name": e["name"], "traits": (e.get("traits") or e.get("species") or "")[:90]}
                for g in ("people", "pets") for e in ents.get(g, []) if e.get("name")]

    # ---------------------------------------------------------------- output ----
    def locations(self) -> Dict[str, Dict[str, Any]]:
        now = self.clock()
        with self._lock:
            items = list(self.sightings.items())
        return {k: {"minutes_ago": round((now - v["seen_at"]) / 60, 1), "mtime": v["seen_at"], "camera": v["camera"],
                    "doing": f"seen on patrol (pan {v['pan']} deg{', read late' if v.get('late') else ''})", "source": "patrol",
                    "patrol_ref": f"{v['entity']}|{v['frame']}",
                    "snapshot_url": f"/api/patrol/frame?entity={v['entity']}&i={v['frame']}"} for k, v in items}

    def frame(self, entity: str, i: int) -> Optional[bytes]:
        with self._lock:
            frames = self.frames.get(entity) or []
            return frames[i] if 0 <= i < len(frames) else None

    def status(self) -> Dict[str, Any]:
        cams = self._pcfg().get("cameras") or {}
        out = {}
        for entity, cam in cams.items():
            st = self.state.get(entity, {})
            out[entity] = {"name": cam.get("name", entity), "busy": st.get("busy", False), "last": st.get("last"),
                           "next": st.get("next"), "skip": st.get("skip"), "frames": st.get("frames", 0),
                           "seen": st.get("seen", []), "first_pan": st.get("first_pan", 0), "step": st.get("step", 30),
                           "interrupted": st.get("interrupted", False)}
        return {"enabled": bool(self._pcfg().get("enabled")), "cameras": out}

    # ------------------------------------------------------------------ loop ----
    def run_once(self) -> None:
        pcfg = self._pcfg()
        if not pcfg.get("enabled"):
            return
        presence = self.presence_fn() or {}            # one read for every camera and frame of this run
        for entity, cam in (pcfg.get("cameras") or {}).items():
            if self.due(entity, cam, presence) is None:
                self.sweep(entity, cam, presence)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                try:
                    self.run_once()
                except Exception as e:  # never let one bad sweep stop the patrol
                    import logging
                    logging.getLogger("StoneSage.Patrol").warning(f"patrol: {type(e).__name__}: {e}")
                self.sleep(60)

        self._thread = threading.Thread(target=loop, name="ptz-patrol", daemon=True)
        self._thread.start()

    def run_now(self, entity: str) -> Dict[str, Any]:
        """Manual sweep (UI button), in the background; refused while that camera is already sweeping."""
        cam = (self._pcfg().get("cameras") or {}).get(entity)
        if not cam:
            return {"ok": False, "error": "not a patrol camera"}
        if not self._claim(entity):                    # claimed here, so the scheduler cannot start a second sweep
            return {"ok": False, "error": "already sweeping"}
        self.manual_at.pop(entity, None)

        def go() -> None:
            if self.before_manual:
                try:
                    self.before_manual()
                except Exception:
                    pass                                     # the sweep goes ahead; frames the engine cannot read are queued
            self._sweep(entity, cam, None, "manual")

        threading.Thread(target=go, daemon=True).start()
        return {"ok": True}


def parse_vision_json(text: str) -> Dict[str, Any]:
    """The VLM's reply -> {"seen": [...], "note": str}; tolerant of code fences and chatter around the JSON."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"seen": [], "note": (text or "")[:200]}
    try:
        d = json.loads(m.group(0))
        return {"seen": [s for s in d.get("seen", []) if isinstance(s, str)], "note": str(d.get("note", ""))[:200]}
    except Exception:
        return {"seen": [], "note": (text or "")[:200]}
