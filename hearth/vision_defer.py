"""
Vision during garden time -- the house's side (2026-10-14; docs/garden/DESIGN.md "Vision during garden time").

While the garden holds the RX 6600 the vision server is lent out. Background callers of the vision model do not fail and do not wait:

  patrol sweeps    Frigate's own detections (face / pet identities) name who is in view straight away; the frame goes to the queue and
                   the late read adds what Frigate could not say (Patrol.deliver_late: a sighting dated when the frame was taken)
  commentary       the read cannot happen now; the frame is queued, and a late read is traced (never spoken; skipped when stale)
  sighting boxes   a card's box needs the model; the frame is queued and the box lands in the box cache for the next view
  wildlife sentry  on the engine host, it hands its frame to StoneSage's queue (POST /api/vision-queue/frame, a scoped key) instead of
                   logging an error, and collects the late results later (GET /api/vision-queue/results + POST .../ack)

Person-started vision never queues: a camera question and the PTZ "Sweep now" hot-load the engine instead (stonesage/courage/takeback.py).

This module holds the decisions as small functions over a queue, so they can be tested with fakes. The queue itself is the platform's
(stonesage/vision_queue.py); this layer decides what a frame, a prompt and a callback key are for each source.
"""

import base64
import binascii
from typing import Any, Callable, Dict, List, Optional

SOURCES = ("patrol", "commentary", "box", "sentry")
SENTRY_SCOPE = "vision-queue"                    # the principal of the sentry's scoped key (stonesage/auth.py SCOPED_KEYS)
SENTRY_ROUTES = [("POST", "/api/vision-queue/frame"), ("GET", "/api/vision-queue/results"), ("POST", "/api/vision-queue/ack")]
MAX_PROMPT = 6000


class Deferrer:
    def __init__(self, get_queue: Callable[[], Any], on_loan: Callable[[str], Optional[float]]):
        self.get_queue, self.on_loan = get_queue, on_loan

    def lent(self) -> bool:
        """The vision engine is lent out right now (an engine lease holds it)."""
        return bool(self.on_loan("vision"))

    # ---- patrol ----
    def patrol(self, frame: bytes, prompt: str, ctx: Dict[str, Any], max_tokens: int = 80) -> Dict[str, Any]:
        """Queue a patrol frame; the answer tells Patrol._look to use Frigate's names for now."""
        res = self.get_queue().enqueue("patrol", ctx.get("camera") or ctx.get("entity") or "", "sweep_frame", frame, prompt,
                                       max_tokens=max_tokens, temperature=0.1,
                                       key="%s|%s" % (ctx.get("entity"), ctx.get("pan")),
                                       meta={"entity": ctx.get("entity"), "pan": ctx.get("pan"), "profiles": ctx.get("profiles") or []})
        return {"seen": [], "skipped": "vision lent out (engine lease); frame queued", "deferred": res.get("id") or bool(res.get("ok"))}

    # ---- commentary ----
    def commentary(self, jpeg: bytes, prompt: str, ctx: Dict[str, Any]) -> None:
        """Queue a commentary read, then raise: the caller has no answer now (it logs the reason as before)."""
        res = self.get_queue().enqueue("commentary", ctx.get("camera") or "", "activity_read", jpeg, prompt, max_tokens=60, temperature=0.1,
                                       meta={"room": ctx.get("room"), "who": ctx.get("who") or []})
        raise RuntimeError("vision lent out (engine lease); frame %s" % ("queued" if res.get("ok") else "not queued"))

    # ---- sighting boxes ----
    def box(self, key: str, jpeg: bytes, prompt: str, size: List[int]) -> bool:
        """Queue a box lookup once per card key. `jpeg` is the picture as it will be asked about (640 px wide); size = [w, h]."""
        res = self.get_queue().enqueue("box", "", "box", jpeg, prompt, max_tokens=120, temperature=0.0, key=key,
                                       meta={"size": size}, dedupe=True)
        return bool(res.get("ok"))


def register_handlers(queue: Any, get_patrol: Callable[[], Any], get_commentary: Callable[[], Any],
                      box_put: Callable[[str, Optional[List[float]]], None], parse_box: Callable[[str, int, int], Optional[List[float]]]) -> None:
    """The in-process sources take their late results from the queue. (The sentry pulls its own: see sentry_results.)"""
    queue.register_handler("patrol", lambda item, text, frame: get_patrol().deliver_late(item, text, frame))
    queue.register_handler("commentary", lambda item, text, frame: get_commentary().deliver_late(item, text, frame))

    def on_box(item: Dict[str, Any], text: str, frame: bytes) -> None:
        w, h = (item.get("meta") or {}).get("size") or [0, 0]
        box_put(item["key"], parse_box(text, int(w), int(h)) if w and h else None)

    queue.register_handler("box", on_box)


# ---- the wildlife sentry's side of the queue (HTTP, scoped key) ----
def accept_sentry_frame(queue: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/vision-queue/frame from the sentry: {camera, prompt, frame_b64, at?, max_tokens?, key?}. The sentry builds its own prompt
    and parses the answer itself, so StoneSage carries no sentry prompt text."""
    camera = str(body.get("camera") or "")[:80]
    prompt = str(body.get("prompt") or "")
    if not camera or not prompt or len(prompt) > MAX_PROMPT:
        return {"ok": False, "error": "camera and a prompt (<= %d chars) are required" % MAX_PROMPT, "status": 400}
    try:
        frame = base64.b64decode(str(body.get("frame_b64") or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "frame_b64 is not base64", "status": 400}
    if not frame.startswith(b"\xff\xd8"):
        return {"ok": False, "error": "the frame must be a JPEG", "status": 400}
    try:
        at = float(body.get("at")) if body.get("at") else None
        tokens = max(16, min(int(body.get("max_tokens") or 180), 400))
    except (TypeError, ValueError):
        return {"ok": False, "error": "at / max_tokens are numbers", "status": 400}
    res = queue.enqueue("sentry", camera, "sentry_frame", frame, prompt, max_tokens=tokens, temperature=0.1,
                        key=str(body.get("key") or "")[:120], at=at)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "not queued", "status": 503}
    return {"ok": True, **res}
