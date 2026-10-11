"""Home Assistant action limits for Computer and the cockpit: which services may be called, verified and blocked
entities, the safe thermostat range, and the natural-language order parser that is checked against them.
Home layer (docs/LAYERS.md); split out of command_whitelist.py, which keeps the platform's shell-command rules."""

import re
from typing import Callable, Dict, Any, List, Tuple, Optional

from lodestone.system_profile import load_cfg

# ==============================================================================
# 1. HOME ASSISTANT WHITELIST & GUARDRAILS
# ==============================================================================

# Allowed Home Assistant service domains and services
HA_ALLOWED_SERVICES: Dict[str, List[str]] = {
    "climate": ["set_temperature", "set_hvac_mode", "set_preset_mode"],
    "light": ["turn_on", "turn_off", "toggle"],
    "switch": ["turn_on", "turn_off", "toggle"],
    "automation": ["trigger", "turn_on", "turn_off"],
    "scene": ["turn_on"],
    "homeassistant": ["turn_on", "turn_off", "toggle"],
    "select": ["select_option"],
    "button": ["press"],
}

# What this home calls things is configuration, not code (config.json):
#   "ha_policy": {"thermostat": "climate.hallway",                          the default thermostat for "set it to 70"
#                 "chat_lights": {"porch": "light.porch", "back": "light.back_yard"}}   words in an order -> light
# Without them the thermostat is the one climate entity Home Assistant has (if exactly one), and a light is the one
# whose entity id shares the most words with the order.
_entity_lister: Optional[Callable[[str], List[str]]] = None     # domain -> entity ids, bound by the Home app


def bind(entity_lister: Callable[[str], List[str]]) -> None:
    global _entity_lister
    _entity_lister = entity_lister


def _settings() -> Dict[str, Any]:
    return load_cfg().get("ha_policy") or {}


def _entities(domain: str) -> List[str]:
    try:
        return list(_entity_lister(domain)) if _entity_lister else []
    except Exception:
        return []


def default_thermostat() -> str:
    configured = str(_settings().get("thermostat") or "").strip().lower()
    if configured:
        return configured
    found = _entities("climate")
    return found[0] if len(found) == 1 else ""


_GENERIC_WORDS = {"the", "light", "lights", "lamp", "floodlight", "timed", "turn", "switch", "on", "off", "in", "my"}


def light_for(target: str) -> str:
    """The light entity an order like 'the back yard floodlight' means, or ''."""
    target = (target or "").lower()
    for word, entity in (_settings().get("chat_lights") or {}).items():
        if str(word).lower() in target:
            return str(entity)
    words = {w for w in re.findall(r"[a-z0-9]+", target) if w not in _GENERIC_WORDS and len(w) > 2}
    best, best_score, tie = "", 0, False
    for entity in _entities("light"):
        score = len(words & set(entity.split(".", 1)[-1].split("_")))
        if score > best_score:
            best, best_score, tie = entity, score, False
        elif score == best_score and score:
            tie = True
    return "" if tie else best


# Blocked entity substrings (destructive actions, security credentials, reboots)
HA_BLOCKED_ENTITY_SUBSTRINGS = [
    "format_sd",
    "reboot",
    "factory_reset",
    "firmware",
    "password",
    "security_status",
    "token"
]

# Entities this route never touches, whatever the service (unlock / open / disarm go through Computer's approval step):
SENSITIVE_ENTITY_DOMAINS = ("lock", "cover", "alarm_control_panel", "valve", "siren", "camera")
SENSITIVE_ENTITY_WORDS = ("garage", "gate_opener", "deadbolt", "door_lock")

# Safe temperature range in Fahrenheit (protects HVAC equipment and home)
MIN_SAFE_TEMP_F = 60.0
MAX_SAFE_TEMP_F = 85.0


def validate_ha_action(domain: str, service: str, service_data: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Validates a Home Assistant service call against strict domain, service, entity,
    and parameter safety whitelists.
    Returns: (is_allowed, reason_or_message, sanitized_service_data)
    """
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    data = dict(service_data or {})

    # 1. Domain & Service Whitelist Check
    if domain not in HA_ALLOWED_SERVICES:
        return False, f"Domain '{domain}' is not in the allowed Home Assistant whitelist.", data

    if service not in HA_ALLOWED_SERVICES[domain]:
        return False, f"Service '{service}' is not permitted for domain '{domain}'. Allowed: {HA_ALLOWED_SERVICES[domain]}", data

    # 2. Entity Whitelist & Sanitization Check
    raw_ids = data.get("entity_id", "")
    if isinstance(raw_ids, str):                      # HA accepts "a, b" as well as a list
        raw_ids = [p for p in raw_ids.split(",")]
    elif not isinstance(raw_ids, (list, tuple)):
        raw_ids = [raw_ids]
    ids = [str(e).strip().lower() for e in raw_ids if str(e).strip()]
    entity_id = ids[0] if ids else ""

    for eid in ids:                                   # every entity named, not only the first
        for blocked in HA_BLOCKED_ENTITY_SUBSTRINGS:
            if blocked in eid:
                return False, f"Entity '{eid}' contains blocked substring '{blocked}'.", data
        if eid.split(".", 1)[0] in SENSITIVE_ENTITY_DOMAINS or any(w in eid for w in SENSITIVE_ENTITY_WORDS):
            return False, f"Entity '{eid}' opens or unlocks something; that needs Computer's approval step, not this route.", data
    if domain == "homeassistant" and (not ids or "all" in ids or any(k in data for k in ("area_id", "device_id", "label_id", "floor_id"))):
        # homeassistant.turn_on fans out to whatever the target covers (locks and garage doors included)
        return False, "homeassistant services need explicit entity_id values.", data

    # 3. Domain-Specific Parameter Validation
    if domain == "climate" and service == "set_temperature":
        target = data.get("temperature")
        if target is None:
            # Check target_temp_high/low if present
            target = data.get("target_temp_high") or data.get("target_temp_low")
        if target is None:
            return False, "set_temperature requires a 'temperature' parameter.", data

        try:
            temp_f = float(target)
        except (ValueError, TypeError):
            return False, f"Invalid temperature value: '{target}'.", data

        if temp_f < MIN_SAFE_TEMP_F or temp_f > MAX_SAFE_TEMP_F:
            return False, (f"Requested temperature {temp_f}°F is outside the safe residential whitelist bounds "
                           f"({MIN_SAFE_TEMP_F}°F to {MAX_SAFE_TEMP_F}°F)."), data

        # Default entity: the home's thermostat (config ha_policy.thermostat, else its only climate entity) if omitted
        if not entity_id:
            entity_id = default_thermostat()
            if not entity_id:
                return False, "set_temperature needs an entity_id (config.json ha_policy.thermostat names the default).", data
            data["entity_id"] = entity_id

        data["temperature"] = round(temp_f, 1)

    return True, "Action approved by Home Assistant whitelist.", data


# ==============================================================================
# 3. CHAT INTENT PARSER & ACTION EXTRACTOR
# ==============================================================================

# Regex patterns for natural language Home Assistant commands
CLIMATE_TEMP_PATTERNS = [
    r"(?:set|change|turn|make|adjust|cool|heat|lower|raise)\s+(?:the\s+)?(?:thermostat|temp|temperature|ac|heat|house|climate)?\s*(?:to\s+)?(\d{2}(?:\.\d)?)\s*(?:degrees|deg|°|f)?\b",
    r"(\d{2}(?:\.\d)?)\s*(?:degrees|deg|°|f)?\s*(?:on\s+the\s+thermostat|for\s+the\s+temp)",
]

LIGHT_PATTERNS = [
    (r"(?:turn|switch)\s+on\s+(?:the\s+)?(.*?(?:light|floodlight|lamp).*)", "turn_on"),
    (r"(?:turn|switch)\s+off\s+(?:the\s+)?(.*?(?:light|floodlight|lamp).*)", "turn_off"),
    (r"(?:turn|switch)\s+(.*?(?:light|floodlight|lamp).*)\s+on", "turn_on"),
    (r"(?:turn|switch)\s+(.*?(?:light|floodlight|lamp).*)\s+off", "turn_off"),
]


def extract_whitelisted_chat_action(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Parses conversational user prompt for actionable smart home intents,
    validates against the whitelist, and returns an executable action dictionary.
    Returns None if no actionable or whitelisted intent is present.
    """
    text = (user_text or "").strip().lower()

    # 1. Climate / Thermostat Target Temperature
    thermostat = default_thermostat()
    for pattern in CLIMATE_TEMP_PATTERNS if thermostat else []:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            temp_str = match.group(1)
            try:
                temp_val = float(temp_str)
                # Verify safe temperature bounds
                is_valid, msg, s_data = validate_ha_action(
                    domain="climate",
                    service="set_temperature",
                    service_data={"entity_id": thermostat, "temperature": temp_val}
                )
                if is_valid:
                    return {
                        "category": "home_assistant",
                        "domain": "climate",
                        "service": "set_temperature",
                        "service_data": s_data,
                        "description": f"Set the thermostat to {temp_val}°F"
                    }
            except Exception:
                pass

    # 2. Lighting Controls
    for pattern, srv in LIGHT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            target_str = match.group(1).strip()
            # Match against the home's lights (config ha_policy.chat_lights, else the entity ids Home Assistant has)
            entity_id = light_for(target_str)

            if entity_id:
                is_valid, msg, s_data = validate_ha_action(
                    domain="light",
                    service=srv,
                    service_data={"entity_id": entity_id}
                )
                if is_valid:
                    return {
                        "category": "home_assistant",
                        "domain": "light",
                        "service": srv,
                        "service_data": s_data,
                        "description": f"{srv.replace('_', ' ').title()} {entity_id}"
                    }

    return None
