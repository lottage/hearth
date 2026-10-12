"""Home services: presence (Frigate, sentry, patrol, GPS), camera looks and scans, commentary, the Alexa relay,
AdGuard blocks, presence in Home Assistant, sighting corrections and boxes, Computer's phone/Echo/voice helpers.
Moved out of server.py by the layer split (docs/LAYERS.md, 2026-10-09) with bodies unchanged. What the platform
provides is bound by bind(ctx) (called from hearth/app.py install): context fields, named platform services, and
the names server.py still lends (HOME_LENT). The platform reaches these services only through hooks."""

from PIL import Image
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import base64
import io
import json
import os
import re
import subprocess
import threading
import time
from lodestone import endpoints
from lodestone.site import clock
import urllib.request
import logging

from hearth.ha_policy import extract_whitelisted_chat_action, validate_ha_action
from hearth.hass_client import HomeAssistantClient
from hearth.immich_client import ImmichClient

logger = logging.getLogger("StoneSage.home")
immich = None

# bound by bind(app_ctx)
LOCAL_TZ = None          # the installation's time zone (platform service, lodestone/site.py)
ROOT_DIR = None
_engine_lease = None
_on_loan = None
_stonesage_data_dir = None
config = None
get_courage_agent = None
get_trace_log = None
hass = None
load_config = None


def camera_ptz_registry() -> Dict[str, Dict[str, Any]]:
    """HA camera entity -> its PTZ controls, for the cameras config.json camera_ui gives a "ptz" (the HA entity prefix of
    a Tapo PTZ camera). presets: optional list in camera_ui; the camera's own list is read live when it is scanned.
    default_preset: camera_ui, else patrol.cameras.<entity>.home_preset: where a scan leaves the camera."""
    out: Dict[str, Dict[str, Any]] = {}
    patrol = (config.get("patrol") or {}).get("cameras") or {}
    for entity, ui in (config.get("camera_ui") or {}).items():
        ptz = ui.get("ptz") if isinstance(ui, dict) else None
        if not ptz:
            continue
        out[entity] = {
            "name": ui.get("name") or entity,
            "preset_entity": f"select.{ptz}_move_to_preset",
            "presets": list(ui.get("presets") or []),
            "default_preset": ui.get("default_preset") or (patrol.get(entity) or {}).get("home_preset") or "",
            "move_buttons": {d: f"button.{ptz}_move_{d}" for d in ("up", "down", "left", "right")},
            "angle_entity": f"number.{ptz}_movement_angle",
        }
    return out


_QUERY_STOP = {"the", "and", "cam", "camera", "room", "door"}


def _query_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _QUERY_STOP}


def capture_live_camera_perception(user_query: str) -> Optional[str]:
    """
    Captures a real-time frame from the most relevant physical camera via Home Assistant proxy,
    resizes to max 640px Lanczos (< 200 vision tokens), and runs optical perception via
    the vision engine (config cluster.vision_url) in a few seconds.
    """
    cam_eid, cam_name = _select_camera_for_query(user_query)
    if not cam_eid:
        return None

    # Fetch snapshot from HA proxy
    img_bytes = hass.get_camera_snapshot(cam_eid)
    if not img_bytes:
        others = [(c["entity"], c["name"]) for c in _household().cameras.values() if c["entity"] != cam_eid]
        alt_eid, alt_name = others[0] if others else ("", "")
        img_bytes = hass.get_camera_snapshot(alt_eid) if alt_eid else None
        if img_bytes:
            cam_eid, cam_name = alt_eid, alt_name
        else:
            return None

    try:
        b64_img = None
        if Image is not None:
            raw_img = Image.open(io.BytesIO(img_bytes))
            w, h = raw_img.size
            scale = min(448 / max(w, h), 1.0)
            nw = max(28, (int(w * scale) // 28) * 28)
            nh = max(28, (int(h * scale) // 28) * 28)
            resized = raw_img.resize((nw, nh), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=80)
            b64_img = base64.b64encode(buf.getvalue()).decode("utf-8")
        else:
            b64_img = base64.b64encode(img_bytes).decode("utf-8")

        prompt = (
            f"You are the real-time optical visual perception system for the attic Computer on camera '{cam_name}'.\n"
            "Describe accurately and objectively what is visible in this camera frame right now in 2 concise sentences.\n"
            + _lookout_sentence()
        )
        payload = {
            "model": "vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                        {"type": "text", "text": prompt}
                    ]
                }
            ],
            "max_tokens": 90,
            "temperature": 0.1
        }
        v_base = endpoints.url("vision", config).rstrip("/")
        vision_url = f"{v_base}/chat/completions" if not v_base.endswith("/chat/completions") else v_base
        req = urllib.request.Request(
            vision_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=50.0) as resp:
            v_data = json.loads(resp.read().decode("utf-8"))
            content = v_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            now_str = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p %Z")
            return (
                f"### [LIVE OPTICAL PERCEPTION FEED: REAL-TIME FRAME CAPTURE ({cam_name})]\n"
                f"Capture Time: {now_str} (Live Home Assistant Camera Proxy Snapshot)\n"
                f"Visual Analysis: {content}\n"
                f"STATUS: Real-time visual observation is ACTIVE. You have verified live visual perception."
            )
    except Exception as e:
        logger.warning(f"Live camera perception failed on '{cam_name}': {e}")
        return None


def _select_camera_for_query(user_query: str) -> Tuple[str, str]:
    """The camera a question is about: (entity, name). The one whose key or name shares a word with the question
    (config.json household.cameras, or camera_ui), else the household's default camera. ("", "") without cameras."""
    hh = _household()
    q = _query_words(user_query)
    for key, cam in hh.cameras.items():
        if q & _query_words(key.replace("_", " ") + " " + str(cam.get("name", ""))):
            return cam["entity"], cam.get("name", key)
    default = hh.cameras.get(hh.default_camera) or next(iter(hh.cameras.values()), None)
    return (default["entity"], default.get("name", "")) if default else ("", "")


def _analyze_single_frame(cam_eid: str, cam_name: str, preset_label: str = "",
                          img_bytes: Optional[bytes] = None) -> Optional[str]:
    """Run vision analysis on one frame (fetched from HA unless the caller passes one) with retry for GPU recovery."""
    img_bytes = img_bytes or hass.get_camera_snapshot(cam_eid)
    if not img_bytes:
        return None

    try:
        if Image is not None:
            raw_img = Image.open(io.BytesIO(img_bytes))
            w, h = raw_img.size
            scale = min(448 / max(w, h), 1.0)
            nw = max(28, (int(w * scale) // 28) * 28)
            nh = max(28, (int(h * scale) // 28) * 28)
            resized = raw_img.resize((nw, nh), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=80)
            b64_img = base64.b64encode(buf.getvalue()).decode("utf-8")
        else:
            b64_img = base64.b64encode(img_bytes).decode("utf-8")

        angle_hint = f" (Preset: {preset_label})" if preset_label else ""
        prompt = (
            f"You are the real-time optical visual perception system for the attic Computer on camera '{cam_name}'{angle_hint}.\n"
            "Describe accurately and objectively what is visible in this camera frame right now in 2 concise sentences.\n"
            + _lookout_sentence()
        )
        payload = {
            "model": "vision",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                {"type": "text", "text": prompt}
            ]}],
            "max_tokens": 90,
            "temperature": 0.1
        }
        v_base = endpoints.url("vision", config).rstrip("/")
        vision_url = f"{v_base}/chat/completions" if not v_base.endswith("/chat/completions") else v_base

        # Retry with backoff — Vulkan GPU needs recovery time between multimodal requests
        max_retries = 3
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(
                    vision_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=50.0) as resp:
                    v_data = json.loads(resp.read().decode("utf-8"))
                    return v_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            except urllib.error.HTTPError as he:
                if he.code == 500 and attempt < max_retries - 1:
                    wait = 4.0 * (attempt + 1)  # 4s, 8s backoff
                    logger.warning(f"Vision 500 on '{preset_label}' attempt {attempt+1}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise
    except Exception as e:
        logger.warning(f"Frame analysis failed on '{cam_name}' preset='{preset_label}': {e}")
        return None


def scan_room_with_camera(user_query: str) -> Optional[str]:
    """
    Performs a multi-angle PTZ room scan by cycling through all camera presets,
    capturing and analyzing a frame at each position. Returns a composite perception report.
    For cameras without PTZ presets, falls back to single-frame capture.
    """
    cam_eid, cam_name = _select_camera_for_query(user_query)
    # Cameras without PTZ (battery cameras) — single frame
    if not camera_ptz_registry().get(cam_eid):
        return capture_live_camera_perception(user_query)
    return scan_camera_presets(cam_eid, cam_name)


def scan_camera_presets(cam_eid: str, cam_name: str) -> Optional[str]:
    """PTZ sweep of one camera through its presets; single analysed frame for cameras without presets."""
    ptz_config = camera_ptz_registry().get(cam_eid)
    if not ptz_config:
        return _analyze_single_frame(cam_eid, cam_name)
    # A patrol sweep on this camera stops before its next move, and we wait (<= 10 s) until it has let go
    get_patrol().note_manual(cam_eid, wait_s=10)

    preset_entity = ptz_config["preset_entity"]
    # HA's own spelling wins over the registry: the driveway preset is "Driveway " (trailing space) in HA,
    # so selecting the registry's "Driveway" failed silently
    from hearth.patrol import is_temp_preset
    live_opts = [p for p in ((hass.get_state(preset_entity) or {}).get("attributes") or {}).get("options") or []
                 if not is_temp_preset(p)]   # the patrol's return preset can linger in HA's list
    presets = live_opts or ptz_config["presets"]
    if not presets:       # a PTZ camera that reports no presets: one frame where it points
        return _analyze_single_frame(cam_eid, cam_name)
    default_preset = next((p for p in presets if p.strip() == ptz_config.get("default_preset", "").strip()), presets[0])
    now_str = datetime.now(LOCAL_TZ).strftime("%I:%M:%S %p %Z")

    scan_results = []
    for i, preset_name in enumerate(presets):
        # Move camera to this preset position
        move_res = hass.select_option(preset_entity, preset_name)
        if not move_res.get("ok"):
            logger.warning(f"PTZ move to preset '{preset_name}' failed: {move_res.get('error')}")
            continue

        # Wait for camera to physically settle at new position
        time.sleep(3.5)

        # Capture and analyze frame at this position
        analysis = _analyze_single_frame(cam_eid, cam_name, preset_label=preset_name)
        if analysis:
            scan_results.append(f"**[{preset_name}]**: {analysis}")
        else:
            scan_results.append(f"**[{preset_name}]**: (Frame capture or analysis failed at this position)")

        # GPU cooldown between sequential vision requests (skip after last)
        if i < len(presets) - 1:
            time.sleep(2.0)

    # Return camera to default position after scan
    try:
        hass.select_option(preset_entity, default_preset)
    except Exception:
        pass

    if not scan_results:
        return None

    composite = "\n".join(f"- {r}" for r in scan_results)
    return (
        f"### [LIVE PTZ ROOM SCAN: MULTI-ANGLE PERCEPTION ({cam_name})]\n"
        f"Scan Time: {now_str} | Presets Scanned: {len(scan_results)}/{len(presets)}\n"
        f"Camera physically panned to each preset position and captured a live snapshot at each angle.\n\n"
        f"{composite}\n\n"
        f"STATUS: Multi-angle PTZ scan COMPLETE. Camera returned to default position '{default_preset}'."
    )


def _household():
    """Who and what this home has (config.json "household", cameras from camera_ui): hearth/household.py."""
    from hearth.household import household_from_config
    return household_from_config(config)


def _lookout_sentence() -> str:
    """The vision prompt's last line: who to look for, as the household describes them (config.json household)."""
    who = _household().lookout_text()
    return (f"Specifically note if {who}, any visitor, or animals are visible." if who
            else "Specifically note if any person, visitor, or animal is visible.")


def _camera_names() -> str:
    """The household's camera names, for prompts: 'Kitchen, Driveway' (config.json household / camera_ui)."""
    return ", ".join(str(c.get("name", k)) for k, c in _household().cameras.items()) or "none configured"


def _pet_keys() -> List[str]:
    return [n.lower() for n in _household().pets]


def _phone_service(target: str = "") -> Optional[str]:
    """HA notify service of a household phone; the first registered one when no target is named."""
    from hearth.household import phone_services
    phones = phone_services(config)
    return phones.get(target) if target else next(iter(phones.values()), None)


_courage_presence_cache: Dict[str, Any] = {"at": 0.0, "state": None, "refreshing": False}


def _courage_refresh_presence() -> None:
    try:
        from harness.core.home_presence_hub import home_presence_hub
        _courage_presence_cache.update(state=home_presence_hub.get_full_presence_state(), at=time.time())
    except Exception as e:
        logger.warning(f"Courage presence refresh failed: {e}")
    finally:
        _courage_presence_cache["refreshing"] = False


_frigate_presence = None


def get_frigate_presence():
    """Live Frigate sightings (backend/frigate_presence.py), or None when config.json has no frigate.url."""
    global _frigate_presence
    if _frigate_presence is None:
        fcfg = load_config().get("frigate") or {}
        if not fcfg.get("url"):
            return None
        from hearth.frigate_presence import FrigatePresence
        _frigate_presence = FrigatePresence(fcfg["url"], fcfg.get("identities"), fcfg.get("min_score", 0.7))
    return _frigate_presence


_patrol = None


def _patrol_frame(source: str) -> Optional[bytes]:
    """'frigate:<camera>' -> Frigate's newest frame; 'go2rtc:<stream>' -> a frame from go2rtc (wakes the camera)."""
    kind, _, name = source.partition(":")
    cfg = load_config()
    try:
        if kind == "frigate":
            fp = get_frigate_presence()
            return fp.latest_frame(name, 720) if fp else None
        if kind == "go2rtc":
            url = (cfg.get("frigate") or {}).get("go2rtc_url", "").rstrip("/")
            with urllib.request.urlopen(f"{url}/api/frame.jpeg?src={urllib.parse.quote(name)}", timeout=20) as r:
                return r.read()
    except Exception as e:
        logger.warning(f"patrol frame from {source}: {e}")
    return None


def _patrol_vision(frame: bytes, where: str, profiles: List[Dict[str, str]]) -> Dict[str, Any]:
    """Ask the vision model which known profiles ([{name, traits}]) are in a patrol frame; JSON answer, names checked
    by the caller."""
    from hearth.patrol import parse_vision_json
    if _on_loan("vision"):
        return {"seen": [], "skipped": "vision lent out (engine lease)"}
    who = "; ".join(f"{p['name']}: {p.get('traits', '')}" for p in profiles)
    img = frame
    if Image is not None:
        im = Image.open(io.BytesIO(frame)).convert("RGB")
        if im.width > 640:
            im = im.resize((640, round(im.height * 640 / im.width)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        img = buf.getvalue()
    prompt = (f"Camera {where}. Known household: {who}. Which of them are visible in this frame? "
              'Reply with JSON only: {"seen": [names from the list], "note": "one short sentence of what is in view"}. '
              "Use an empty list if none of them is clearly visible.")
    payload = {"model": "vision", "max_tokens": 80, "temperature": 0.1, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}},
        {"type": "text", "text": prompt}]}]}
    v_base = endpoints.url("vision", config).rstrip("/")
    url = v_base if v_base.endswith("/chat/completions") else f"{v_base}/chat/completions"
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.load(r)["choices"][0]["message"]["content"]
        return parse_vision_json(text)
    except Exception as e:
        logger.warning(f"patrol vision on {where}: {e}")
        return {"seen": [], "error": f"{type(e).__name__}: {e}"[:200]}   # counted in the patrol's trace


def get_patrol():
    global _patrol
    if _patrol is None:
        from hearth.patrol import Patrol
        fp = get_frigate_presence()
        _patrol = Patrol(load_config, hass, _patrol_frame, _patrol_vision,
                         (lambda cam: fp.in_view_now(cam)) if fp else (lambda cam: [{"label": "unknown"}]),
                         _courage_presence,  # includes the patrol's own sightings: a resident seen on patrol = home
                         state_path=os.path.join(ROOT_DIR, "data", "patrol_state.json"))
        _patrol.on_sweep = get_trace_log().write
    return _patrol


_commentary = None


def _commentary_vision(jpeg: bytes, prompt: str) -> str:
    """One vision-model read of a frame (shrunk to 640 px wide), plain text back."""
    if _on_loan("vision"):
        raise RuntimeError("vision lent out (engine lease)")
    img = jpeg
    if Image is not None:
        im = Image.open(io.BytesIO(jpeg)).convert("RGB")
        if im.width > 640:
            im = im.resize((640, round(im.height * 640 / im.width)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        img = buf.getvalue()
    payload = {"model": "vision", "max_tokens": 60, "temperature": 0.1, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}},
        {"type": "text", "text": prompt}]}]}
    v_base = _vision_url().rstrip("/")
    url = v_base if v_base.endswith("/chat/completions") else f"{v_base}/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def _commentary_llm(messages: List[Dict[str, str]]) -> str:
    """Computer's remark: the coordinator, no tools, no thinking, one short line."""
    base = endpoints.url("coordinator", config).rstrip("/")
    brain = _engine_lease.brain("coordinator") if _engine_lease else None
    if brain and brain.get("mode") == "on_loan":
        raise RuntimeError("the coordinator is lent out (engine lease) and there is no stand-in")
    if brain:                                          # lite: the stand-in writes the one-liner
        base = brain["url"].rstrip("/") + "/v1"
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = {"model": "coordinator", "messages": messages, "temperature": 0.6, "max_tokens": 80,
            "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def _commentary_seen_since(name: str, since: float) -> Optional[str]:
    """The camera that saw this resident BY NAME at or after `since` (Frigate face, sentry, patrol), else None.
    Only asked while an arrival waits for confirmation; the cached presence read (Frigate live, hub <= ~1 min) is fine."""
    loc = ((_courage_presence() or {}).get("locations") or {}).get(name)
    if not loc or loc.get("minutes_ago") is None:
        return None
    return (loc.get("camera") or loc.get("room") or "a camera") if time.time() - loc["minutes_ago"] * 60 >= since - 30 else None


_ha_presence = None


def get_ha_presence():
    """StoneSage's merged presence published to Home Assistant sensors (backend/ha_presence.py)."""
    global _ha_presence
    if _ha_presence is None:
        from hearth.ha_presence import HAPresence
        _ha_presence = HAPresence(_courage_presence, hass.set_state)
    return _ha_presence


_adguard_blocks = None


def get_adguard_blocks():
    """Dashboard one-touch 'Block TikTok': an HA input_boolean drives AdGuard's blocked service (backend/adguard_blocks.py).
    None unless config.json adguard_blocks.enabled; the AdGuard login comes from env (ADGUARD_USER / ADGUARD_PASSWORD)."""
    global _adguard_blocks
    if _adguard_blocks is None:
        from hearth import adguard_blocks
        _adguard_blocks = adguard_blocks.from_config(
            config, lambda entity, on: hass.call_service("input_boolean", "turn_on" if on else "turn_off", {"entity_id": entity}))
    return _adguard_blocks


_alexa_relay = None


def _alexa_turn(history: List[Dict[str, str]], session_id: str):
    """One Computer turn for the Alexa skill: (text to speak, whether a yes/no is now pending so the Alexa session stays open)."""
    from stonesage.courage.agent import spoken, streamed
    agent = get_courage_agent()
    answer, got_final = "", False
    for ev in streamed(agent.run(history, session_id)):
        if ev["type"] in ("partial", "final"):
            got_final = got_final or ev["type"] == "final"
            answer += ev["content"]
        elif ev["type"] == "tool_call":
            logger.info(f"Courage (Alexa) tool: {ev.get('name')} {ev.get('arguments')}")
    if not got_final:
        answer += " My brain on :8001 isn't answering. Try again in a moment."
    return spoken(answer), bool(agent.pending.get(session_id))


def get_alexa_relay():
    """Alexa -> Computer through the relay (backend/alexa_relay.py, skill in deploy/alexa-skill/). None when not configured."""
    global _alexa_relay
    cfg = config.get("alexa_relay") or {}
    if _alexa_relay is None and cfg.get("enabled") and cfg.get("key"):
        from hearth.alexa_relay import AlexaRelay
        _alexa_relay = AlexaRelay(cfg["key"], _alexa_turn, announce=lambda text, room: _courage_speak(text, room),
                                  base_url=cfg.get("relay_url", "https://ntfy.sh"), allowed_users=cfg.get("allowed_users"),
                                  default_room=cfg.get("default_room", "kitchen"))
    return _alexa_relay


def _commentary_history(cameras: List[str], since: float) -> List[Tuple[float, str]]:
    """(time, camera) of every person Frigate tracked on these cameras since `since`: start and end of each event."""
    url = ((load_config().get("frigate") or {}).get("url") or "").rstrip("/")
    out: List[Tuple[float, str]] = []
    for cam in cameras if url else []:
        with urllib.request.urlopen(f"{url}/api/events?camera={urllib.parse.quote(cam)}&label=person"
                                    f"&after={int(since)}&limit=500", timeout=5) as r:
            for ev in json.load(r):
                out += [(float(t), cam) for t in (ev.get("start_time"), ev.get("end_time")) if t]
    return out


def get_commentary():
    """When Computer speaks unprompted (backend/commentary.py): arrivals, cooking, cleaning; config.json commentary."""
    global _commentary
    if _commentary is None:
        from hearth.commentary import Commentary
        fp = get_frigate_presence()
        _commentary = Commentary(load_config, hass.get_state,
                                 (lambda cam: fp.in_view_now(cam)) if fp else (lambda cam: []),
                                 (lambda cam: fp.latest_frame(cam, 720)) if fp else (lambda cam: None),
                                 _commentary_vision, _commentary_llm, _courage_speak, trace=get_trace_log().write,
                                 state_path=os.path.join(_stonesage_data_dir(), "commentary_state.json"),
                                 seen_since=_commentary_seen_since, history=_commentary_history,
                                 ha_history=hass.get_history)
    return _commentary


def _apply_presence_correction(source: str, ref: str, shown: str, action: str, name: str) -> Dict[str, Any]:
    """One correction of one sighting, the same for the card buttons (POST /api/presence/correct) and Computer's
    correct_sighting tool. action: reject | relabel. Caller invalidates the presence cache on success."""
    pc = _presence_corrections()
    if source == "patrol":
        # Patrol sightings live in the patrol's memory; a person correction also trains Frigate faces
        if action == "relabel":
            name = pc._canonical(name) or ""
        if action not in ("reject", "relabel") or (action == "relabel" and not name):
            return {"ok": False, "error": "unknown profile (add it first)" if action == "relabel" else "bad action"}
        res = get_patrol().correct(ref, shown, action, name)
        frame = res.pop("frame", None)
        if res.get("ok") and action == "relabel" and frame and pc._is_person(name):
            res["face_registered"] = pc.register_face_image(frame, name, "patrol.jpg")  # cropped to them
        return res
    res = pc.correct(source, ref, action, name)
    fp = get_frigate_presence()
    if res.get("ok") and source == "frigate" and fp:
        fp.apply_correction(ref, res.get("name") if action == "relabel" else None)
    return res


def _courage_correct_sighting(who: str, action: str, name: str = "") -> Dict[str, Any]:
    """Computer's correct_sighting tool: the newest sighting of `who` (what the Residents & Pets card shows) is
    re-filed under `name` (is_really) or hidden (wrong); reports what shows for `who` now."""
    from hearth.frigate_presence import norm_name
    key = norm_name(who)
    loc = ((_courage_presence() or {}).get("locations") or {}).get(key)
    if not loc:
        return {"ok": False, "error": f"there is no current sighting of {who} to correct"}
    source = loc.get("source") or "sentry"
    ref = loc.get("event_id") if source == "frigate" else loc.get("patrol_ref") if source == "patrol" else loc.get("snapshot")
    if not ref:
        return {"ok": False, "error": f"{who}'s latest sighting has no picture, so it cannot be corrected"}
    shown = "Someone" if key == "someone" else ((_profile(who) or {}).get("name") or who.capitalize())
    what = f"{shown} on the {loc.get('camera') or 'camera'}, {round(loc.get('minutes_ago') or 0)} min ago"
    res = _apply_presence_correction(source, ref, shown, "reject" if action == "wrong" else "relabel", name)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "the correction failed"}
    _invalidate_presence()
    nxt = ((_courage_presence() or {}).get("locations") or {}).get(key)
    out: Dict[str, Any] = {"ok": True, "corrected": what,
                           "now": "hidden as wrong" if action == "wrong" else f"filed under {res.get('name') or name}",
                           f"{shown}_latest_now": (f"{round(nxt.get('minutes_ago') or 0)} min ago on {nxt.get('camera')}"
                                                   if nxt else "no earlier sighting")}
    if res.get("faces_trained") or res.get("face_registered") is True:
        out["face_recognition"] = "taught from this sighting"
    return out


def _presence_corrections():
    from hearth.presence_corrections import PresenceCorrections
    pcfg = load_config().get("presence") or {}
    return PresenceCorrections(pcfg.get("ssh") or endpoints.ssh_target(config),
                               pcfg.get("admin_script", "/opt/cluster-bridge/wildlife_admin.py"),
                               (load_config().get("frigate") or {}).get("url", ""),
                               prepare_face=_crop_to_person)


# ---- Where the subject is in a sighting's picture (sighting_boxes.py): card boxes and face-training crops ----
_box_cache = None


_vision_lock = threading.Lock()   # one grounding call at a time: the cards ask together, the vision slot is single


def _vision_url() -> str:
    return endpoints.url("vision", config)


def _profile(name: str) -> Optional[Dict[str, Any]]:
    from hearth.frigate_presence import norm_name
    ents = (_courage_hub_presence() or {}).get("known_entities") or {}
    return next((e for g in ("people", "pets") for e in ents.get(g, []) if norm_name(e.get("name", "")) == norm_name(name)), None)


def _sentry_snapshot_bytes(fn: str) -> Optional[bytes]:
    """A wildlife-sentry snapshot from the engine host (name checked: letters, digits, _ . - only, .jpg)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.jpg", fn or "") or ".." in fn:
        return None
    if not endpoints.configured("coordinator", config):   # no engine host (fresh install): nothing to ssh to
        return None
    try:
        sub = subprocess.run(endpoints.ssh_cmd(config, f"cat /opt/cluster-bridge/wildlife/snapshots/{fn}"), capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:   # the engine host slow or unreachable: a clean "not found", not a dead request
        logger.debug(f"sentry snapshot {fn}: {e}")
        return None
    return sub.stdout if sub.returncode == 0 and sub.stdout else None


def _sighting_box(source: str, ref: str, name: str) -> Optional[List[float]]:
    """[x, y, w, h] (fractions) of `name` in a card's picture, or None. Cached per sighting."""
    global _box_cache
    from hearth import sighting_boxes as sb
    from hearth.frigate_presence import EVENT_ID, norm_name
    if _box_cache is None:
        _box_cache = sb.BoxCache(os.path.join(ROOT_DIR, "data", "sighting_boxes.json"))
    key = f"{source}|{ref}|{norm_name(name)}"
    if source == "patrol":
        entity = ref.rpartition("|")[0]
        key += f"|{(get_patrol().state.get(entity) or {}).get('last')}"   # frame indexes are reused by the next sweep
    known, box = _box_cache.get(key)
    if known:
        return box
    if source == "frigate":
        if not EVENT_ID.match(ref or ""):
            raise ValueError("bad event id")
        box = sb.frigate_box((load_config().get("frigate") or {}).get("url", ""), ref)
    else:
        if source == "sentry":
            jpeg = _sentry_snapshot_bytes(ref)
        elif source == "patrol":
            entity, _, idx = ref.rpartition("|")
            jpeg = get_patrol().frame(entity, int(idx)) if idx.isdigit() else None
        else:
            raise ValueError("unknown source")
        if not jpeg:
            return None
        with _vision_lock:
            known, box = _box_cache.get(key)          # another card may have asked for the same one meanwhile
            if known:
                return box
            box = sb.locate(jpeg, sb.describe(_profile(name), name), _vision_url())
    _box_cache.put(key, box)
    return box


def _crop_to_person(jpeg: bytes, name: str) -> bytes:
    """Face training: only the corrected person, when the vision model can find them in the frame."""
    from hearth import sighting_boxes as sb
    with _vision_lock:
        box = sb.locate(jpeg, sb.describe(_profile(name), name), _vision_url())
    if not box:
        return jpeg
    logger.info(f"face training for {name}: cropped to {box}")
    return sb.crop(jpeg, box)


def _invalidate_presence() -> None:
    """After a correction or new profile: the next presence read (cards, Courage) goes back to the source."""
    _courage_presence_cache["at"] = 0.0
    try:
        from harness.core.home_presence_hub import home_presence_hub
        home_presence_hub.invalidate()
    except Exception:
        pass


_go2rtc_names_cache: Dict[str, Any] = {"at": 0.0, "names": None}


def _go2rtc_stream_names(cfg: Dict[str, Any]) -> Optional[set]:
    """Stream names go2rtc has right now (None if unreachable: then the quality menu is not filtered).
    10 s cache: opening the LIVE tab lists the tiles and then validates one WebRTC/MP4 request per tile."""
    if time.time() - _go2rtc_names_cache["at"] < 10:
        return _go2rtc_names_cache["names"]
    try:
        from hearth.frigate_presence import go2rtc_stream_names
        names = go2rtc_stream_names((cfg.get("frigate") or {}).get("go2rtc_url", ""))
    except Exception as e:
        logger.warning(f"go2rtc unreachable: {e}")
        names = None
    _go2rtc_names_cache.update(at=time.time(), names=names)
    return names


def _webrtc_streams(cfg: Dict[str, Any]) -> Dict[str, str]:
    """HA camera entity -> go2rtc stream the LIVE tab may play: Frigate cameras (their sub stream) plus
    camera_ui entries that name a go2rtc stream directly. Only streams go2rtc actually has ({} if unreachable)."""
    fcfg = cfg.get("frigate") or {}
    cams = dict(fcfg.get("cameras") or {})
    cams.update({e: c["go2rtc"] for e, c in (cfg.get("camera_ui") or {}).items() if c.get("go2rtc")})
    names = _go2rtc_stream_names(cfg)
    if names is None:
        return {}
    from hearth.frigate_presence import live_cameras
    by_cam = {c["id"]: c["stream"] for c in live_cameras(fcfg.get("go2rtc_url", ""), list(cams.values()), names)}
    return {entity: by_cam[cam] for entity, cam in cams.items() if cam in by_cam}


def _courage_hub_presence() -> Dict[str, Any]:
    """Presence hub state. A fresh read takes ~1.4 s, so serve a cached copy and refresh it in the background."""
    age = time.time() - _courage_presence_cache["at"]
    if _courage_presence_cache["state"] is not None and age < 60:
        if age > 20 and not _courage_presence_cache["refreshing"]:
            _courage_presence_cache["refreshing"] = True
            threading.Thread(target=_courage_refresh_presence, daemon=True).start()
        return _courage_presence_cache["state"]
    _courage_refresh_presence()
    return _courage_presence_cache["state"] or {}


def _courage_hub_and_frigate() -> Dict[str, Any]:
    """Hub state with Frigate's live sightings merged in per identity (newest wins). Merged on every read,
    never into the hub cache: Frigate's view is seconds old, the hub's up to a minute."""
    state = _courage_hub_presence()
    fp = get_frigate_presence()
    if not fp:
        return state
    from hearth.frigate_presence import merge_locations
    return {**state, "locations": merge_locations(state.get("locations") or {}, fp.locations())}


_home_status_cache: Dict[str, Any] = {"at": 0.0, "data": {}}


def _ha_home_status() -> Dict[str, Dict[str, Any]]:
    """Residents' HA person state (phone GPS / Life360), 30 s cache: {"<name>": {state, source_label, since_min}}."""
    if time.time() - _home_status_cache["at"] < 30:
        return _home_status_cache["data"]
    out: Dict[str, Dict[str, Any]] = {}
    for name in (load_config().get("patrol") or {}).get("residents") or _household().people:
        st = hass.get_state(f"person.{name.lower()}")
        if not st:
            continue
        src = (st.get("attributes") or {}).get("source") or ""
        since = None
        try:
            changed = datetime.fromisoformat(st["last_changed"].replace("Z", "+00:00"))
            since = round((datetime.now(timezone.utc) - changed).total_seconds() / 60, 1)
        except Exception:
            pass
        out[name.lower()] = {"state": st.get("state"), "source": src, "since_min": since,
                             "source_label": "Life360" if "life360" in src else "phone GPS"}
    _home_status_cache.update(at=time.time(), data=out)
    return out


def _courage_presence() -> Dict[str, Any]:
    """Everything Courage and the cards know: hub + Frigate + PTZ patrol sightings (newest wins per identity), and
    the residents' GPS home status from HA (Life360 / phone)."""
    state = _courage_hub_and_frigate()
    try:
        state = {**state, "home_status": _ha_home_status()}
    except Exception as e:
        logger.warning(f"home status from HA: {e}")
    if _patrol is None:
        return state
    from hearth.frigate_presence import merge_locations
    return {**state, "locations": merge_locations(state.get("locations") or {}, _patrol.locations())}


def _courage_camera_look(cam_eid: str, cam_name: str, people_only: bool = False) -> Optional[str]:
    """Courage's camera looks. For a camera Frigate watches (config.json frigate.cameras: HA entity -> Frigate
    camera) the frame is Frigate's latest (~0.1 s, vs ~1.5 s for an HA snapshot) and Frigate's tracked objects
    are named first. The VLM is skipped only for people_only looks (presence_now) when Frigate sees nobody:
    a camera_look question may be about anything, and Frigate only tracks people, dogs and cats.
    Other cameras, or Frigate unreachable: the HA snapshot + VLM path as before."""
    fp = get_frigate_presence()
    fcam = ((load_config().get("frigate") or {}).get("cameras") or {}).get(cam_eid)
    lent = _on_loan("vision")
    if lent:   # engine lease: no vision model until then; Frigate's own objects are still live
        until = clock(datetime.fromtimestamp(lent))
        if fp and fcam:
            try:
                from hearth.frigate_presence import describe_in_view
                seen = describe_in_view(fp.in_view_now(fcam)) or "no people, dogs or cats"
                return f"Frigate, live on {cam_name}: {seen}. (Vision is lent out until {until}, so no closer look.)"
            except Exception as e:
                logger.warning(f"Frigate look on {cam_name} during a lease: {e}")
        return f"Vision is lent out until {until}, so I can't look through the {cam_name} camera until then."
    if fp and fcam:
        try:
            from hearth.frigate_presence import describe_in_view
            objects = fp.in_view_now(fcam)
            if not objects and people_only:
                return f"Frigate, live on {cam_name}: no people, dogs or cats in view right now."
            seen = describe_in_view(objects) or "no people, dogs or cats"
            desc = _analyze_single_frame(cam_eid, cam_name, img_bytes=fp.latest_frame(fcam))
            return f"Frigate, live on {cam_name}: {seen}." + (f" Vision: {desc}" if desc else "")
        except Exception as e:
            logger.warning(f"Frigate camera_look on {cam_name} failed, falling back to HA snapshot: {e}")
    return _analyze_single_frame(cam_eid, cam_name)


def _courage_notify(message: str, target: str = "") -> Dict[str, Any]:
    service = _phone_service(target)
    if not service:
        return {"ok": False, "error": f"no phone registered in Home Assistant for '{target}'"}
    return hass.call_service("notify", service, {"title": "Computer", "message": message})


def _courage_speak(message: str, room: str = "") -> Dict[str, Any]:
    from hearth.household import speaker_services
    speakers = speaker_services(config)
    service = speakers.get(room) if room else next(iter(speakers.values()), None)
    if not service:
        return {"ok": False, "error": f"no Echo for '{room}'"}
    return hass.call_service("notify", service, {"message": message, "data": {"type": "announce"}})


def _tts_cache_dir() -> str:
    return os.path.join(_stonesage_data_dir(), "tts_cache")


TTS_VOICE, TTS_SPEED = "bm_george", 1.06      # what the page's voice mode asks for (chat.js)


def _tts_warm() -> None:
    """Make the audio for every reflex confirmation ahead of time, so a spoken "Living Room TV Lights off." needs no synthesis.
    Runs once shortly after start (the voice server is shared with live turns) and is quiet when everything is cached.
    No voice server configured (a fresh install): nothing to warm, so nothing is called."""
    if not endpoints.configured("voice", config):
        logger.info("TTS cache: no voice server configured, nothing to pre-make")
        return
    try:
        from hearth import tts_cache
        from hearth import reflex
        from stonesage.courage.agent import CANNED_LINES
        from harness.connectors.voice_connector import voice_connector
        ents = reflex.candidates(lambda d: hass.get_states(d))
        lines = tts_cache.reflex_lines([(e.get("friendly_name") or e["entity_id"], e.get("state")) for e in ents], CANNED_LINES)
        t = time.time()
        made = tts_cache.warm(_tts_cache_dir(), lines, TTS_VOICE, TTS_SPEED,
                              lambda tx, v, sp: voice_connector.synthesize_speech(tx, voice=v, speed=sp))
        logger.info(f"TTS cache: {len(lines)} confirmations for {len(ents)} devices, {made} made in {time.time() - t:.0f}s")
    except Exception as e:
        logger.warning(f"TTS cache warm-up failed: {e}")


# ---------------------------------------------------------------------------------------------------------------------
# Hooks the platform calls (apps.py; registered by hearth/app.py install). Each answers one question the platform has
# about the house, so the platform never imports Home.
# ---------------------------------------------------------------------------------------------------------------------
try:
    import redis   # Valkey fallback for the last sightings (ground_context)
except ImportError:
    redis = None


def _unpack_entities(res: Any) -> List[Dict[str, Any]]:
    """Helper to safely extract entity lists from Home Assistant responses."""
    if isinstance(res, dict):
        return res.get("entities", [])
    elif isinstance(res, list):
        return res
    return []


def courage_home() -> Dict[str, Any]:
    """Computer's house, for the platform's agent builder (server.get_courage_agent): its home tools, the presence card,
    the on/off reflex and learned phrasings, and the phone that approves actions (HA mobile app) if one is set up."""
    from hearth.reflex import LearnedReflexes, resolve
    from hearth.persona import render_persona
    ha_cfg = config.get("homeassistant", {})
    phone = _phone_service()
    household = _household()
    return {
        "deps": dict(
            household=household,
            ha_states=lambda domain: hass.get_states(domain),
            ha_call=lambda domain, service, data: hass.call_service(domain, service, data),
            presence=_courage_presence,
            camera_look=_courage_camera_look,
            camera_scan=scan_camera_presets,
            correct_sighting=_courage_correct_sighting,
            notify=_courage_notify,
            speak=_courage_speak,
        ),
        "presence_fn": _courage_presence,
        "persona": render_persona(household),   # the soul's starting persona (soul.py keeps its own copy after)
        "reflex_resolve": resolve,
        "learned": LearnedReflexes(os.path.join(_stonesage_data_dir(), "courage_reflexes.json")),
        "phone_approvals": (ha_cfg.get("url", hass.base_url), ha_cfg["token"], phone) if ha_cfg.get("token") and phone else None,
    }


def notify_phone(title: str, message: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """A push to the household's phone (Home Assistant mobile app): lease check-ins, "Computer got stuck"."""
    service = _phone_service()
    if not service:
        return {"ok": False, "error": "no phone registered"}
    return hass.call_service("notify", service, {"title": title, "message": message, "data": data})


def privacy_terms() -> set:
    """Words that make text home content for Boost's egress rule: household, pet and camera names, every recognition
    profile, and everyone in the Life360 circle (full, first and last names)."""
    hh = _household()
    terms = set(hh.keys) | {c["name"] for c in hh.cameras.values()}
    terms |= {k.replace("_", " ") for k in hh.cameras}
    try:
        ents = _courage_hub_presence().get("known_entities") or {}
        terms |= {e["name"] for g in ("people", "pets") for e in ents.get(g, []) if e.get("name")}
    except Exception as e:
        logger.warning(f"Boost home terms: profiles: {e}")
    for t in (hass.get_states("device_tracker").get("entities") or []):
        if t["entity_id"].startswith("device_tracker.life360_"):
            full = re.sub(r"^life360\s+", "", t.get("friendly_name") or "", flags=re.I).strip()
            if full:
                terms.add(full)
                terms.update(w for w in full.split() if len(w) >= 3)
    return terms


def gpu_busy(roles: List[str]) -> Optional[str]:
    """Why an engine a lease would take is in use by the house right now (a patrol sweep on the vision model), or None."""
    if "vision" in roles and _patrol is not None:
        busy = [c["name"] for c in (_patrol.status().get("cameras") or {}).values() if c.get("busy")]
        if busy:
            return f"patrol sweeping {', '.join(busy)}"
    return None


def household() -> Dict[str, Any]:
    """Is anyone awake who might want the house brain? (the idle gate's question; commentary.Commentary.household)"""
    return get_commentary().household()


def ground_context(agent_id: str, user_query: str) -> List[str]:
    """Home grounding for agents that use keyword grounding instead of tools (server.ground_hardware_context): profiles,
    presence, live camera looks and scans, the wildlife ledger, Home Assistant devices. Text blocks for the prompt."""
    q_lower = (user_query or "").lower()
    injections: List[str] = []
    # 0. Verified Human & Pet Profiles Grounding
    entity_keywords = tuple(_household().keys) + ("pet", "pets", "cat", "dog", "who", "family", "resident", "animals", "animal", "creature", "where", "what's", "whats", "see", "seen")
    if agent_id in ("home-agent", "wildlife-agent", "companion", "courage-computer", "courage") or any(k in q_lower for k in entity_keywords):
        try:
            from harness.core.entity_profiles import entity_profiles
            summary = entity_profiles.get_grounded_entity_summary()   # "" when no profiles are kept
            if summary:
                injections.append(summary)
        except Exception as e:
            logger.debug(f"Entity profile grounding deferred: {e}")

    # 1. Vision, Camera & Wildlife Grounding
    vision_keywords = ("wildlife", "animal", "deer", "fox", "rabbit", "perimeter", "faunasentinel", 
                       "camera", "cameras", "cam", "see", "outside", "look", "watching", "front door", 
                       "driveway", "back yard", "side yard", "living room", "kitchen", "where is", 
                       "where's", "did you see", "have you seen", "who is", "who\'s", *_household().keys,
                       "visitor", "stranger", "anyone", "realtime", "real time", 
                       "right now", "take a look", "check")
    needs_vision = (agent_id in ("radagast", "sentinel", "faunasentinel", "courage-computer", "courage")) or any(k in q_lower for k in vision_keywords)

    if needs_vision:
        # A. Query Real-Time Human, Pet & Perimeter Presence from Home Assistant Sensors & Valkey A-MEM
        try:
            presence_lines = []
            p_state = hass.get_state("sensor.last_person_sighting")
            if p_state and p_state.get("state") not in (None, "unavailable", "unknown"):
                attrs = p_state.get("attributes", {})
                p_name = attrs.get("person", p_state.get("state"))
                p_cam = attrs.get("camera", "camera")
                p_clothes = attrs.get("clothing", "")
                p_time = attrs.get("timestamp", "")
                p_role = attrs.get("role", "Resident")
                presence_lines.append(f"- **Last Person Sighted**: {p_name} ({p_role}) on `{p_cam}`. Clothing: {p_clothes}. Sighted: {p_time}")

            pet_state = hass.get_state("sensor.last_pet_sighting")
            if pet_state and pet_state.get("state") not in (None, "unavailable", "unknown"):
                p_attrs = pet_state.get("attributes", {})
                pet_name = p_attrs.get("name", pet_state.get("state"))
                pet_cam = p_attrs.get("camera", "camera")
                pet_breed = p_attrs.get("breed") or p_attrs.get("species", "")
                pet_time = p_attrs.get("timestamp", "")
                presence_lines.append(f"- **Last Pet Sighted**: {pet_name} ({pet_breed}) on `{pet_cam}`. Sighted: {pet_time}")

            subj_state = hass.get_state("sensor.last_subject_detected")
            if subj_state and subj_state.get("state") not in (None, "unavailable", "unknown"):
                s_attrs = subj_state.get("attributes", {})
                s_details = s_attrs.get("details", "")
                if s_details:
                    presence_lines.append(f"- **Most Recent Sentry Description**: \"{s_details}\"")

            # Fallback to Valkey A-MEM if HA sensors were empty
            if not presence_lines and redis is not None:
                try:
                    valkey_host = endpoints.host("valkey", config)
                    r_valkey = redis.Redis(host=valkey_host, port=6379, decode_responses=True, socket_timeout=1.0)
                    last_p_json = r_valkey.get("amem:sighting:last_person")
                    if last_p_json:
                        p_data = json.loads(last_p_json)
                        presence_lines.append(f"- **Last Person Sighted (A-MEM)**: {p_data.get('name')} on `{p_data.get('camera')}`. Clothing: {p_data.get('clothing')}. Time: {p_data.get('timestamp')}")
                    last_pet_json = r_valkey.get("amem:sighting:last_pet")
                    if last_pet_json:
                        pet_data = json.loads(last_pet_json)
                        presence_lines.append(f"- **Last Pet Sighted (A-MEM)**: {pet_data.get('name')} ({pet_data.get('breed') or pet_data.get('species')}) on `{pet_data.get('camera')}`. Time: {pet_data.get('timestamp')}")
                except Exception:
                    pass

            if presence_lines:
                injections.append(
                    "### [REAL-TIME RESIDENT, PET & VISITOR PRESENCE (FAUNASENTINEL HARDWARE SENSORS)]:\n" +
                    "\n".join(presence_lines)
                )
        except Exception as e:
            logger.debug(f"Failed to fetch presence states: {e}")

        # B. Real-Time Live Optical Perception (On-Demand Snapshot Analysis)
        camera_targets = (
            "living room", "kitchen", "driveway", "front door", "side yard", 
            "back yard", "backyard", "porch", "yard", "outside", "perimeter", 
            "camera", "cameras", "cam", "couch", "sofa", "room"
        )
        perception_verbs = (
            "check", "look", "see", "view", "watch", "what's in", "whats in", 
            "what is in", "what's on", "whats on", "what is on", "what does", 
            "who is", "who's", "is anyone", "snapshot", "photo", "picture", 
            "feed", "show me", "status", "again", "right now", "realtime", 
            "real time", "peek", "happening"
        )
        scan_keywords = (
            "pan", "scan", "sweep", "search the room", "search around",
            "look around", "all angles", "every angle", "search for",
            "scan the", "sweep the", "pan the", "pan around",
            "can you find", "find me", "where am i", "where are you"
        )
        has_target = any(t in q_lower for t in camera_targets)
        has_verb = any(v in q_lower for v in perception_verbs)
        is_scan_request = any(s in q_lower for s in scan_keywords)
        explicit_realtime = any(r in q_lower for r in (
            "right now", "real time", "realtime", "take a look", "take a peek", 
            "look outside", "see outside", "what do you see", "check again", 
            *(f"where is {k}" for k in _household().keys if k in _pet_keys()), "look at the front", "look at the back"
        ))

        if is_scan_request:
            # PTZ multi-angle room scan
            scan_perception = scan_room_with_camera(user_query)
            if scan_perception:
                injections.append(scan_perception)
        elif (has_target and has_verb) or explicit_realtime or ("camera" in q_lower and ("see" in q_lower or "look" in q_lower)):
            # Single-frame perception
            live_perception = capture_live_camera_perception(user_query)
            if live_perception:
                injections.append(live_perception)

        # C. Query Wildlife Activity Ledger from the Cluster Bridge
        try:
            mcp_url = endpoints.url("mcp", config)
            req = urllib.request.Request(
                f"{mcp_url}/messages",
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_wildlife_log", "arguments": {"limit_lines": 35}}
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                texts = [c.get("text", "") for c in data.get("result", {}).get("content", []) if c.get("type") == "text"]
                wildlife_text = "\n".join(texts).strip()
                has_actual_sightings = (
                    wildlife_text and
                    "Error" not in wildlife_text and
                    len(wildlife_text.replace("# Wildlife Activity Log", "").strip()) > 10
                )
                if has_actual_sightings:
                    injections.append(f"### [REAL-TIME HARDWARE SENSORY DATA: WILDLIFE & PERIMETER SENTRY LEDGER]\n"
                                      f"The 24/7 wildlife sentry daemon actively monitors the physical cameras ({_camera_names()}).\n"
                                      f"Recent live animal, pet, and resident sightings from the master ledger:\n{wildlife_text}")
                else:
                    injections.append(
                        "### [REAL-TIME HARDWARE SENSORY DATA: WILDLIFE & PERIMETER SENTRY LEDGER]\n"
                        f"The 24/7 wildlife sentry daemon actively monitors the physical cameras ({_camera_names()}).\n"
                        "LEDGER STATUS: No new sightings or movement events were detected or logged in this immediate window."
                    )
        except Exception as e:
            logger.debug(f"Failed to fetch live wildlife log: {e}")

        # D. Query active camera entity states from Home Assistant
        try:
            cam_states = _unpack_entities(hass.get_states(domain_filter="camera"))
            if cam_states:
                cam_lines = []
                for c in cam_states:
                    eid = c.get("entity_id", "")
                    if "hd" in eid or "direct" in eid or "stream" in eid:
                        fn = c.get("attributes", {}).get("friendly_name", eid)
                        st = c.get("state", "idle")
                        status_label = "ONLINE (Armed & Standing By)" if st in ("idle", "recording", "streaming") else st.upper()
                        cam_lines.append(f"- {fn} (`{eid}`): {status_label}")
                if cam_lines:
                    injections.append(
                        "### [PHYSICAL TAPO CAMERAS CONNECTED VIA HOME ASSISTANT]:\n" + "\n".join(cam_lines[:6]) + "\n"
                        "CAMERA GROUNDING INVARIANT: In Home Assistant, an 'idle' camera state means the camera is 100% ONLINE, operational, and standing by. "
                        "NEVER report a camera as offline, broken, or interrupted when it is standing by."
                    )
        except Exception as e:
            logger.debug(f"Failed to fetch camera states: {e}")

    # 2. Smart Home, Climate & Device Grounding
    ha_keywords = ("thermostat", "temperature", "temp", "hvac", "climate", "heat", "cool", "lights", "light", 
                   "plug", "switch", "home assistant", "haos", "dryer", "appliances", "power", "nest", "device", "devices",
                   "house", "home", "status", "overview", "everything", "what's", "whats", "telemetry", "condition")
    needs_ha = (agent_id in ("varda", "hearth", "climate", "home_assistant")) or any(k in q_lower for k in ha_keywords)

    if needs_ha:
        try:
            is_entity_discovery = any(k in q_lower for k in [
                "what entities", "which entities", "list entities", "all entities", "see entities", 
                "show entities", "poll entities", "entities in home assistant", "entities in ha", 
                "what devices", "all devices", "list devices", "how many entities", "what can you see",
                "real life poll", "real poll", "why so limited", "inspect entities"
            ])

            if is_entity_discovery:
                # Perform a direct real-time live poll across all 1500+ entities in Home Assistant OS
                all_res = hass.get_states()
                all_ents = all_res.get("entities", []) if isinstance(all_res, dict) else (all_res if isinstance(all_res, list) else [])
                if all_ents:
                    total_count = len(all_ents)
                    domain_counts = {}
                    for e in all_ents:
                        d = e.get("entity_id", "").split(".")[0]
                        domain_counts[d] = domain_counts.get(d, 0) + 1
                    
                    dom_summary_str = ", ".join(f"{d}: {c}" for d, c in sorted(domain_counts.items(), key=lambda x: -x[1])[:10])
                    lines = [
                        f"- Total Verified Entities in Home Assistant: {total_count}",
                        f"- Active Domain Breakdown: {dom_summary_str}",
                    ]

                    # Domain-specific breakdown if mentioned
                    target_dom = None
                    for d in ("switch", "light", "climate", "camera", "sensor", "binary_sensor", "media_player", "automation", "valve", "button"):
                        if d in q_lower:
                            target_dom = d
                            break

                    if target_dom:
                        dom_matches = [e for e in all_ents if e.get("entity_id", "").startswith(f"{target_dom}.")]
                        lines.append(f"- All '{target_dom}' Entities ({len(dom_matches)} total):")
                        for dm in dom_matches[:20]:
                            eid = dm.get("entity_id")
                            fn = dm.get("friendly_name") or dm.get("attributes", {}).get("friendly_name", eid)
                            st = dm.get("state")
                            lines.append(f"  * `{eid}` ('{fn}'): state={st}")
                    else:
                        # Broad summary across key interactive devices
                        interactive_samples = []
                        for dm in all_ents:
                            d = dm.get("entity_id", "").split(".")[0]
                            if d in ("climate", "light", "switch", "camera", "media_player", "person", "valve"):
                                eid = dm.get("entity_id")
                                fn = dm.get("friendly_name") or dm.get("attributes", {}).get("friendly_name", eid)
                                st = dm.get("state")
                                interactive_samples.append(f"  * `{eid}` ('{fn}'): state={st}")
                        lines.append(f"- Key Interactive Devices Sample ({len(interactive_samples)} total interactive entities):")
                        lines.extend(interactive_samples[:25])

                    injections.append("### [REAL-TIME LIVE HOME ASSISTANT AUDIT (1,500+ TOTAL DEVICES REGISTERED)]:\n" + "\n".join(lines))
                    injections.append(
                        "### [REAL-TIME POLLING INVARIANTS]:\n"
                        f"- You have completed an actual live poll of Home Assistant OS at {endpoints.url('homeassistant', config)}.\n"
                        f"- You can see all {total_count} entities registered across the network.\n"
                        "- Report this exact live poll data to the user. Inform them of the true scope of your Home Assistant visibility."
                    )
            else:
                climate_states = _unpack_entities(hass.get_states(domain_filter="climate"))
                switch_states = _unpack_entities(hass.get_states(domain_filter="switch"))
                light_states = _unpack_entities(hass.get_states(domain_filter="light"))
                tracker_states = _unpack_entities(hass.get_states(domain_filter="device_tracker"))
                
                lines = []
                for c in climate_states:
                    attrs = c.get("attributes", {})
                    name = attrs.get("friendly_name") or c.get("entity_id", "Thermostat")
                    mode = c.get("state", "unknown")
                    raw_target = attrs.get("temperature", "unknown")
                    raw_curr = attrs.get("current_temperature", "unknown")
                    try:
                        t_clean = f"{int(round(float(raw_target)))}"
                    except Exception:
                        t_clean = str(raw_target)
                    try:
                        c_clean = f"{int(round(float(raw_curr)))}"
                    except Exception:
                        c_clean = str(raw_curr)
                    lines.append(f"- Thermostat '{name}': Mode={mode}, Target Temperature={t_clean} degrees Fahrenheit, Current Temperature={c_clean} degrees Fahrenheit")
                
                active_switches = [s.get("attributes", {}).get("friendly_name", s.get("entity_id")) for s in switch_states if s.get("state") == "on"]
                if active_switches:
                    lines.append(f"- Active Switches/Plugs (ON): {', '.join(active_switches[:8])}")
                
                active_lights = [l.get("attributes", {}).get("friendly_name", l.get("entity_id")) for l in light_states if l.get("state") == "on"]
                if active_lights:
                    lines.append(f"- Active Lights (ON): {', '.join(active_lights[:8])}")
                else:
                    lines.append("- Active Lights: All lights currently OFF")

                for t in tracker_states:
                    e_id = t.get("entity_id", "").lower()
                    if "phone" in e_id or any(p in e_id for p in _household().phones):
                        name = t.get("attributes", {}).get("friendly_name", t.get("entity_id"))
                        lines.append(f"- Presence '{name}': {t.get('state')}")

                if lines:
                    injections.append("### [AMBIENT HOME ASSISTANT TELEMETRY SNAPSHOT (1,500+ total devices registered)]:\n" + "\n".join(lines))
                
                injections.append(
                    "### [SMART HOME GROUNDING & LIVE POLLING INVARIANTS]:\n"
                    "- The list above is ONLY a quick ambient snapshot of active items (thermostat, active switches).\n"
                    "- Home Assistant manages 1,500+ live entities across sensors, switches, lights, cameras, automations, and climate.\n"
                    f"- When the user asks about devices or what is happening, answer {_household().owner} directly, conversationally, and factually based on the live data above. DO NOT emit mock tool calls, code stubs, or 'Calling: ...' lines."
                )
        except Exception as e:
            logger.debug(f"Failed to fetch HA states: {e}")
    return injections


def start() -> None:
    """Home's background services (registered with the platform's on_start by hearth/app.py)."""
    # Warm Computer's presence cache so the first question doesn't pay the ~1.4 s presence read
    threading.Thread(target=_courage_refresh_presence, daemon=True, name="courage-presence-warmup").start()
    threading.Timer(45.0, _tts_warm).start()  # a little after start: the voice server is shared with live turns
    if get_frigate_presence():
        threading.Thread(target=_frigate_presence.start, daemon=True, name="frigate-presence-start").start()
    if (config.get("patrol") or {}).get("enabled"):
        get_patrol().start()  # PTZ sweeps (hearth/patrol.py); checks every minute which camera is due
    if (config.get("ha_presence") or {}).get("enabled"):
        get_ha_presence().start()  # who-was-seen-where sensors in HA (hearth/ha_presence.py)
    if get_adguard_blocks():
        get_adguard_blocks().start()  # dashboard 'Block TikTok' toggle -> AdGuard blocked service (hearth/adguard_blocks.py)
    if get_alexa_relay():
        get_alexa_relay().start()  # Echo requests via the Alexa skill and a relay, outbound only (hearth/alexa_relay.py)
    if config.get("commentary"):
        get_commentary().start()  # unprompted remarks (hearth/commentary.py); enabled/mode are read every tick

def chat_action(user_query: str):
    """A house order in plain words from an agent without tools (the platform's chat route): if it is a whitelisted
    Home Assistant action, check it, run it, and return (note for the system prompt, memory badge or None); else None."""
    badge = None
    action_plan = extract_whitelisted_chat_action(user_query)
    if action_plan and action_plan.get("category") == "home_assistant":
        dom = action_plan.get("domain")
        srv = action_plan.get("service")
        s_data = action_plan.get("service_data", {})

        is_valid, val_msg, sanitized_data = validate_ha_action(dom, srv, s_data)
        if is_valid:
            logger.info(f"Executing whitelisted HA action from chat: {dom}.{srv} -> {sanitized_data}")
            ha_res = hass.call_service(dom, srv, sanitized_data)
            desc = action_plan.get("description", f"{dom}.{srv}")
            if ha_res.get("ok", True):
                action_note = (
                    f"### [VERIFIED HARDWARE ACTION EXECUTION RECORD]:\n"
                    f"- Action: {desc}\n"
                    f"- Service: `{dom}.{srv}`\n"
                    f"- Parameters: `{json.dumps(sanitized_data)}`\n"
                    f"- Status: SUCCESS (Directly executed on Home Assistant)\n"
                    f"INSTRUCTION: {_household().owner}'s command has already been executed on the physical device. "
                    f"Confirm the action was completed factually without roleplay or hesitation."
                )
                badge = f"Executed: {desc}"
            else:
                action_note = (
                    f"### [HARDWARE ACTION EXECUTION ATTEMPT FAILED]:\n"
                    f"- Action: {desc}\n"
                    f"- Error: {ha_res.get('error', 'Service call failed')}\n"
                    f"INSTRUCTION: Inform {_household().owner} that executing this action failed on Home Assistant."
                )
            return action_note, badge
    return None


def config_saved(cfg: Dict[str, Any]) -> None:
    """Settings were saved: refresh Home's clients (the token in place; Immich is rebuilt)."""
    global immich
    hass.set_token(cfg.get("homeassistant", {}).get("token", ""))
    hass.base_url = endpoints.url("homeassistant", cfg)      # the setup wizard can change the address too
    immich = ImmichClient(cfg.get("immich", {}), os.path.join(ROOT_DIR, "uploads"))


def bind(app_ctx) -> None:
    """Give this module what it needs from the platform (called once by hearth/app.py install)."""
    global ROOT_DIR
    ROOT_DIR = app_ctx.root_dir
    global _stonesage_data_dir
    _stonesage_data_dir = app_ctx.data_dir
    global config
    config = app_ctx.config
    global LOCAL_TZ
    LOCAL_TZ = app_ctx.services["LOCAL_TZ"]
    global _engine_lease
    _engine_lease = app_ctx.services["_engine_lease"]
    global _on_loan
    _on_loan = app_ctx.services["_on_loan"]
    global get_courage_agent
    get_courage_agent = app_ctx.services["get_courage_agent"]
    global get_trace_log
    get_trace_log = app_ctx.services["get_trace_log"]
    global load_config
    load_config = app_ctx.services["load_config"]
    # Home's own clients (the platform no longer creates them)
    global hass, immich
    hass = HomeAssistantClient(endpoints.url("homeassistant", config),
                               config.get("homeassistant", {}).get("token", ""))
    immich = ImmichClient(config.get("immich", {}), os.path.join(ROOT_DIR, "uploads"))
    from hearth import ha_policy      # the order parser finds the thermostat / lights among Home Assistant's own entities
    ha_policy.bind(lambda domain: [e["entity_id"] for e in (hass.get_states(domain_filter=domain).get("entities") or [])])
