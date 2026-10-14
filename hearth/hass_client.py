"""
Home Assistant REST Client for StoneSage
Queries entities, states, and telemetry from Home Assistant
and executes service calls (climate/Nest control, lights, switches, scenes).
"""

import json
import urllib.parse
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

STATES_TTL_S = 0.5    # see HomeAssistantClient.__init__: one voice order reads the state list several times in ~100 ms


class HomeAssistantClient:
    def __init__(self, base_url: str = "http://homeassistant.local:8123", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        # One /api/states read serves every get_states() within STATES_TTL_S: a single voice order asks for light, switch, fan
        # and validates the entity again, which was four full reads (~100 ms each) for one shortcut. Dropped on every
        # service call, so nothing we just changed is ever read from before the change.
        self._states_cache: Optional[tuple] = None

    def set_token(self, token: str):
        self.token = token.strip()

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def ping(self) -> Dict[str, Any]:
        """Verify connectivity to Home Assistant."""
        start = time.perf_counter()
        req = urllib.request.Request(f"{self.base_url}/api/", headers=self._get_headers())
        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                latency = round((time.perf_counter() - start) * 1000, 1)
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "online": True,
                    "latency_ms": latency,
                    "message": data.get("message", "API running"),
                    "token_valid": True
                }
        except urllib.error.HTTPError as e:
            latency = round((time.perf_counter() - start) * 1000, 1)
            if e.code == 401:
                return {"online": True, "latency_ms": latency, "token_valid": False, "error": "401 Unauthorized (Invalid or missing token)"}
            return {"online": False, "latency_ms": latency, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"online": False, "error": str(e)}

    def get_states(self, domain_filter: Optional[str] = None) -> Dict[str, Any]:
        """Fetch entity states, optionally filtered by domain (e.g. 'climate', 'light')."""
        if not self.token:
            return {"ok": False, "error": "Home Assistant Token not configured", "entities": []}

        cached = self._states_cache
        if cached and time.monotonic() - cached[0] < STATES_TTL_S:
            return self._filter_states(cached[1], domain_filter)
        req = urllib.request.Request(f"{self.base_url}/api/states", headers=self._get_headers())
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw_states = json.loads(resp.read().decode("utf-8"))
                self._states_cache = (time.monotonic(), raw_states)
                return self._filter_states(raw_states, domain_filter)
        except Exception as e:
            return {"ok": False, "error": str(e), "entities": []}

    @staticmethod
    def _filter_states(raw_states: List[Dict[str, Any]], domain_filter: Optional[str]) -> Dict[str, Any]:
        filtered = []
        for s in raw_states:
            entity_id = s.get("entity_id", "")
            if domain_filter and not entity_id.startswith(f"{domain_filter}."):
                continue
            filtered.append({
                "entity_id": entity_id,
                "state": s.get("state"),
                "friendly_name": s.get("attributes", {}).get("friendly_name", entity_id),
                "attributes": s.get("attributes", {}),
                "last_updated": s.get("last_updated")
            })
        return {"ok": True, "entities": filtered, "count": len(filtered)}

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch state and attributes for a specific entity."""
        if not self.token:
            return None
        req = urllib.request.Request(f"{self.base_url}/api/states/{entity_id}", headers=self._get_headers())
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None

    def set_state(self, entity_id: str, state: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Create or update an entity's state through the REST API (not persisted: gone after an HA restart)."""
        if not self.token:
            return {"ok": False, "error": "Home Assistant Token not configured"}
        req = urllib.request.Request(f"{self.base_url}/api/states/{entity_id}", method="POST",
                                     data=json.dumps({"state": state, "attributes": attributes}).encode("utf-8"),
                                     headers=self._get_headers())
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                resp.read()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_history(self, entity_ids: List[str], since: float) -> Dict[str, List[tuple]]:
        """State changes since `since` (epoch s) per entity: {entity_id: [(epoch, state), ...]} oldest first.
        Raises on HTTP errors (callers retry)."""
        if not self.token or not entity_ids:
            return {}
        start = urllib.parse.quote(datetime.fromtimestamp(since, timezone.utc).isoformat())
        url = (f"{self.base_url}/api/history/period/{start}?filter_entity_id={','.join(entity_ids)}"
               "&minimal_response&no_attributes")
        with urllib.request.urlopen(urllib.request.Request(url, headers=self._get_headers()), timeout=10) as resp:
            series = json.loads(resp.read().decode("utf-8"))
        out: Dict[str, List[tuple]] = {}
        for rows in series:
            if not rows:
                continue
            eid = rows[0].get("entity_id")            # minimal_response: only the first row names the entity
            pts = []
            for r in rows:
                try:
                    pts.append((datetime.fromisoformat(r["last_changed"].replace("Z", "+00:00")).timestamp(), r["state"]))
                except (KeyError, ValueError):
                    continue
            out[eid] = pts
        return out

    def get_camera_snapshot(self, entity_id: str) -> Optional[bytes]:
        """Fetch raw JPEG frame from Home Assistant camera proxy."""
        if not self.token:
            return None
        req = urllib.request.Request(f"{self.base_url}/api/camera_proxy/{entity_id}", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.read()
        except Exception:
            return None

    def call_service(self, domain: str, service: str, service_data: Dict[str, Any], timeout: float = 4) -> Dict[str, Any]:
        """Call a Home Assistant service (e.g. climate.set_temperature, light.turn_on). HA answers when the service
        has finished, so a slow device (a sleeping solar camera saving a preset) needs a longer timeout."""
        if not self.token:
            return {"ok": False, "error": "Home Assistant Token not configured"}

        self._states_cache = None            # whatever we are about to change must be read fresh next time
        url = f"{self.base_url}/api/services/{domain}/{service}"
        payload = json.dumps(service_data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._get_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return {"ok": True, "result": res}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_script_config(self, object_id: str) -> Optional[Dict[str, Any]]:
        """The stored config of a script made in Home Assistant's editor (alias, sequence...), or None."""
        if not self.token or not object_id or not all(c.isalnum() or c == "_" for c in object_id):
            return None
        req = urllib.request.Request(f"{self.base_url}/api/config/script/config/{object_id}", headers=self._get_headers())
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                cfg = json.loads(resp.read().decode("utf-8-sig"))
            return cfg if isinstance(cfg, dict) else None
        except Exception:
            return None

    def press_button(self, entity_id: str) -> Dict[str, Any]:
        """Press a Home Assistant button entity (e.g. camera PTZ move buttons)."""
        return self.call_service("button", "press", {"entity_id": entity_id})

    def select_option(self, entity_id: str, option: str, timeout: float = 4) -> Dict[str, Any]:
        """Select an option on a Home Assistant select entity (e.g. camera preset)."""
        return self.call_service("select", "select_option", {"entity_id": entity_id, "option": option}, timeout=timeout)

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Convenience method returning organized entities for the Sage & Stone dashboard."""
        all_res = self.get_states()
        if not all_res.get("ok"):
            return all_res

        entities = all_res.get("entities", [])
        climate = [e for e in entities if e["entity_id"].startswith("climate.")]
        lights = [e for e in entities if e["entity_id"].startswith("light.")]
        switches = [e for e in entities if e["entity_id"].startswith("switch.")]
        sensors = [e for e in entities if e["entity_id"].startswith("sensor.") and any(k in e["entity_id"] for k in ["temp", "humid", "power", "battery"])]

        return {
            "ok": True,
            "climate": climate,
            "lights": lights,
            "switches": switches,
            "sensors": sensors[:12]  # top 12 relevant sensors
        }
