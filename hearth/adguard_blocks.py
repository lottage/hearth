"""One-touch "Block TikTok" for the dashboard: a Home Assistant input_boolean drives AdGuard Home's built-in blocked service.

Why this shape: HA's own AdGuard integration cannot toggle blocked services, and editing AdGuard's yaml means restarting DNS
for the whole house. AdGuard's API can do it live, but it needs a login, so the login lives here (env vars from
/etc/stonesage/secrets.env on this host: ADGUARD_USER / ADGUARD_PASSWORD) and HA never holds it.

Flow: tap the tile -> input_boolean.block_tiktok changes -> this listener (HA websocket, outbound only) applies it to AdGuard
-> verifies it by reading AdGuard back. If AdGuard refuses or cannot be read, the switch is put back so the tile never lies.
Reality wins on drift: every RECONCILE_S the switch is set to whatever AdGuard really has (unless we just applied something).

Limits worth knowing: AdGuard sees every query as coming from the router, so this blocks the whole house, and anything that
does not use AdGuard for DNS (cellular, private DNS / DoH, the IPv6 DNS servers the router advertises) is not affected.
"""

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("adguard_blocks")

RECONCILE_S = 600
RECENT_APPLY_S = 60
OWN_ECHO_S = 10


def plan(current_ids: List[str], service: str, want_blocked: bool) -> Tuple[List[str], bool]:
    """The ids AdGuard should hold after this change, and whether anything must be sent. Other services are kept as they
    are and order is preserved; asking for what is already true sends nothing."""
    ids = [i for i in current_ids if isinstance(i, str)]
    if want_blocked and service not in ids:
        return ids + [service], True
    if not want_blocked and service in ids:
        return [i for i in ids if i != service], True
    return ids, False


class AdGuardError(Exception):
    pass


def _default_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


class AdGuardClient:
    """The few AdGuard Home calls needed. `http(method, url, headers, body, timeout) -> (status, bytes)` is injectable for tests."""

    def __init__(self, base_url: str, user: str, password: str, http: Callable = _default_http, timeout: float = 8.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._http = http
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json", "Accept": "application/json"}

    def _call(self, method: str, path: str, payload=None) -> Tuple[int, bytes]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            return self._http(method, self.base + path, self._headers, body, self.timeout)
        except Exception as e:
            raise AdGuardError(f"AdGuard not reachable: {e}")

    def get_blocked(self) -> Dict:
        status, raw = self._call("GET", "/control/blocked_services/get")
        if status in (401, 403):
            raise AdGuardError("AdGuard rejected the login (check ADGUARD_USER / ADGUARD_PASSWORD)")
        if status != 200:
            raise AdGuardError(f"blocked_services/get returned HTTP {status}")
        data = json.loads(raw.decode("utf-8") or "{}")
        # Older builds answered with a bare list of ids; newer ones with {"ids": [...], "schedule": {...}}.
        if isinstance(data, list):
            return {"ids": data, "schedule": None}
        return {"ids": data.get("ids") or [], "schedule": data.get("schedule")}

    def known_service(self, service: str) -> bool:
        status, raw = self._call("GET", "/control/blocked_services/all")
        if status != 200:
            return True  # cannot check: let the update decide
        data = json.loads(raw.decode("utf-8") or "{}")
        services = data.get("blocked_services") if isinstance(data, dict) else data
        return any((s or {}).get("id") == service for s in (services or []))

    def set_blocked(self, ids: List[str], schedule) -> None:
        payload = {"ids": ids}
        if schedule:
            payload["schedule"] = schedule
        status, _ = self._call("PUT", "/control/blocked_services/update", payload)
        if status in (404, 405):  # pre-0.107.37 API
            status, _ = self._call("POST", "/control/blocked_services/set", ids)
        if status in (401, 403):
            raise AdGuardError("AdGuard rejected the login")
        if status != 200:
            raise AdGuardError(f"blocked_services update returned HTTP {status}")


class AdGuardBlocks:
    def __init__(self, ha_url: str, ha_token: str, client: Optional[AdGuardClient], entity: str = "input_boolean.block_tiktok",
                 service: str = "tiktok", set_boolean: Optional[Callable[[bool], None]] = None, clock: Callable[[], float] = time.time):
        self.ha_url, self.token, self.client = ha_url.rstrip("/"), ha_token, client
        self.entity, self.service = entity, service
        self._set_boolean = set_boolean
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.last: Dict = {"at": None, "wanted": None, "result": "not run yet"}
        self._applied_at = 0.0
        self._own: Optional[Tuple[bool, float]] = None

    # ---- the decision logic (no network of its own besides the injected client) ----
    def apply(self, want_blocked: bool) -> str:
        """Make AdGuard match `want_blocked` and verify it. Returns a short outcome; on failure puts the HA switch back."""
        if self.client is None:
            return self._record(want_blocked, "no AdGuard credentials configured", revert=True)
        with self._lock:
            try:
                current = self.client.get_blocked()
                if want_blocked and not self.client.known_service(self.service):
                    return self._record(want_blocked, f"AdGuard has no blocked service called '{self.service}'", revert=True)
                ids, change = plan(current["ids"], self.service, want_blocked)
                if change:
                    self.client.set_blocked(ids, current["schedule"])
                    self._applied_at = self._clock()
                now_blocked = self.service in self.client.get_blocked()["ids"]
                if now_blocked != want_blocked:
                    return self._record(want_blocked, "AdGuard did not keep the change", revert=True)
                return self._record(want_blocked, "applied" if change else "already in that state")
            except AdGuardError as e:
                return self._record(want_blocked, str(e), revert=True)
            except Exception as e:  # never let one bad reply kill the listener thread
                return self._record(want_blocked, f"unexpected: {e}", revert=True)

    def reconcile(self) -> Optional[bool]:
        """Reality wins: returns AdGuard's real state (and tells HA) unless we applied something moments ago or cannot read it."""
        if self.client is None or self._clock() - self._applied_at < RECENT_APPLY_S:
            return None
        try:
            real = self.service in self.client.get_blocked()["ids"]
        except Exception as e:
            self.last = {"at": self._clock(), "wanted": None, "result": f"reconcile could not read AdGuard: {e}"}
            return None
        self._put(real)
        return real

    def _put(self, on: bool) -> None:
        """Set the HA switch, remembering that the resulting state_changed event is ours (see own_echo)."""
        if not self._set_boolean:
            return
        self._own = (on, self._clock())
        self._set_boolean(on)

    def own_echo(self, on: bool) -> bool:
        """True for the state_changed event that our own _put caused: it must not be applied again (a failed apply puts the
        switch back, and without this a down AdGuard would loop forever)."""
        return self._own is not None and self._own[0] == on and self._clock() - self._own[1] < OWN_ECHO_S

    def _record(self, wanted: bool, result: str, revert: bool = False) -> str:
        self.last = {"at": self._clock(), "wanted": wanted, "result": result}
        if result in ("applied", "already in that state"):
            logger.info(f"TikTok block {'on' if wanted else 'off'}: {result}")
        else:
            logger.warning(f"TikTok block {'on' if wanted else 'off'} failed: {result}")
        if revert:
            try:
                # Without a login nothing can be blocked, so "off" is the only honest state; otherwise undo the tap.
                self._put(False if self.client is None else not wanted)
            except Exception as e:
                logger.warning(f"could not put the HA switch back: {e}")
        return result

    def status(self) -> Dict:
        return {"enabled": True, "entity": self.entity, "service": self.service, "credentials": self.client is not None,
                "listener_running": bool(self._thread and self._thread.is_alive()), "last": self.last}

    # ---- the Home Assistant side ----
    async def _listen(self) -> None:
        import websockets
        url = self.ha_url.replace("http", "ws", 1) + "/api/websocket"
        backoff = 5
        loop = asyncio.get_running_loop()
        while True:
            try:
                async with websockets.connect(url, max_size=None, ping_interval=30) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                    if json.loads(await ws.recv()).get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant rejected the token")
                    await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
                    backoff = 5
                    logger.info(f"listening for {self.entity} (Block TikTok)")
                    await loop.run_in_executor(None, self.reconcile)
                    last_reconcile = time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.monotonic() - last_reconcile > RECONCILE_S:
                            last_reconcile = time.monotonic()
                            await loop.run_in_executor(None, self.reconcile)
                        if raw is None:
                            continue
                        msg = json.loads(raw)
                        data = (msg.get("event") or {}).get("data") or {}
                        if data.get("entity_id") != self.entity:
                            continue
                        new, old = data.get("new_state") or {}, data.get("old_state") or {}
                        if new.get("state") not in ("on", "off") or new.get("state") == old.get("state"):
                            continue
                        if self.own_echo(new["state"] == "on"):
                            continue
                        await loop.run_in_executor(None, self.apply, new["state"] == "on")
            except Exception as e:
                logger.warning(f"Block TikTok listener: {e}; retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=lambda: asyncio.run(self._listen()), name="adguard-blocks", daemon=True)
        self._thread.start()


def from_config(config: Dict, ha_set_boolean: Callable[[str, bool], None]) -> Optional[AdGuardBlocks]:
    """Build from config.json `adguard_blocks` + env credentials; None when the feature is off."""
    cfg = config.get("adguard_blocks") or {}
    if not cfg.get("enabled"):
        return None
    user = os.environ.get(cfg.get("user_env", "ADGUARD_USER"), "")
    password = os.environ.get(cfg.get("password_env", "ADGUARD_PASSWORD"), "")
    client = AdGuardClient(cfg["url"], user, password) if user and password and cfg.get("url") else None
    entity = cfg.get("entity", "input_boolean.block_tiktok")
    ha = config.get("homeassistant") or {}
    return AdGuardBlocks(ha.get("url", ""), ha.get("token", ""), client, entity=entity, service=cfg.get("service", "tiktok"),
                         set_boolean=lambda on: ha_set_boolean(entity, on))
