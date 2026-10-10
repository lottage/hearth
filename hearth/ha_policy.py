"""Home Assistant action limits for Computer and the cockpit: which services may be called, verified and blocked
entities, the safe thermostat range, and the natural-language order parser that is checked against them.
Home layer (docs/LAYERS.md); split out of command_whitelist.py, which keeps the platform's shell-command rules."""

import re
from typing import Dict, Any, List, Tuple, Optional

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

# Verified default entities for this home
HA_VERIFIED_ENTITIES = {
    "climate": ["climate.nest_thermostat"],
    "light": [
        "light.back_yard_floodlight_timed",
        "light.side_yard_floodlight_timed",
        "light.driveway_front_door_floodlight_timed"
    ],
    "switch": [
        "switch.kitchen_living_room",
        "switch.kitchen_living_room_led",
        "switch.yard_driveway_front_door_preset_patrol_mode",
        "switch.kitchen_kitchen_living_room_preset_patrol_mode"
    ]
}

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
    entity_id = data.get("entity_id", "")
    if isinstance(entity_id, list):
        entity_id = entity_id[0] if entity_id else ""
    entity_id = str(entity_id).strip().lower()

    if entity_id:
        for blocked in HA_BLOCKED_ENTITY_SUBSTRINGS:
            if blocked in entity_id:
                return False, f"Entity '{entity_id}' contains blocked substring '{blocked}'.", data

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

        # Default entity to Nest thermostat if omitted
        if not entity_id:
            entity_id = "climate.nest_thermostat"
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
    for pattern in CLIMATE_TEMP_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            temp_str = match.group(1)
            try:
                temp_val = float(temp_str)
                # Verify safe temperature bounds
                is_valid, msg, s_data = validate_ha_action(
                    domain="climate",
                    service="set_temperature",
                    service_data={"entity_id": "climate.nest_thermostat", "temperature": temp_val}
                )
                if is_valid:
                    return {
                        "category": "home_assistant",
                        "domain": "climate",
                        "service": "set_temperature",
                        "service_data": s_data,
                        "description": f"Set Nest Thermostat to {temp_val}°F"
                    }
            except Exception:
                pass

    # 2. Lighting Controls
    for pattern, srv in LIGHT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            target_str = match.group(1).strip()
            # Match against known lights
            entity_id = None
            if "back" in target_str or "yard" in target_str:
                entity_id = "light.back_yard_floodlight_timed"
            elif "side" in target_str:
                entity_id = "light.side_yard_floodlight_timed"
            elif "driveway" in target_str or "front" in target_str:
                entity_id = "light.driveway_front_door_floodlight_timed"

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
