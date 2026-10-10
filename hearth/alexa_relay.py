"""Alexa -> Computer without a public endpoint: the skill's Lambda and this house meet at a relay (ntfy.sh topics).

Why: an Echo can only hand words to code through an Alexa custom skill, and the skill's endpoint must be reachable from Amazon.
This house is behind CGNAT, so the skill (hosted free by Amazon, deploy/alexa-skill/) posts each request to a relay topic and
this module, which only ever makes OUTBOUND requests, reads it, runs Computer, and posts the answer to a second topic that the
skill is waiting on.

The relay is a public service with public topic names, so everything on it is sealed with one shared key:
  - topic names, the cipher key and the MAC key are all derived from that key (HMAC-SHA256), so the skill needs only the one value;
  - a message is base64url(nonce16 | ciphertext | tag32); ciphertext = plaintext XOR an HMAC-SHA256 counter keystream, tag =
    HMAC-SHA256(mac_key, label NUL nonce ciphertext) with a label per direction ("req" / "res") so a request can never be replayed
    as an answer. Standard library only (no AES here), and node's `crypto` does the same on the Lambda side.
  - a request must be fresh (+-120 s) and its id unseen; a message without the key is dropped and never answered.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("alexa_relay")

VERSION = 1
FRESH_S = 120          # a request older (or newer) than this is rejected: replay window
SESSION_TTL_S = 600    # an Alexa conversation is forgotten 10 min after its last turn
HISTORY_MAX = 8        # messages kept per Alexa session
MAX_SAY_CHARS = 1200   # what is sent back (the relay refuses messages over 4 KB; Alexa speaks long text slowly anyway)
SKEW_MARGIN_S = 1.5    # time Alexa still needs after an answer is posted (return trip + speech start)


def _mac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def derive(key: str) -> Dict[str, Any]:
    """Everything that follows from the shared key: cipher key, MAC key, and the two (secret) topic names."""
    k = key.encode("utf-8")
    return {"enc": _mac(k, b"ss-relay enc"), "mac": _mac(k, b"ss-relay mac"),
            "req": "ssr-" + _mac(k, b"ss-relay topic req").hex()[:32],
            "res": "ssr-" + _mac(k, b"ss-relay topic res").hex()[:32]}


def _keystream(enc: bytes, nonce: bytes, n: int) -> bytes:
    out, i = b"", 0
    while len(out) < n:
        out += _mac(enc, nonce + i.to_bytes(4, "big"))
        i += 1
    return out[:n]


def seal(keys: Dict[str, Any], label: str, obj: Dict[str, Any]) -> str:
    pt = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(pt, _keystream(keys["enc"], nonce, len(pt))))
    tag = _mac(keys["mac"], label.encode() + b"\0" + nonce + ct)
    return base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("=")


def open_sealed(keys: Dict[str, Any], label: str, text: str) -> Dict[str, Any]:
    """The message's JSON, or ValueError for anything not sealed with our key for this direction."""
    try:
        raw = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
    except Exception:
        raise ValueError("not base64")
    if len(raw) < 16 + 2 + 32:
        raise ValueError("too short")
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, _mac(keys["mac"], label.encode() + b"\0" + nonce + ct)):
        raise ValueError("bad tag")
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(keys["enc"], nonce, len(ct))))
    obj = json.loads(pt.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("not an object")
    return obj


class AlexaRelay:
    """Listens on the request topic, answers on the response topic.

    run_turn(history, session_id) -> (text, more): Computer's answer, and whether it is waiting for a yes/no (the Alexa session
    then stays open). announce(text, room) speaks a late answer on an Echo (Alexa gives a skill ~8 s)."""

    def __init__(self, key: str, run_turn: Callable[[List[Dict[str, str]], str], Tuple[str, bool]],
                 announce: Optional[Callable[[str, str], Any]] = None, base_url: str = "https://ntfy.sh",
                 allowed_users: Optional[List[str]] = None, default_room: str = "kitchen", max_per_minute: int = 20,
                 clock: Callable[[], float] = time.time, opener: Callable[..., Any] = urllib.request.urlopen):
        if not key or len(key) < 32:
            raise ValueError("alexa_relay.key must be a long random string (>= 32 characters)")
        self.keys = derive(key)
        self.run_turn, self.announce = run_turn, announce
        self.base_url = base_url.rstrip("/")
        self.allowed_users = set(allowed_users or [])
        self.default_room, self.max_per_minute = default_room, max_per_minute
        self.clock, self._open = clock, opener
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._recent: List[float] = []
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._since = str(int(clock()))                       # never replay what was said before we started
        self.stats = {"requests": 0, "answered": 0, "announced": 0, "rejected": 0, "late": 0, "errors": 0}
        self.connected, self.last_error, self.last_request_at = False, "", 0.0
        self.users_seen: Dict[str, int] = {}                  # hashed Alexa user ids (to pin allowed_users)
        self._thread: Optional[threading.Thread] = None

    # ---- listening ---------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="alexa-relay")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        backoff = 1.0
        logger.info("Alexa relay: listening for requests (outbound only)")
        while not self._stop.is_set():
            try:
                self.listen_once()
                backoff = 1.0
            except Exception as e:
                self.connected, self.last_error = False, f"{type(e).__name__}: {e}"[:200]
                logger.warning("Alexa relay: %s (retry in %.0fs)", self.last_error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def listen_once(self) -> None:
        """One streaming connection to the request topic; returns when the relay closes it."""
        url = f"{self.base_url}/{self.keys['req']}/json?since={self._since}"
        with self._open(urllib.request.Request(url), timeout=90) as resp:       # ntfy sends a keepalive every ~45 s
            self.connected = True
            for raw in resp:
                if self._stop.is_set():
                    return
                try:
                    ev = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if ev.get("id"):
                    self._since = ev["id"]
                if ev.get("event") == "message" and ev.get("message"):
                    threading.Thread(target=self.process, args=(ev["message"],), daemon=True, name="alexa-turn").start()
        self.connected = False

    # ---- one request -------------------------------------------------------------------------------
    def process(self, text: str) -> Optional[str]:
        """Handle one sealed request: returns what was done ('answered' | 'announced' | 'rejected:<why>'), for tests."""
        t0 = self.clock()
        try:
            req = open_sealed(self.keys, "req", text)
        except ValueError:
            return self._reject("not ours")
        rid, words = str(req.get("id") or ""), str(req.get("text") or "").strip()
        if req.get("v") != VERSION or not rid or not words:
            return self._reject("malformed")
        if abs(t0 - float(req.get("ts") or 0)) > FRESH_S:
            return self._reject("stale")
        user = str(req.get("uid") or "")
        if user:
            self.users_seen[user] = self.users_seen.get(user, 0) + 1
        if self.allowed_users and user not in self.allowed_users:
            return self._reject("user not allowed")
        with self._lock:
            if rid in self._seen:
                return self._reject("replay")
            self._seen[rid] = t0
            while len(self._seen) > 256:
                self._seen.popitem(last=False)
            self._recent = [x for x in self._recent if t0 - x < 60]
            if len(self._recent) >= self.max_per_minute:
                return self._reject("rate limit")
            self._recent.append(t0)
        self.stats["requests"] += 1
        self.last_request_at = t0
        say, more = self._turn(str(req.get("sid") or rid), words)
        say = (say or "").strip()[:MAX_SAY_CHARS] or "I have nothing to say to that."
        deadline = float(req.get("dl") or 6.5)
        if self.clock() - t0 > deadline - SKEW_MARGIN_S:          # Alexa has given up waiting: say it on the Echo instead
            self.stats["late"] += 1
            return self._announce(say, req)
        try:
            self._post(self.keys["res"], seal(self.keys, "res", {"v": VERSION, "re": rid, "ts": int(self.clock()),
                                                                   "say": say, "more": bool(more)}))
            self.stats["answered"] += 1
            return "answered"
        except Exception as e:
            self.last_error = f"publish failed: {e}"[:200]
            self.stats["errors"] += 1
            return self._announce(say, req)

    def _turn(self, sid: str, words: str) -> Tuple[str, bool]:
        session_id = "alexa:" + hashlib.sha1(sid.encode()).hexdigest()[:12]
        now = self.clock()
        with self._lock:
            for k in [k for k, v in self._sessions.items() if now - v["at"] > SESSION_TTL_S]:
                del self._sessions[k]
            sess = self._sessions.setdefault(session_id, {"history": [], "at": now})
            sess["at"] = now
            sess["history"].append({"role": "user", "content": words})
            history = list(sess["history"])
        try:
            say, more = self.run_turn(history, session_id)
        except Exception as e:
            logger.warning("Alexa relay: the turn failed: %s", e)
            self.stats["errors"] += 1
            say, more = "Something broke on my end. Try again in a moment.", False
        with self._lock:
            sess["history"].append({"role": "assistant", "content": say})
            del sess["history"][:-HISTORY_MAX]
        return say, more

    def _announce(self, say: str, req: Dict[str, Any]) -> str:
        if self.announce:
            try:
                self.announce(say, self.default_room)
                self.stats["announced"] += 1
                return "announced"
            except Exception as e:
                self.last_error = f"announce failed: {e}"[:200]
                self.stats["errors"] += 1
        return "lost"

    def _reject(self, why: str) -> str:
        self.stats["rejected"] += 1
        logger.debug("Alexa relay: dropped a message (%s)", why)
        return "rejected:" + why

    def _post(self, topic: str, body: str) -> None:
        req = urllib.request.Request(f"{self.base_url}/{topic}", data=body.encode("ascii"), method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self._open(req, timeout=10) as resp:
            if getattr(resp, "status", 200) >= 300:
                raise RuntimeError(f"relay answered {resp.status}")

    def status(self) -> Dict[str, Any]:
        return {"enabled": True, "connected": self.connected, "relay": self.base_url, "stats": dict(self.stats),
                "last_request_ago_s": round(self.clock() - self.last_request_at) if self.last_request_at else None,
                "last_error": self.last_error, "users_seen": dict(self.users_seen),
                "user_filter": bool(self.allowed_users), "sessions": len(self._sessions)}
