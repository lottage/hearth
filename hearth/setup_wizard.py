"""Hearth's setup wizard (docs/LAYERS.md M4): the browser version of `python -m stonesage.first_run` for the Home half of
config.json -- the Home Assistant address and token, who lives there, their phones and speakers, and the cameras.

    GET  /api/hearth/setup                what is set and what is missing (never the token: only whether one is stored)
    POST /api/hearth/setup/test-ha        {url, token?}  can we talk to that Home Assistant? (token omitted = the stored one,
                                          but only for the stored address, so a stored token is never sent somewhere else)
    GET  /api/hearth/setup/discover       read-only suggestions from the stored Home Assistant connection: people, phones,
                                          speakers, cameras (pets: Home Assistant has none)
    POST /api/hearth/setup/save           {homeassistant:{url,token?}, household:{...}, camera_ui?:{...}, camera_ui_remove?:[...]}
                                          validated, merged into config.json (unknown keys kept, a backup first), then the
                                          running Home app re-reads it

The token is written only to config.json. It is not returned by any route, not logged, and not part of any error text.
Pure functions (status, validate, merge, discover_from) carry the logic; the route functions are thin.
"""

import copy
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from lodestone.config_mask import MASK
from hearth.household import camera_key, household_from_config
from stonesage import first_run

logger = logging.getLogger("StoneSage.home")

ctx = None                    # apps.AppContext, set by install()
HA_TIMEOUT_S = 4.0
SAVEABLE = ("homeassistant", "household", "camera_ui", "camera_ui_remove")
HOUSEHOLD_FIELDS = ("people", "owner_pronouns", "pets", "phones", "speakers", "descriptions")
PLACEHOLDERS = ("your_", "changeme", "xxxx", "<")      # config.example.json values nobody replaced
KEY = re.compile(r"^[a-z0-9_]+$")
SERVICE = re.compile(r"^[a-z0-9_]+$")
ENTITY = re.compile(r"^camera\.[a-z0-9_]+$")
# words that name a device rather than a room: "alexa_media_kitchen_echo_show_8_2" -> "kitchen"
DEVICE_WORDS = {"echo", "dot", "show", "spot", "pop", "studio", "flex", "input", "plus", "auto", "hub", "sub", "gen"}
STREAM_NAME = re.compile(r"\s+(hd|sd)?\s*stream(\s+direct)?$", re.I)


# ---------------------------------------------------------------- reading the config ----
def _config_file() -> str:
    return (ctx.services.get("config_file") if ctx else None) or first_run.CONFIG_FILE


def _current() -> Dict[str, Any]:
    """config.json as it is on disk; {} when there is none (the platform's load_config falls back to the example file,
    whose placeholder token and addresses must not be mistaken for a real setup, or written back)."""
    if not os.path.exists(_config_file()):
        return {}
    cfg = ctx.services["load_config"]()
    return cfg if isinstance(cfg, dict) else {}


def _real(value: Any) -> str:
    v = value.strip() if isinstance(value, str) else ""
    return "" if not v or any(v.lower().startswith(p) for p in PLACEHOLDERS) else v


def _stored_ha(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """(url, token) as configured; '' when unset or still the example's placeholder."""
    ha = cfg.get("homeassistant") or {}
    return _real(ha.get("url")), _real(ha.get("token"))


def setup_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What is configured and what is missing. `required` gaps make it incomplete; `suggested` ones do not."""
    url, token = _stored_ha(cfg)
    block = cfg.get("household") or {}
    hh = household_from_config(cfg)
    required, suggested = [], []
    if not url:
        required.append("homeassistant.url")
    if not token:
        required.append("homeassistant.token")
    if not hh.people:
        required.append("household.people")
    if not block.get("phones"):
        suggested.append("household.phones")
    if not block.get("speakers"):
        suggested.append("household.speakers")
    if not hh.cameras:
        suggested.append("cameras")
    return {
        "ok": True,
        "complete": not required,
        "missing": required,
        "suggested": suggested,
        "homeassistant": {"url": url, "url_set": bool(url), "token_set": bool(token)},
        "household": {
            "people": hh.people, "owner_pronouns": list(hh.owner_pronouns), "pets": hh.pets,
            "phones": dict(block.get("phones") or {}), "speakers": dict(block.get("speakers") or {}),
            "descriptions": hh.descriptions,
        },
        "cameras": [{"key": k, "entity": c.get("entity", ""), "name": c.get("name", k)} for k, c in hh.cameras.items()],
    }


# ---------------------------------------------------------------- talking to Home Assistant ----
def _clean_url(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return None
    u = url.strip().rstrip("/")
    p = urllib.parse.urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    return u


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):      # the token must not follow a redirect to another address
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _ha_get(url: str, token: str, path: str) -> Any:
    """GET <url><path> with the bearer token; parsed JSON. Raises urllib errors (their text never contains the token)."""
    req = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with _opener.open(req, timeout=HA_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_connection(url: str, token: str) -> Dict[str, Any]:
    """{ok, error|version, location}: is there a Home Assistant at url that accepts the token?"""
    try:
        _ha_get(url, token, "/api/")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error": f"Home Assistant answered, but refused the token (HTTP {e.code}). "
                                          "Make a new long-lived access token in your HA profile page."}
        return {"ok": False, "error": f"Home Assistant answered HTTP {e.code} {e.reason}: is that the right address?"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Cannot reach {url}: {e.reason}"}
    except (OSError, ValueError) as e:                 # timeouts, a reply that is not JSON (another web server)
        return {"ok": False, "error": f"No Home Assistant answer at {url}: {type(e).__name__}"}
    out: Dict[str, Any] = {"ok": True, "message": "Connected."}
    try:
        info = _ha_get(url, token, "/api/config")
        out["version"], out["location"] = info.get("version", ""), info.get("location_name", "")
    except Exception:                                  # /api/ worked; the version is a nicety
        pass
    return out


def _room_of(service: str) -> str:
    """'alexa_media_kitchen_echo_show_8_2' -> 'kitchen'; the whole name if nothing is left."""
    parts = [p for p in service[len("alexa_media_"):].split("_") if p and not p.isdigit() and p not in DEVICE_WORDS]
    return "_".join(parts) or service[len("alexa_media_"):]


def discover_from(states: List[Dict[str, Any]], services: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Suggestions from Home Assistant's /api/states and /api/services. Pure, read-only."""
    people = []
    for st in states:
        eid = st.get("entity_id", "")
        if eid.startswith("person."):
            name = (st.get("attributes") or {}).get("friendly_name") or eid.split(".", 1)[1].replace("_", " ").title()
            people.append({"entity": eid, "name": name, "key": re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")})
    notify = sorted({s for d in services if d.get("domain") == "notify" for s in (d.get("services") or {})})
    phones = []
    for svc in (s for s in notify if s.startswith("mobile_app_")):
        guess = next((p["key"] for p in people if any(len(t) >= 3 and t in svc for t in p["key"].split("_"))), "")
        phones.append({"service": svc, "person": guess})
    speakers, taken = [], set()
    for svc in (s for s in notify if s.startswith("alexa_media_") and s != "alexa_media_last_called"):
        room = _room_of(svc)
        n = 2
        while room in taken:                           # two Echos in one room
            room, n = f"{_room_of(svc)}_{n}", n + 1
        taken.add(room)
        speakers.append({"room": room, "service": svc})
    by_key: Dict[str, Dict[str, Any]] = {}
    for st in states:
        eid = st.get("entity_id", "")
        if not eid.startswith("camera."):
            continue
        key = camera_key(eid)
        name = STREAM_NAME.sub("", (st.get("attributes") or {}).get("friendly_name") or key.replace("_", " ").title())
        cam = by_key.setdefault(key, {"key": key, "entity": eid, "name": name, "variants": []})
        cam["variants"].append(eid)
        if "hd_stream" in eid and "hd_stream" not in cam["entity"]:      # the picture, not the low-resolution twin
            cam["entity"] = eid
    return {"ok": True, "people": people, "pets": [], "phones": phones, "speakers": speakers,
            "cameras": list(by_key.values()),
            "notes": ["Home Assistant has no pets: add them yourself.",
                      "Pick the phone for each person and the rooms you want Computer to speak in."]}


# ---------------------------------------------------------------- validating and merging a save ----
def _name_list(value: Any, what: str, errors: List[str]) -> List[str]:
    if not isinstance(value, list):
        errors.append(f"{what} must be a list of names")
        return []
    out: List[str] = []
    for v in value:
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{what}: a name is empty")
        elif v.strip().lower() in [o.lower() for o in out]:
            errors.append(f"{what}: {v.strip()!r} is listed twice")
        else:
            out.append(v.strip())
    return out


def _key_map(value: Any, what: str, errors: List[str]) -> Dict[str, str]:
    """{lower-case key: HA notify service}"""
    if not isinstance(value, dict):
        errors.append(f"{what} must map a lower-case key to a Home Assistant notify service")
        return {}
    out = {}
    for k, v in value.items():
        if not isinstance(k, str) or not KEY.match(k):
            errors.append(f"{what}: key {k!r} must be lower-case letters, digits and underscores")
        elif not isinstance(v, str) or not SERVICE.match(v.strip()):
            errors.append(f"{what}: {k!r} needs a notify service name such as mobile_app_alex_phone")
        else:
            out[k] = v.strip()
    return out


def _pronouns(value: Any, errors: List[str]) -> List[str]:
    parts = [x.strip().lower() for x in value.replace("/", ",").split(",")] if isinstance(value, str) else \
        [str(x).strip().lower() for x in value] if isinstance(value, list) else []
    if len(parts) != 2 or not all(re.match(r"^[a-z]+$", p) for p in parts):
        errors.append("owner_pronouns must be a pair: subject and object, e.g. she/her")
        return []
    return parts


def validate(body: Any, existing: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """(clean changes, errors). Changes keep only the sections the body gave; `existing` is only read (descriptions are
    checked against the people and pets that will exist after the save)."""
    errors: List[str] = []
    if not isinstance(body, dict) or not body:
        return {}, ["nothing to save"]
    for k in body:
        if k not in SAVEABLE:
            errors.append(f"unknown field {k!r}")
    clean: Dict[str, Any] = {}

    ha = body.get("homeassistant")
    if ha is not None:
        if not isinstance(ha, dict):
            errors.append("homeassistant must be {url, token?}")
        else:
            out: Dict[str, str] = {}
            if "url" in ha:
                u = _clean_url(ha["url"])
                if u is None:
                    errors.append("homeassistant.url must be an http:// or https:// address, e.g. http://homeassistant.local:8123")
                else:
                    out["url"] = u
            tok = ha.get("token")
            if isinstance(tok, str) and tok.strip() and not tok.startswith(MASK):     # blank or masked = keep the stored token
                tok = tok.strip()
                if not tok.isascii() or re.search(r"\s", tok):
                    errors.append("homeassistant.token has spaces or odd characters: paste the token again")
                else:
                    out["token"] = tok
            elif tok is not None and not isinstance(tok, str):
                errors.append("homeassistant.token must be text")
            stored_url, stored_tok = _stored_ha(existing)
            if "url" in out and stored_tok and "token" not in out and out["url"].rstrip("/") != stored_url.rstrip("/"):
                # the stored token must not follow a new address (it would be sent there on the next call)
                errors.append("homeassistant.url changed: paste the token again (a stored token is never sent to a new address)")
            if out:
                clean["homeassistant"] = out

    hh = body.get("household")
    if hh is not None:
        if not isinstance(hh, dict):
            errors.append("household must be an object")
        else:
            out = {}
            for k in hh:
                if k not in HOUSEHOLD_FIELDS:
                    errors.append(f"household.{k}: unknown field")
            if "people" in hh:
                out["people"] = _name_list(hh["people"], "people", errors)
            if "owner_pronouns" in hh:
                p = _pronouns(hh["owner_pronouns"], errors)
                if p:
                    out["owner_pronouns"] = p
            if "pets" in hh:
                pets = hh["pets"]
                if isinstance(pets, list):                                  # [{name, species}] from a form
                    pets = {p.get("name", ""): p.get("species", "") for p in pets if isinstance(p, dict)}
                if not isinstance(pets, dict):
                    errors.append("pets must map a name to a species")
                else:
                    out["pets"] = {}
                    for n, sp in pets.items():
                        if not isinstance(n, str) or not n.strip():
                            errors.append("pets: a name is empty")
                        elif not isinstance(sp, str) or not sp.strip():
                            errors.append(f"pets: {n!r} needs a species (cat, dog...)")
                        else:
                            out["pets"][n.strip()] = sp.strip().lower()
            if "phones" in hh:
                out["phones"] = _key_map(hh["phones"], "phones", errors)
            if "speakers" in hh:
                out["speakers"] = _key_map(hh["speakers"], "speakers", errors)
            if "descriptions" in hh:
                d = hh["descriptions"]
                if not isinstance(d, dict):
                    errors.append("descriptions must map a name to text")
                else:
                    out["descriptions"] = {}
                    block = existing.get("household") or {}
                    names = {n.lower() for n in (out.get("people", block.get("people")) or [])} | \
                            {n.lower() for n in (out.get("pets", block.get("pets")) or {})}
                    for n, text in d.items():
                        if not isinstance(text, str) or len(text) > 300:
                            errors.append(f"descriptions: {n!r} must be text of at most 300 characters")
                        elif str(n).strip().lower() not in names:
                            errors.append(f"descriptions: {n!r} is not one of the people or pets")
                        elif text.strip():
                            out["descriptions"][str(n).strip()] = text.strip()
            clean["household"] = out

    cams = body.get("camera_ui")
    if cams is not None:
        if not isinstance(cams, dict):
            errors.append("camera_ui must map a camera entity to {name}")
        else:
            out = {}
            for entity, c in cams.items():
                name = c.get("name") if isinstance(c, dict) else None
                if not isinstance(entity, str) or not ENTITY.match(entity):
                    errors.append(f"camera_ui: {entity!r} is not a camera entity (camera.something)")
                elif not isinstance(name, str) or not name.strip():
                    errors.append(f"camera_ui: {entity} needs a display name")
                else:
                    out[entity] = {"name": name.strip()}
            clean["camera_ui"] = out
    rm = body.get("camera_ui_remove")
    if rm is not None:
        if not isinstance(rm, list) or not all(isinstance(e, str) and ENTITY.match(e) for e in rm):
            errors.append("camera_ui_remove must list camera entities")
        else:
            clean["camera_ui_remove"] = list(rm)
    return clean, errors


def merge(cfg: Dict[str, Any], changes: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """(new config, names of the changed sections). Only the given fields change; every other key stays as it was."""
    new = copy.deepcopy(cfg)
    changed: List[str] = []
    for k, v in (changes.get("homeassistant") or {}).items():
        if (new.get("homeassistant") or {}).get(k) != v:
            new.setdefault("homeassistant", {})[k] = v
            changed.append(f"homeassistant.{k}")
    for k, v in (changes.get("household") or {}).items():
        if (new.get("household") or {}).get(k) != v:
            new.setdefault("household", {})[k] = v
            changed.append(f"household.{k}")
    for entity, c in (changes.get("camera_ui") or {}).items():
        entry = new.setdefault("camera_ui", {}).setdefault(entity, {})
        if entry.get("name") != c["name"]:
            entry["name"] = c["name"]                          # live / ptz / variants of an existing tile stay
            changed.append(f"camera_ui.{entity}")
    for entity in changes.get("camera_ui_remove") or []:
        if entity in (new.get("camera_ui") or {}):
            del new["camera_ui"][entity]
            changed.append(f"camera_ui.{entity} (removed)")
    return new, changed


# ---------------------------------------------------------------- applying a save to the running app ----
def apply_to_running(new: Dict[str, Any], changed: List[str]) -> Dict[str, Any]:
    """Make the running process see `new`: the live config object (Home reads it per call, so the household, speakers and
    cameras follow at once), the Home Assistant client, and Computer's tools if the agent is already built.
    Returns {restart_needed, notes}."""
    notes: List[str] = []
    for section in ("homeassistant", "household", "camera_ui"):
        if section in new:
            ctx.config[section] = copy.deepcopy(new[section])
        elif section in ctx.config:
            del ctx.config[section]
    saved = ctx.hooks.get("config_saved")
    if saved:
        saved(new)                                    # Home Assistant token and address, Immich
    agent_if_built = ctx.services.get("courage_agent_if_built")
    agent = agent_if_built() if agent_if_built else None
    if agent is not None:
        agent.tools.deps.household = household_from_config(ctx.config)
    restart = any(c.startswith("homeassistant") or c == "household.phones" for c in changed)
    if restart:
        notes.append("Restart StoneSage to pick up phone approvals (they connect to Home Assistant when it starts).")
    if any(c in ("household.people", "household.pets", "household.owner_pronouns") for c in changed):
        notes.append("Computer's saved persona text keeps the household it was created with; names, tools and cameras "
                     "follow the change now.")
    return {"restart_needed": restart, "notes": notes}


def save(body: Any) -> Tuple[int, Dict[str, Any]]:
    cfg = _current()
    changes, errors = validate(body, cfg)
    if errors:
        return 400, {"ok": False, "errors": errors}
    new, changed = merge(cfg, changes)
    if not changed:
        return 200, {"ok": True, "changed": [], "message": "Nothing changed.", "restart_needed": False, "notes": [],
                     "status": setup_status(cfg)}
    backup = first_run.backup_file(_config_file())
    ctx.services["save_config"](new)
    logger.info("Hearth setup saved: %s", ", ".join(changed))     # field names only: never values
    effect = apply_to_running(new, changed)
    return 200, {"ok": True, "changed": changed, "backup": backup and os.path.basename(backup),
                 "message": "Saved.", **effect, "status": setup_status(new)}


# ---------------------------------------------------------------- routes ----
def _get_setup(h, parsed, body):
    h.send_json(setup_status(_current()))


def _post_test_ha(h, parsed, body):
    body = body if isinstance(body, dict) else {}
    url = _clean_url(body.get("url"))
    if url is None:
        h.send_json({"ok": False, "error": "Enter an http:// or https:// address, e.g. http://homeassistant.local:8123"}, 400)
        return
    token = body.get("token")
    token = token.strip() if isinstance(token, str) and not token.startswith(MASK) else ""
    if not token:
        stored_url, stored_token = _stored_ha(_current())
        if stored_token and stored_url.rstrip("/") == url:
            token = stored_token
        elif stored_token:
            h.send_json({"ok": False, "error": "Paste the token to test a different address: the stored one is only used for the stored address."}, 400)
            return
    if not token:
        h.send_json({"ok": False, "error": "Paste a long-lived access token (HA profile page, bottom)."}, 400)
        return
    h.send_json(check_connection(url, token))


def _get_discover(h, parsed, body):
    url, token = _stored_ha(_current())
    if not url or not token:
        h.send_json({"ok": False, "error": "Save the Home Assistant address and token first (step 1)."}, 400)
        return
    check = check_connection(url, token)
    if not check["ok"]:
        h.send_json({"ok": False, "error": check["error"]}, 502)
        return
    try:
        states = _ha_get(url, token, "/api/states")
        services = _ha_get(url, token, "/api/services")
    except Exception as e:
        h.send_json({"ok": False, "error": f"Home Assistant stopped answering while reading: {type(e).__name__}"}, 502)
        return
    h.send_json(discover_from(states if isinstance(states, list) else [], services if isinstance(services, list) else []))


def _post_save(h, parsed, body):
    status, out = save(body)
    h.send_json(out, status)


def install(app_ctx) -> None:
    global ctx
    ctx = app_ctx
    app_ctx.add_route("GET", "/api/hearth/setup", _get_setup)
    app_ctx.add_route("POST", "/api/hearth/setup/test-ha", _post_test_ha)
    app_ctx.add_route("GET", "/api/hearth/setup/discover", _get_discover)
    app_ctx.add_route("POST", "/api/hearth/setup/save", _post_save)
