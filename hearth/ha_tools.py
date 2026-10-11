"""Home Assistant tools for the platform's generic tool registry (tool_harness.ToolRegistry): poll entities, read one
entity's state, call a service. Home layer (docs/LAYERS.md): the registry never imports this module; the server calls
register(tool_registry) once, which adds the tools, their aliases, their text-call names and their prompt rules.
Moved out of tool_harness.py (2026-10-09). Actions are checked by hearth/ha_policy.py and refused if the check fails."""

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from hearth.ha_policy import validate_ha_action

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HA_URL = "http://homeassistant.local:8123"   # Home Assistant's standard address; config.json / HASS_URL override

def _get_ha_config() -> Tuple[str, str]:
    """Retrieves Home Assistant URL and long-lived access token from env or backend config.json."""
    token = os.environ.get("HASS_TOKEN", "")
    url = os.environ.get("HASS_URL", DEFAULT_HA_URL).rstrip("/")
    if not token:
        try:
            cfg_path = os.environ.get("STONESAGE_CONFIG_FILE") or os.path.join(BACKEND_DIR, "config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    ha_cfg = cfg.get("homeassistant", {})
                    token = ha_cfg.get("token", "")
                    if ha_cfg.get("url"):
                        url = ha_cfg.get("url").rstrip("/")
        except Exception:
            pass
    return url, token


def tool_home_assistant_call(domain: str, service: str, service_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Calls a Home Assistant service (lights, switches, thermostat)."""
    dom = (domain or "").strip().lower()
    srv = (service or "").strip().lower()
    data = dict(service_data or {})

    try:
        is_allowed, reason, sanitized_data = validate_ha_action(dom, srv, data)
    except Exception as e:  # fail closed: a broken safety check must never let the action through
        return {"ok": False, "error": f"Home Assistant action not checked ({e}); refused."}
    if not is_allowed:
        return {"ok": False, "error": f"Home Assistant action rejected: {reason}"}
    data = sanitized_data

    try:
        url, hass_token = _get_ha_config()
        if not hass_token:
            return {"ok": False, "error": "Home Assistant token not configured."}
        svc_url = f"{url}/api/services/{dom}/{srv}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {hass_token}"
        }
        req = urllib.request.Request(svc_url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return {"ok": True, "domain": dom, "service": srv, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def tool_home_assistant_entities(domain: Optional[str] = None, search: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
    """
    Performs a real-time live poll of Home Assistant to discover and inspect entities.
    Supports filtering by domain (e.g. 'climate', 'light', 'switch', 'sensor', 'binary_sensor', 'camera', 'media_player', 'automation', 'person')
    or searching across entity IDs and friendly names.
    If called without arguments, returns the total entity count, domain breakdown across all 1500+ devices, and key interactive entities.
    """
    url, hass_token = _get_ha_config()
    if not hass_token:
        return {"ok": False, "error": "Home Assistant token not configured."}

    dom_filter = (domain or "").strip().lower()
    search_term = (search or "").strip().lower()

    try:
        req = urllib.request.Request(
            f"{url}/api/states",
            headers={"Authorization": f"Bearer {hass_token}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            all_states = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"Failed polling Home Assistant: {e}"}

    total_count = len(all_states)

    # Domain summary
    domain_counts: Dict[str, int] = {}
    for s in all_states:
        d = s.get("entity_id", "").split(".")[0]
        domain_counts[d] = domain_counts.get(d, 0) + 1

    matched = []
    for s in all_states:
        eid = s.get("entity_id", "")
        item_dom = eid.split(".")[0]
        attrs = s.get("attributes", {})
        fname = attrs.get("friendly_name", eid)

        if dom_filter and item_dom != dom_filter:
            continue

        if search_term:
            if search_term not in eid.lower() and search_term not in fname.lower():
                continue

        # Extract compact attributes to save context window
        compact_attrs = {}
        for k in ("temperature", "current_temperature", "unit_of_measurement", "device_class",
                  "brightness", "hvac_action", "battery_level", "ip_address"):
            if k in attrs:
                compact_attrs[k] = attrs[k]

        matched.append({
            "entity_id": eid,
            "friendly_name": fname,
            "state": s.get("state"),
            "attributes": compact_attrs if compact_attrs else None
        })

    # If no filters supplied, return domain summary + sample of key interactive entities
    if not dom_filter and not search_term:
        priority_domains = ("climate", "light", "switch", "camera", "media_player", "person", "valve", "siren")
        interactive = [m for m in matched if m["entity_id"].split(".")[0] in priority_domains]
        return {
            "ok": True,
            "total_entities_in_ha": total_count,
            "domain_breakdown": domain_counts,
            "instructions": "Call home_assistant_entities(domain='<domain>') or home_assistant_entities(search='<keyword>') to inspect specific items.",
            "showing_sample_count": min(len(interactive), limit),
            "entities": interactive[:limit]
        }

    return {
        "ok": True,
        "total_entities_in_ha": total_count,
        "domain_filter": dom_filter or None,
        "search_term": search_term or None,
        "matched_count": len(matched),
        "showing_count": min(len(matched), limit),
        "entities": matched[:limit]
    }


def tool_home_assistant_get_state(entity_id: str) -> Dict[str, Any]:
    """Retrieves full real-time state and attributes for a specific Home Assistant entity."""
    eid = (entity_id or "").strip()
    if not eid:
        return {"ok": False, "error": "entity_id parameter is required."}

    url, hass_token = _get_ha_config()
    if not hass_token:
        return {"ok": False, "error": "Home Assistant token not configured."}

    try:
        req = urllib.request.Request(
            f"{url}/api/states/{eid}",
            headers={"Authorization": f"Bearer {hass_token}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "ok": True,
                "entity_id": data.get("entity_id"),
                "state": data.get("state"),
                "friendly_name": data.get("attributes", {}).get("friendly_name", eid),
                "attributes": data.get("attributes", {}),
                "last_changed": data.get("last_changed"),
                "last_updated": data.get("last_updated")
            }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error": f"Entity '{eid}' not found in Home Assistant."}
        return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def register(registry) -> None:
    """Add the Home Assistant tools to a ToolRegistry (call once at startup)."""
    registry.register_tool(
        name="home_assistant_call",
        description="Controls smart home devices (lights, plugs, switches, Nest thermostat).",
        parameters={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domain (climate, light, switch, etc.)."},
                "service": {"type": "string", "description": "Service name (turn_on, turn_off, set_temperature)."},
                "service_data": {"type": "object", "description": "Service parameters (entity_id, temperature, etc.)."}
            },
            "required": ["domain", "service"]
        },
        handler=tool_home_assistant_call,
        is_core=True
    )

    registry.register_tool(
        name="home_assistant_entities",
        description="Performs a real-time live poll of Home Assistant to discover entities, device states, and sensors across all 1500+ home devices. Supports domain filter (e.g. 'climate', 'light', 'switch', 'sensor', 'binary_sensor', 'camera', 'media_player') or keyword search across entity names. Call this whenever the user asks what devices or entities you can see, or to check real-time state across the home.",
        parameters={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Optional domain filter (e.g. 'climate', 'light', 'switch', 'sensor', 'binary_sensor', 'camera', 'media_player')."},
                "search": {"type": "string", "description": "Optional search keyword to match against entity_id or friendly name."},
                "limit": {"type": "integer", "description": "Maximum number of entities to return (default: 40).", "default": 40}
            }
        },
        handler=tool_home_assistant_entities,
        is_core=True
    )

    registry.register_tool(
        name="home_assistant_get_state",
        description="Fetches the complete real-time live state and all attributes for a specific Home Assistant entity_id (e.g. 'climate.hallway', 'switch.garden_pump').",
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "Exact entity_id in Home Assistant."}
            },
            "required": ["entity_id"]
        },
        handler=tool_home_assistant_get_state,
        is_core=True
    )
    registry.add_aliases({
        "ha_entities": "home_assistant_entities",
        "get_entities": "home_assistant_entities",
        "list_entities": "home_assistant_entities",
        "poll_entities": "home_assistant_entities",
        "ha_poll": "home_assistant_entities",
        "entities": "home_assistant_entities",
        "ha_state": "home_assistant_get_state",
        "get_state": "home_assistant_get_state",
        "ha_get_state": "home_assistant_get_state",
        "entity_state": "home_assistant_get_state",
    })
    registry.add_text_call_names(["home_assistant_call", "home_assistant_entities", "home_assistant_get_state", "ha_entities", "get_entities", "list_entities", "poll_entities", "ha_poll", "entities", "ha_state", "get_state"])
    registry.add_prompt_section("SMART HOME REAL-TIME SENSORY INVARIANTS", (
        "1. Real-Time Device Discovery: When asked what devices or entities you can see, or to check conditions across the home, NEVER claim devices don't exist based solely on a brief ambient summary. CALL `home_assistant_entities(domain=...)` or `home_assistant_entities()` to perform a real-time live poll of Home Assistant OS (which manages 1,500+ live sensors, switches, lights, cameras, climate units, and automations).\n"
        "2. State Inspection & Control: Use `home_assistant_get_state(entity_id=...)` to inspect exact attributes/states, and `home_assistant_call(domain=..., service=...)` to actuate devices.\n"
    ))
