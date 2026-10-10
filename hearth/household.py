"""The household Computer serves, read from config.json (docs/LAYERS.md M2: no names, cameras or phones in code).

    "household": {
      "people": ["Alex", "Sam"],                      first = the owner, who approves actions
      "owner_pronouns": ["she", "her"],               optional, default they/them
      "pets": {"Bella": "cat"},                       optional; default from frigate.identities (species -> name)
      "phones": {"alex": "mobile_app_alex_phone"},    person key -> HA notify service (push, approvals)
      "speakers": {"kitchen": "alexa_media_kitchen", "everywhere": "alexa_media_everywhere"},   room -> HA notify service
      "cameras": {"kitchen": {"entity": "camera.kitchen", "name": "Kitchen"}},   optional; default from camera_ui
      "descriptions": {"Alex": "dark hair, male", "Bella": "grey tabby cat"},   optional: helps vision name them
      "ptz_cameras": ["kitchen"], "default_camera": "kitchen"                     optional; default from camera_ui
    }
    "infra_words": ["myserver", "rack"]               top level, optional: names of the site's own machines; a switch whose entity
                                                      id contains one is never turned off (added to server / nas / router)

Without the block, a home still gets its cameras (camera_ui) and pets (frigate.identities); nobody is named, so presence
lists no one and approvals fall back to "the owner".
"""

import re
from typing import Any, Dict

from stonesage.courage.household import Household

_STREAM_SUFFIX = re.compile(r"_(hd_)?stream(_direct)?$")


def camera_key(entity_id: str) -> str:
    """'camera.driveway_front_door_hd_stream_direct' -> 'driveway_front_door' (the key tools and patrol use)."""
    return _STREAM_SUFFIX.sub("", entity_id.split(".", 1)[-1])


def _cameras_from_ui(cfg: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    cams = {}
    for entity, ui in (cfg.get("camera_ui") or {}).items():
        if entity.startswith("camera.") and isinstance(ui, dict):
            cams[camera_key(entity)] = {"entity": entity, "name": ui.get("name") or entity}
    return cams


def household_from_config(cfg: Dict[str, Any]) -> Household:
    block = cfg.get("household") or {}
    cameras = block.get("cameras") or _cameras_from_ui(cfg)
    ptz = block.get("ptz_cameras")
    if ptz is None:
        ptz = [ui["ptz"] for ui in (cfg.get("camera_ui") or {}).values() if isinstance(ui, dict) and ui.get("ptz") in cameras]
    pets = block.get("pets")
    if pets is None:
        pets = {name.title(): species for species, name in ((cfg.get("frigate") or {}).get("identities") or {}).items()}
    pronouns = tuple(block.get("owner_pronouns") or ())
    if len(pronouns) != 2:              # subject, object
        pronouns = ("they", "them")
    return Household(
        people=list(block.get("people") or []),
        owner_pronouns=pronouns,
        pets=dict(pets),
        cameras=dict(cameras),
        ptz_cameras=list(ptz),
        default_camera=block.get("default_camera") or next(iter(cameras), ""),
        phones=list(block.get("phones") or {}),
        speakers=list(block.get("speakers") or {}),
        descriptions=dict(block.get("descriptions") or {}),
        infra_words=list(cfg.get("infra_words") or block.get("infra_words") or []),
    )


def phone_services(cfg: Dict[str, Any]) -> Dict[str, str]:
    """person key -> HA notify service, e.g. {"alex": "mobile_app_alex_phone"}."""
    return dict((cfg.get("household") or {}).get("phones") or {})


def speaker_services(cfg: Dict[str, Any]) -> Dict[str, str]:
    """room -> HA notify service for announcements; "everywhere" = every speaker."""
    return dict((cfg.get("household") or {}).get("speakers") or {})
