"""The Home app (docs/LAYERS.md, layer 2): what this package brings to the StoneSage platform, registered through the
app context (apps.py) instead of being imported by the platform. Loaded when config.json "apps" lists "hearth" (or its old name "home").

Routes: moved here from server.py's do_GET/do_POST by the layer split (2026-10-09), bodies unchanged except `self` -> `h`
(the request handler). Each answers exactly as before: tests/test_routes_home.py pins every one through the real handler.
Names the server still lends (HOME_LENT in server.py) are bound into this module by install(); that list only shrinks.
"""

from datetime import datetime, timezone
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import logging

from lodestone import endpoints

logger = logging.getLogger("StoneSage.home")
PLATFORM_SERVICES = ("cluster",)   # platform API this app uses (ctx.services), bound by install()
ctx = None          # apps.AppContext, set by install()
cluster = None
_apply_presence_correction = None
_courage_presence = None
_go2rtc_stream_names = None
_invalidate_presence = None
_presence_corrections = None
_sentry_snapshot_bytes = None
_sighting_box = None
_stonesage_data_dir = None
_tts_cache_dir = None
_webrtc_streams = None
extract_whitelisted_chat_action = None
get_adguard_blocks = None
get_alexa_relay = None
get_commentary = None
get_frigate_presence = None
get_ha_presence = None
get_patrol = None
hass = None
immich = None
load_config = None
save_config = None
validate_ha_action = None


def _get_api_profiles(h, parsed, body):
    """GET /api/profiles: the household's people and pets (data/profiles/entity_profiles.json, private) and today's
    calendar events from Home Assistant."""
    try:
        from harness.core.entity_profiles import entity_profiles
        cfg = load_config()
        cal_data = entity_profiles.poll_calendar_events(
            hass_url=endpoints.url("homeassistant", cfg),
            token=(cfg.get("hass") or cfg.get("homeassistant") or {}).get("token", "")
        )
        h.send_json({"ok": True, "profiles": entity_profiles.profiles, "calendar_events": cal_data})
    except Exception as e:
        h.send_json({"ok": False, "error": str(e)})


def _get_api_frigate_presence(h, parsed, body):
    """GET /api/frigate/presence"""
    fp = get_frigate_presence()
    h.send_json({"ok": bool(fp), **(fp.status() if fp else {"error": "config.json has no frigate.url"})})
    return


def _get_api_patrol_status(h, parsed, body):
    """GET /api/patrol/status"""
    h.send_json({"ok": True, **get_patrol().status()})
    return


def _get_api_ha_presence_status(h, parsed, body):
    """GET /api/ha-presence/status"""
    h.send_json({"ok": True, **get_ha_presence().status()})
    return


def _get_api_adguard_blocks_status(h, parsed, body):
    """GET /api/adguard-blocks/status"""
    blocks = get_adguard_blocks()
    h.send_json({"ok": True, **(blocks.status() if blocks else {"enabled": False})})
    return


def _get_api_alexa_relay_status(h, parsed, body):
    """GET /api/alexa-relay/status"""
    relay = get_alexa_relay()
    h.send_json({"ok": True, **(relay.status() if relay else {"enabled": False})})
    return


def _get_api_commentary_status(h, parsed, body):
    """GET /api/commentary/status"""
    h.send_json({"ok": True, **get_commentary().status()})
    return


def _get_api_patrol_frame(h, parsed, body):
    """GET /api/patrol/frame"""
    q = urllib.parse.parse_qs(parsed.query or "")
    try:
        img = get_patrol().frame(q.get("entity", [""])[0], int(q.get("i", ["-1"])[0]))
    except ValueError:
        img = None
    if not img:
        h.send_json({"ok": False, "error": "no such patrol frame"}, 404)
        return
    h.send_response(200)
    h.send_header("Content-Type", "image/jpeg")
    h.send_header("Content-Length", str(len(img)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(img)
    return


def _get_api_presence_profiles(h, parsed, body):
    """GET /api/presence/profiles"""
    try:
        h.send_json(_presence_corrections().profiles())
    except Exception as e:
        h.send_json({"ok": False, "error": str(e)}, 502)
    return


def _get_api_presence_status(h, parsed, body):
    """GET /api/presence/status"""
    try:
        h.send_json({"ok": True, "data": _courage_presence()})  # hub + Frigate, same view Courage has
    except Exception as e:
        h.send_json({"ok": False, "error": str(e)}, 500)
    return


def _get_api_cameras_live(h, parsed, body):
    """GET /api/cameras/live"""
    # Tiles for the LIVE tab (camera_ui.py): WebRTC/MP4 via go2rtc, or snapshot; PTZ presets
    from hearth import camera_ui
    cfg = load_config()
    h.send_json({"ok": True, "cameras": camera_ui.list_cameras(cfg, hass.get_state, _webrtc_streams(cfg),
                                                                   _go2rtc_stream_names(cfg))})
    return


def _get_api_cameras_snapshot(h, parsed, body):
    """GET /api/cameras/snapshot"""
    from hearth import camera_ui
    q = urllib.parse.parse_qs(parsed.query or "")
    entity, force = q.get("entity", [""])[0], q.get("force", ["0"])[0] == "1"
    try:
        img, age = camera_ui.snapshot(entity, load_config(), hass.get_camera_snapshot, force=force)
    except KeyError:
        h.send_json({"ok": False, "error": "unknown camera"}, 404)
        return
    if not img:
        h.send_json({"ok": False, "error": "camera did not answer"}, 504)
        return
    h.send_response(200)
    h.send_header("Content-Type", "image/jpeg")
    h.send_header("Content-Length", str(len(img)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Snapshot-Age", str(int(age)))
    h.end_headers()
    h.wfile.write(img)
    return


def _get_api_cameras_mp4(h, parsed, body):
    """GET /api/cameras/mp4"""
    # Fallback when a browser cannot do WebRTC to the go2rtc host (Firefox on Android 16 without the
    # local-network permission sends no ICE checks at all): go2rtc's fragmented MP4, H.264 copied (no
    # transcoding, video only), relayed over this same HTTP(S) connection. One thread per viewer.
    # audio=1: go2rtc's mp4=flac turns the cameras' A-law audio into FLAC, which browsers play in MP4.
    from hearth import camera_ui
    q = urllib.parse.parse_qs(parsed.query or "")
    stream, audio = q.get("stream", [""])[0], q.get("audio", ["0"])[0] == "1"
    cfg = load_config()
    allowed = set(_webrtc_streams(cfg).values()) | {s for v in camera_ui.variant_streams(cfg).values() for s in v.values()}
    if stream not in allowed:
        h.send_json({"ok": False, "error": f"unknown stream '{stream}'"}, 400)
        return
    go2rtc = (cfg.get("frigate") or {}).get("go2rtc_url", "").rstrip("/")
    try:
        upstream = urllib.request.urlopen(f"{go2rtc}/api/stream.mp4?src={urllib.parse.quote(stream)}"
                                          + ("&mp4=flac" if audio else ""), timeout=15)
    except Exception as e:
        h.send_json({"ok": False, "error": f"go2rtc: {e}"}, 502)
        return
    try:
        h.send_response(200)
        h.send_header("Content-Type", "video/mp4")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "close")
        h.end_headers()
        while True:
            chunk = upstream.read(64 * 1024)
            if not chunk:
                break
            h.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
        pass  # viewer closed the tile or left the page
    finally:
        upstream.close()
        h.close_connection = True
    return


def _get_api_frigate_snapshot(h, parsed, body):
    """GET /api/frigate/snapshot/*"""
    path = parsed.path
    # Frigate event snapshot through StoneSage (same origin, so HTTPS pages can show it)
    from hearth.frigate_presence import EVENT_ID
    eid = path[len("/api/frigate/snapshot/"):]
    fcfg = load_config().get("frigate") or {}
    if not EVENT_ID.match(eid) or not fcfg.get("url"):
        h.send_json({"ok": False, "error": "bad event id"}, 400)
        return
    try:
        with urllib.request.urlopen(f"{fcfg['url'].rstrip('/')}/api/events/{eid}/snapshot.jpg?h=270", timeout=5) as r:
            img = r.read()
        h.send_response(200)
        h.send_header("Content-Type", "image/jpeg")
        h.send_header("Content-Length", str(len(img)))
        h.send_header("Cache-Control", "public, max-age=3600")
        h.end_headers()
        h.wfile.write(img)
    except Exception as e:
        h.send_json({"ok": False, "error": f"snapshot unavailable: {e}"}, 404)
    return


def _get_api_presence_snapshot(h, parsed, body):
    """GET /api/presence/snapshot/*"""
    path = parsed.path
    img = _sentry_snapshot_bytes(path[len("/api/presence/snapshot/"):].strip())
    if img:
        h.send_response(200)
        h.send_header("Content-Type", "image/jpeg")
        h.send_header("Content-Length", str(len(img)))
        h.send_header("Cache-Control", "public, max-age=3600")
        h.end_headers()
        h.wfile.write(img)
        return
    h.send_json({"ok": False, "error": "Snapshot not found"}, 404)
    return


def _get_api_presence_box(h, parsed, body):
    """GET /api/presence/box"""
    # Where a card's subject is in its picture, for the 2px box (sighting_boxes.py)
    q = urllib.parse.parse_qs(parsed.query or "")
    try:
        box = _sighting_box(q.get("source", [""])[0], q.get("ref", [""])[0], q.get("name", [""])[0])
        h.send_json({"ok": True, "box": box})
    except ValueError as e:
        h.send_json({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        h.send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
    return


def _get_api_ha_status(h, parsed, body):
    """GET /api/ha/status"""
    status = hass.ping()
    if isinstance(status, dict):
        status["url"] = getattr(hass, "base_url", None)
    h.send_json(status)
    return


def _get_api_ha_dashboard(h, parsed, body):
    """GET /api/ha/dashboard"""
    dashboard = hass.get_dashboard_summary()
    if isinstance(dashboard, dict):
        cfg = load_config()
        dashboard["customizations"] = cfg.get("ha_entity_customizations", {})
    h.send_json(dashboard)
    return


def _get_api_ha_entities(h, parsed, body):
    """GET /api/ha/entities"""
    domain = urllib.parse.parse_qs(parsed.query).get("domain", [None])[0]
    states = hass.get_states(domain)
    h.send_json(states)
    return


def _get_api_immich_stats(h, parsed, body):
    """GET /api/immich/stats"""
    h.send_json(immich.get_stats())
    return


def _get_api_immich_assets(h, parsed, body):
    """GET /api/immich/assets"""
    cat = urllib.parse.parse_qs(parsed.query).get("category", ["all"])[0]
    assets = immich.get_assets(cat)
    h.send_json({"ok": True, "assets": assets})
    return


def _get_api_immich_people(h, parsed, body):
    """GET /api/immich/people"""
    people = immich.get_people()
    h.send_json({"ok": True, "people": people})
    return


def _get_api_voice_cache(h, parsed, body):
    """GET /api/voice/cache"""
    from hearth import tts_cache
    d = _tts_cache_dir()
    files = [f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.endswith(".wav")]
    h.send_json({"ok": True, "clips": len(files), **tts_cache.stats()})
    return


def _get_api_voice_bench_phrases(h, parsed, body):
    """GET /api/voice/bench/phrases"""
    from hearth import voice_bench
    done = {r["phrase"]["id"] for r in voice_bench.load_records(_stonesage_data_dir())}
    h.send_json({"ok": True, "phrases": [dict(p, done=p["id"] in done) for p in voice_bench.phrases(load_config())]})
    return


def _get_api_voice_bench_results(h, parsed, body):
    """GET /api/voice/bench/results"""
    from hearth import voice_bench
    h.send_json({"ok": True, **voice_bench.summary(_stonesage_data_dir()),
                    "records": [{"phrase": r["phrase"]["text"], "cond": r["phrase"]["cond"],
                                 "results": {x["engine"]: x.get("text", x.get("error")) for x in r["results"]}}
                                for r in voice_bench.load_records(_stonesage_data_dir())]})
    return


def _curated_cameras():
    """The cameras config.json camera_ui lists, as cards: name, snapshot / stream URLs and (PTZ cameras) the presets the
    camera reports now."""
    from hearth import camera_ui
    cfg = load_config()
    cams = []
    for entity, ui in camera_ui.camera_entries(cfg).items():
        card = {"entity_id": entity, "name": ui.get("name", entity), "state": "idle",
                "snapshot_url": f"/api/hass/camera_snapshot?entity_id={entity}",
                "stream_url": f"/api/hass/camera_stream?entity_id={entity}"}
        if ui.get("ptz"):
            try:
                card["presets"] = camera_ui.presets(ui["ptz"], hass.get_state)
            except Exception:
                card["presets"] = list(ui.get("presets") or [])
        cams.append(card)
    return cams


def _get_api_hass_cameras(h, parsed, body):
    """GET /api/hass/cameras"""
    h.send_json({"ok": True, "cameras": _curated_cameras()})
    return


def _get_api_hass_camera_snapshot(h, parsed, body):
    """GET /api/hass/camera_snapshot"""
    cameras = list((load_config().get("camera_ui") or {}))
    entity_id = urllib.parse.parse_qs(parsed.query).get("entity_id", [cameras[0] if cameras else ""])[0]
    if not entity_id.startswith("camera."):
        h.send_json({"ok": False, "error": "entity_id=camera.<name> is required (no camera is configured in camera_ui)"}, 400)
        return
    ha_url = f"{hass.base_url}/api/camera_proxy/{entity_id}"
    req = urllib.request.Request(ha_url, headers=hass._get_headers())
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = resp.read()
            h.send_response(200)
            h.send_header("Content-Type", "image/jpeg")
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            h.end_headers()
            h.wfile.write(data)
            return
    except Exception as e:
        h.send_json({"ok": False, "error": str(e)}, 502)
        return


def _get_api_hass_camera_stream(h, parsed, body):
    """GET /api/hass/camera_stream"""
    entity_id = urllib.parse.parse_qs(parsed.query).get("entity_id", [""])[0]
    if not entity_id.startswith("camera."):
        h.send_json({"ok": False, "error": "entity_id=camera.<name> is required"}, 400)
        return
    ha_url = f"{hass.base_url}/api/camera_proxy_stream/{entity_id}"
    req = urllib.request.Request(ha_url, headers=hass._get_headers())
    streaming = False
    try:
        resp = urllib.request.urlopen(req, timeout=8.0)
        content_type = resp.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
        h.send_response(200)
        h.send_header("Content-Type", content_type)
        h.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        h.end_headers()
        streaming = True
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            h.wfile.write(chunk)
            h.wfile.flush()
        return
    except Exception as e:
        if not streaming:     # nothing sent yet: say why instead of closing the connection without an answer
            h.send_json({"ok": False, "error": f"camera stream unavailable: {e}"}, 502)
        return


def _post_api_presence_ask(h, parsed, body):
    """POST /api/presence/ask"""
    try:
        query = body.get("query", "").strip()
        if not query:
            h.send_json({"ok": False, "error": "Query required"}, 400)
            return
        from harness.core.home_presence_hub import home_presence_hub
        ans = home_presence_hub.ask_courage_computer(query)
        h.send_json({"ok": True, "data": ans})
    except Exception as e:
        h.send_json({"ok": False, "error": str(e)}, 500)
    return


def _post_api_presence_speak(h, parsed, body):
    """POST /api/presence/speak"""
    try:
        text = body.get("text", "").strip()
        voice = body.get("voice", "bm_george")
        speed = float(body.get("speed", 1.06))
        from harness.connectors.voice_connector import voice_connector
        import base64
        from hearth import tts_cache
        # short lines Computer says over and over come from disk (tts_cache.py); the rest is synthesised
        audio, cached = tts_cache.get_or_make(_tts_cache_dir(), text, voice, speed,
                                              lambda t, v, sp: voice_connector.synthesize_speech(t, voice=v, speed=sp))
        if audio:
            b64 = base64.b64encode(audio).decode("ascii")
            h.send_json({"ok": True, "audio_base64": b64, "cached": cached})
        else:
            h.send_json({"ok": False, "error": "TTS synthesis failed"}, 500)
    except Exception as e:
        h.send_json({"ok": False, "error": str(e)}, 500)
    return


def _post_api_voice_transcribe(h, parsed, body):
    """POST /api/voice/transcribe"""
    # Speech-to-text: the voice server on the engine host first, the legacy voice host as the fallback (voice_client.py).
    # Frontend sends: { "audio_base64": "...", "mime_type": "audio/webm" }
    try:
        import base64 as b64mod
        audio_b64 = body.get("audio_base64", "")
        mime_type = body.get("mime_type", "audio/webm")
        if not audio_b64:
            h.send_json({"ok": False, "error": "No audio_base64 provided"}, 400)
            return
        audio_data = b64mod.b64decode(audio_b64)
        if len(audio_data) < 100:
            h.send_json({"ok": False, "error": "Audio too short"}, 400)
            return
        from hearth import voice_client
        res = voice_client.transcribe(load_config(), audio_data, mime_type)
        h.send_json({"ok": True, "text": (res.get("text") or "").strip(),
                        "engine": res.get("engine"), "ms": res.get("ms"), "via": res.get("via")})
    except Exception as e:
        logger.error("Voice transcribe error: %s", e)
        h.send_json({"ok": False, "error": str(e)}, 502)
    return


def _post_api_voice_bench_sample(h, parsed, body):
    """POST /api/voice/bench/sample"""
    # One recorded phrase: saved, then every engine transcribes it (voice_bench.py)
    try:
        import base64 as b64mod
        from hearth import voice_bench
        audio_data = b64mod.b64decode(body.get("audio_base64", ""))
        if len(audio_data) < 100:
            h.send_json({"ok": False, "error": "Audio too short"}, 400)
            return
        rec = voice_bench.save_and_score(load_config(), _stonesage_data_dir(), body.get("phrase_id", ""),
                                         audio_data, body.get("mime_type", "audio/webm"))
        h.send_json({"ok": True, **rec})
    except ValueError as e:
        h.send_json({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        logger.error("Voice bench error: %s", e)
        h.send_json({"ok": False, "error": str(e)}, 500)
    return


def _post_api_ha_entity_customize(h, parsed, body):
    """POST /api/ha/entity/customize"""
    entity_id = body.get("entity_id", "").strip()
    if not entity_id:
        h.send_json({"ok": False, "error": "Missing entity_id"}, 400)
        return
    cfg = load_config()
    if "ha_entity_customizations" not in cfg:
        cfg["ha_entity_customizations"] = {}
    cfg["ha_entity_customizations"][entity_id] = {
        "custom_name": body.get("custom_name", "").strip(),
        "room": body.get("room", "").strip(),
        "icon": body.get("icon", "").strip(),
        "hidden": bool(body.get("hidden", False)),
        "updated_at": datetime.now().isoformat()
    }
    save_config(cfg)
    h.send_json({"ok": True, "entity_id": entity_id, "customization": cfg["ha_entity_customizations"][entity_id]})
    return


def _post_api_presence_correct(h, parsed, body):
    """POST /api/presence/correct, /api/presence/profiles"""
    path = parsed.path
    # Residents & Pets: "✗ Wrong" / "Correct as" on a sighting, and "+ New profile" (presence_corrections.py)
    try:
        if path == "/api/presence/profiles":
            res = _presence_corrections().add_profile(body)
        else:
            res = _apply_presence_correction(body.get("source", ""), body.get("ref", ""), body.get("shown", ""),
                                             body.get("action", ""), body.get("name", ""))
        if res.get("ok"):
            _invalidate_presence()
        h.send_json(res, 200 if res.get("ok") else 400)
    except Exception as e:
        h.send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 502)
    return


def _post_api_patrol_run(h, parsed, body):
    """POST /api/patrol/run"""
    h.send_json(get_patrol().run_now(body.get("entity", "")))
    return


def _post_api_commentary_test(h, parsed, body):
    """POST /api/commentary/test"""
    # What Computer would say for a made-up event, never spoken: {trigger, facts, who?: [names], room?}
    c = get_commentary()
    from hearth.commentary import _cfg
    facts = str(body.get("facts") or "")[:400]
    if not facts:
        h.send_json({"ok": False, "error": "facts required"}, 400)
        return
    rec = c.remark(_cfg(load_config().get("commentary")), str(body.get("trigger") or "test")[:20],
                   str(body.get("room") or "kitchen")[:20],
                   [str(w)[:30].lower() for w in (body.get("who") or [])][:3], facts, dry=True)
    h.send_json({"ok": rec.get("outcome") == "dry_run", **rec})
    return


def _post_api_cameras_webrtc_report(h, parsed, body):
    """POST /api/cameras/webrtc-report"""
    # Browser-side ICE diagnostics after a failed WebRTC stream (presence_garden.js reportIceFailure)
    r = body or {}
    logger.warning("WebRTC failed from %s (%s) on %s: ua=%s | local=%s | remote=%s | pairs=%s",
                   h.client_address[0], str(r.get("page"))[:60], str(r.get("stream"))[:60],
                   str(r.get("ua"))[:160], r.get("local"), r.get("remote"), r.get("pairs"))
    h.send_json({"ok": True})
    return


def _post_api_cameras_ptz(h, parsed, body):
    """POST /api/cameras/ptz"""
    # PTZ is free for Courage and the owner (CLAUDE.md); names and presets are checked against HA
    from hearth import camera_ui
    try:
        kind, eid, option = camera_ui.ptz_command(body.get("entity", ""), load_config(), body.get("action", ""),
                                                  body.get("preset", ""), hass.get_state)
        get_patrol().note_manual(body.get("entity", ""))  # the owner is steering: the patrol keeps out of the way
        res = hass.press_button(eid) if kind == "button" else hass.select_option(eid, option)
        h.send_json({"ok": bool(res.get("ok", True)), "done": eid, "error": res.get("error")})
    except ValueError as e:
        h.send_json({"ok": False, "error": str(e)}, 400)
    return


def _post_api_cameras_webrtc(h, parsed, body):
    """POST /api/cameras/webrtc"""
    # WebRTC signalling relay to go2rtc; only streams the LIVE tab lists are allowed
    fcfg = load_config().get("frigate") or {}
    stream = body.get("stream", "")
    try:
        from hearth.frigate_presence import webrtc_answer
        allowed = set(_webrtc_streams(load_config()).values())
        if stream not in allowed:
            h.send_json({"ok": False, "error": f"unknown stream '{stream}'"}, 400)
            return
        h.send_json({"ok": True, "answer": webrtc_answer(fcfg["go2rtc_url"], stream, body.get("offer") or {})})
    except Exception as e:
        h.send_json({"ok": False, "error": f"go2rtc: {e}"}, 502)
    return


def _post_api_ha_service(h, parsed, body):
    """POST /api/ha/service"""
    domain = body.get("domain", "")
    service = body.get("service", "")
    data = body.get("service_data", {})

    # Check whitelist
    is_valid, msg, clean_data = validate_ha_action(domain, service, data)
    if not is_valid:
        h.send_json({"ok": False, "error": f"Whitelist rejection: {msg}"}, 403)
        return

    res = hass.call_service(domain, service, clean_data)
    h.send_json(res)
    return


def _post_api_ha_command(h, parsed, body):
    """POST /api/ha/command"""
    prompt = body.get("prompt", "").strip()
    if not prompt:
        h.send_json({"ok": False, "error": "Prompt cannot be empty"}, 400)
        return

    # 1. Fast deterministic whitelist check
    direct_action = extract_whitelisted_chat_action(prompt)
    if direct_action and direct_action.get("category") == "home_assistant":
        dom = direct_action["domain"]
        srv = direct_action["service"]
        s_data = direct_action["service_data"]
        call_res = hass.call_service(dom, srv, s_data)
        h.send_json({
            "ok": call_res.get("ok", True),
            "message": f"✨ Executed: {direct_action.get('description', f'{dom}.{srv}')}",
            "action": direct_action,
            "result": call_res
        })
        return

    try:
        # Ask the worker engine to parse user intent into a JSON tool action
        parse_payload = {
            "model": "worker",
            "messages": [
                {
                    "role": "system",
                    "content": "You are Varda, Home Assistant NLP automation parser. Given a user command, output valid JSON with keys: 'domain' (e.g. light, switch, climate, homeassistant), 'service' (e.g. turn_on, turn_off, set_temperature), 'service_data' (dict, including entity_id if applicable). Output ONLY valid JSON."
                },
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 120,
            "temperature": 0.1
        }
        req = urllib.request.Request(
            endpoints.url("worker", load_config()) + "/chat/completions",
            data=json.dumps(parse_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            content = res_data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                action = json.loads(m.group(0))
                domain = action.get("domain", "homeassistant")
                service = action.get("service", "turn_on")
                s_data = action.get("service_data", {})
                if "entity_id" in action and "entity_id" not in s_data:
                    s_data["entity_id"] = action["entity_id"]

                # Whitelist validation before execution
                is_valid, msg, clean_s_data = validate_ha_action(domain, service, s_data)
                if not is_valid:
                    h.send_json({"ok": False, "error": f"Whitelist rejection: {msg}"}, 403)
                    return

                call_res = hass.call_service(domain, service, clean_s_data)
                h.send_json({
                    "ok": call_res.get("ok", True),
                    "message": f"✨ Varda executed: {domain}.{service} on {clean_s_data.get('entity_id', 'target')}",
                    "action": action,
                    "result": call_res
                })
                return
    except Exception as ex:
        logger.error(f"Error executing /api/ha/command: {ex}")
    h.send_json({"ok": False, "error": f"Failed to execute command: {prompt}"}, 500)
    return


def _get_api_hass_summary(h, parsed, body):
    """GET /api/hass/summary, /api/ha/dashboard"""
    summary = hass.get_dashboard_summary()
    helpers_res = hass.get_states(domain_filter="input_boolean")
    autos_res = hass.get_states(domain_filter="automation")
    summary["helpers"] = helpers_res.get("entities", [])
    summary["automations"] = [a for a in autos_res.get("entities", []) if any(k in a.get("entity_id", "") for k in ["stonesage", "bedtime", "movie", "patrol", "eco"])]

    # Curated Camera Feeds
    summary["cameras"] = _curated_cameras()
    summary["customizations"] = load_config().get("ha_entity_customizations", {})
    h.send_json(summary)
    return


def _post_api_hass_call(h, parsed, body):
    """POST /api/hass/call, /api/ha/service"""
    cluster.signal_preemption(reason="ha_service_call", in_flight=False)
    domain = body.get("domain", "")
    service = body.get("service", "")
    service_data = body.get("service_data", {})
    entity_id = str(service_data.get("entity_id", "")).lower()

    # CRITICAL SAFETY INVARIANT: Block power cuts to physical server infrastructure
    if domain in ["switch"] and service in ["turn_off", "toggle"]:
        from stonesage.courage.tools import infra_words
        blocklist = infra_words(load_config().get("infra_words"))
        if any(b in entity_id for b in blocklist):
            h.send_json({
                "ok": False,
                "error": f"SAFETY VIOLATION BLOCKED: Cannot cut power to server infrastructure ({entity_id})."
            }, 403)
            return

    res = hass.call_service(domain, service, service_data)
    h.send_json(res)
    return


# What the routes above use, by where it comes from now (2026-10-09, step F3)
FROM_SERVICES = ("_apply_presence_correction", "_courage_presence", "_go2rtc_stream_names", "_invalidate_presence",
                 "_presence_corrections", "_sentry_snapshot_bytes", "_sighting_box", "_tts_cache_dir", "_webrtc_streams",
                 "get_adguard_blocks", "get_alexa_relay", "get_commentary", "get_frigate_presence", "get_ha_presence",
                 "get_patrol")


def install(app_ctx) -> None:
    global ctx
    ctx = app_ctx
    from hearth import services
    from hearth.ha_policy import extract_whitelisted_chat_action as _extract, validate_ha_action as _validate
    services.bind(app_ctx)
    g = globals()
    g.update({n: getattr(services, n) for n in FROM_SERVICES})          # Home's own services
    g.update(validate_ha_action=_validate, extract_whitelisted_chat_action=_extract,
             _stonesage_data_dir=app_ctx.data_dir,
             load_config=app_ctx.services["load_config"], save_config=app_ctx.services["save_config"])
    from stonesage.apps import LiveRef
    # Home's own clients, as live references: config_saved rebuilds Immich, and these must follow
    g.update(hass=LiveRef(lambda: services.hass), immich=LiveRef(lambda: services.immich))
    if app_ctx.tool_registry is not None:
        from hearth import ha_tools
        ha_tools.register(app_ctx.tool_registry)   # Home Assistant tools for agents without Computer's loop
    g.update({n: app_ctx.services[n] for n in PLATFORM_SERVICES})
    # what the platform asks the house (apps.py hooks)
    app_ctx.hooks.update(courage_home=services.courage_home, notify_phone=services.notify_phone,
                         privacy_terms=services.privacy_terms, gpu_busy=services.gpu_busy,
                         household=services.household, ground_context=services.ground_context,
                         chat_action=services.chat_action, config_saved=services.config_saved)
    app_ctx.on_start(services.start)
    ctx.add_route("GET", "/api/profiles", _get_api_profiles)
    ctx.add_route("GET", "/api/frigate/presence", _get_api_frigate_presence)
    ctx.add_route("GET", "/api/patrol/status", _get_api_patrol_status)
    ctx.add_route("GET", "/api/ha-presence/status", _get_api_ha_presence_status)
    ctx.add_route("GET", "/api/adguard-blocks/status", _get_api_adguard_blocks_status)
    ctx.add_route("GET", "/api/alexa-relay/status", _get_api_alexa_relay_status)
    ctx.add_route("GET", "/api/commentary/status", _get_api_commentary_status)
    ctx.add_route("GET", "/api/patrol/frame", _get_api_patrol_frame)
    ctx.add_route("GET", "/api/presence/profiles", _get_api_presence_profiles)
    ctx.add_route("GET", "/api/presence/status", _get_api_presence_status)
    ctx.add_route("GET", "/api/cameras/live", _get_api_cameras_live)
    ctx.add_route("GET", "/api/cameras/snapshot", _get_api_cameras_snapshot)
    ctx.add_route("GET", "/api/cameras/mp4", _get_api_cameras_mp4)
    ctx.add_route("GET", "/api/frigate/snapshot/", _get_api_frigate_snapshot, prefix=True)
    ctx.add_route("GET", "/api/presence/snapshot/", _get_api_presence_snapshot, prefix=True)
    ctx.add_route("GET", "/api/presence/box", _get_api_presence_box)
    ctx.add_route("GET", "/api/ha/status", _get_api_ha_status)
    ctx.add_route("GET", "/api/ha/dashboard", _get_api_ha_dashboard)
    ctx.add_route("GET", "/api/ha/entities", _get_api_ha_entities)
    ctx.add_route("GET", "/api/immich/stats", _get_api_immich_stats)
    ctx.add_route("GET", "/api/immich/assets", _get_api_immich_assets)
    ctx.add_route("GET", "/api/immich/people", _get_api_immich_people)
    ctx.add_route("GET", "/api/voice/cache", _get_api_voice_cache)
    ctx.add_route("GET", "/api/voice/bench/phrases", _get_api_voice_bench_phrases)
    ctx.add_route("GET", "/api/voice/bench/results", _get_api_voice_bench_results)
    ctx.add_route("GET", "/api/hass/cameras", _get_api_hass_cameras)
    ctx.add_route("GET", "/api/hass/camera_snapshot", _get_api_hass_camera_snapshot)
    ctx.add_route("GET", "/api/hass/camera_stream", _get_api_hass_camera_stream)
    ctx.add_route("POST", "/api/presence/ask", _post_api_presence_ask)
    ctx.add_route("POST", "/api/presence/speak", _post_api_presence_speak)
    ctx.add_route("POST", "/api/voice/transcribe", _post_api_voice_transcribe)
    ctx.add_route("POST", "/api/voice/bench/sample", _post_api_voice_bench_sample)
    ctx.add_route("POST", "/api/ha/entity/customize", _post_api_ha_entity_customize)
    ctx.add_route("POST", "/api/presence/correct", _post_api_presence_correct)
    ctx.add_route("POST", "/api/presence/profiles", _post_api_presence_correct)
    ctx.add_route("POST", "/api/patrol/run", _post_api_patrol_run)
    ctx.add_route("POST", "/api/commentary/test", _post_api_commentary_test)
    ctx.add_route("POST", "/api/cameras/webrtc-report", _post_api_cameras_webrtc_report)
    ctx.add_route("POST", "/api/cameras/ptz", _post_api_cameras_ptz)
    ctx.add_route("POST", "/api/cameras/webrtc", _post_api_cameras_webrtc)
    ctx.add_route("POST", "/api/ha/service", _post_api_ha_service)
    ctx.add_route("POST", "/api/ha/command", _post_api_ha_command)
    ctx.add_route("GET", "/api/hass/summary", _get_api_hass_summary)
    ctx.add_route("POST", "/api/hass/call", _post_api_hass_call, feature="raw_ha_call")   # raw service call: off unless features.raw_ha_call
    from hearth import setup_wizard
    setup_wizard.install(app_ctx)       # the setup wizard's routes (/api/hearth/setup...)
