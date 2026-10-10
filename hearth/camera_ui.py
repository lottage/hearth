"""
Camera tiles for the StoneSage LIVE tab: how each camera is shown, snapshots and PTZ.

Per camera (config.json camera_ui, keyed by HA camera entity):
  live: "webrtc"   go2rtc stream played over WebRTC: a Frigate camera (frigate.cameras), or any go2rtc stream
                   named by go2rtc: (the solar TCW90 via tapo://, which go2rtc only opens while someone watches)
        "snapshot" still frame only, refreshed on demand (battery TC82s must not be kept awake)
  ptz:  HA entity prefix of a Tapo PTZ camera: button.<ptz>_move_up/down/left/right and
        select.<ptz>_move_to_preset (preset names are read from HA live, never hardcoded)
  min_refresh_s: snapshots newer than this are served from cache instead of waking the camera again

(HA's own HLS stream was tried for the TCW90 and dropped on 2026-09-25: HA's muxer failed on about half the starts.)
"""

import io
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

MOVES = ("up", "down", "left", "right")
STALE_S = 300   # a tile opening (no refresh press) fetches a new frame when the cached one is older than this

_snap_cache: Dict[str, Tuple[float, bytes]] = {}
_snap_lock = threading.Lock()


def camera_entries(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return cfg.get("camera_ui") or {}


def presets(ptz: str, get_state: Callable[[str], Optional[Dict[str, Any]]]) -> List[str]:
    """Preset names exactly as HA spells them (the driveway's 'Driveway ' has a trailing space), without the
    patrol's temporary return preset."""
    from hearth.patrol import is_temp_preset
    st = get_state(f"select.{ptz}_move_to_preset") or {}
    return [p for p in (st.get("attributes") or {}).get("options") or [] if not is_temp_preset(p)]


def variant_streams(cfg: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """HA entity -> {"low"|"hd": go2rtc stream}: the on-demand transcodes in Frigate's go2rtc config."""
    return {e: dict(c.get("variants") or {}) for e, c in camera_entries(cfg).items() if c.get("variants")}


def list_cameras(cfg: Dict[str, Any], get_state: Callable[[str], Optional[Dict[str, Any]]],
                 webrtc_streams: Dict[str, str], available: Optional[set] = None) -> List[Dict[str, Any]]:
    """Tiles in config order. webrtc_streams: HA entity -> go2rtc stream; available: go2rtc stream names
    (variants go2rtc does not have are left out of the tile's quality menu)."""
    out = []
    for entity, c in camera_entries(cfg).items():
        live = "webrtc" if c.get("live") == "webrtc" else "snapshot"
        tile = {"entity": entity, "name": c.get("name", entity), "live": live,
                "min_refresh_s": int(c.get("min_refresh_s", 0))}
        if live == "webrtc":
            if entity not in webrtc_streams:
                tile["live"] = "snapshot"  # Frigate/go2rtc down: still show something
            else:
                tile["stream"] = webrtc_streams[entity]
                tile["variants"] = {k: v for k, v in (c.get("variants") or {}).items()
                                    if available is None or v in available}
        if c.get("ptz"):
            try:
                tile["ptz"] = {"presets": presets(c["ptz"], get_state)}
            except Exception:
                tile["ptz"] = {"presets": []}
        out.append(tile)
    return out


# ------------------------------------------------------------ snapshots ----
def snapshot(entity: str, cfg: Dict[str, Any], fetch: Callable[[str], Optional[bytes]],
             force: bool = False, width: int = 640, clock: Callable[[], float] = time.time) -> Tuple[Optional[bytes], float]:
    """(jpeg, age_s). A cached frame younger than the camera's min_refresh_s is returned even when force=True,
    so a refresh button cannot keep a battery camera awake. Without force (a tile opening) the cache serves up to
    STALE_S (or min_refresh_s if longer), so an opened tile never shows a frame from hours ago."""
    c = camera_entries(cfg).get(entity)
    if c is None:
        raise KeyError(entity)
    min_age = int(c.get("min_refresh_s", 0))
    now = clock()
    with _snap_lock:
        cached = _snap_cache.get(entity)
    if cached and now - cached[0] < (min_age if force else max(min_age, STALE_S)):
        return cached[1], now - cached[0]
    raw = fetch(entity)
    if not raw:
        return (cached[1], now - cached[0]) if cached else (None, 0.0)
    img = _shrink(raw, width)
    with _snap_lock:
        _snap_cache[entity] = (now, img)
    return img, 0.0


def _shrink(raw: bytes, width: int) -> bytes:
    """Tapo snapshots are up to 4K; a tile needs ~640 px."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        if im.width <= width:
            return raw
        im = im.convert("RGB").resize((width, round(im.height * width / im.width)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception:
        return raw


# ------------------------------------------------------------------ PTZ ----
def ptz_command(entity: str, cfg: Dict[str, Any], action: str, preset: str,
                get_state: Callable[[str], Optional[Dict[str, Any]]]) -> Tuple[str, str, Optional[str]]:
    """Validated HA call for a PTZ request: ('button', entity_id, None) or ('select', entity_id, option)."""
    c = camera_entries(cfg).get(entity) or {}
    ptz = c.get("ptz")
    if not ptz:
        raise ValueError("camera has no PTZ")
    if action in MOVES:
        return "button", f"button.{ptz}_move_{action}", None
    if action == "preset":
        if preset not in presets(ptz, get_state):
            raise ValueError(f"unknown preset '{preset}'")
        return "select", f"select.{ptz}_move_to_preset", preset
    raise ValueError(f"unknown PTZ action '{action}'")
