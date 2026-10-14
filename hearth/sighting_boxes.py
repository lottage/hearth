"""
Where the subject of a Residents & Pets sighting is in its picture: a 2px box on the card (so the owner sees which of
several people or animals a label means), and a crop of a corrected person's frame before it goes into Frigate's
face library (so the right face is learned when two people are in view).

  frigate          the detector's own box from the event (data.box, fractions of the frame): no model call
  sentry, patrol   the vision model (Qwen2.5-VL grounding) is asked to locate that profile by its description; it
                   answers in pixels of the 640 px image it was sent (checked 2026-09-25: 1-2 s, boxes on target)

A box is [x, y, w, h] as fractions of the image. Boxes are cached in data/sighting_boxes.json per
(source, ref, name): a snapshot never changes, so each subject is located once. A failed look is cached too and
retried after an hour.
"""

import base64
import io
import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

WIDTH = 640                 # frames are sent to the vision model at this width (CLAUDE.md: 448-640 px)
RETRY_S = 3600              # a subject the model could not find is asked again after this
_NUM = r"(-?\d+(?:\.\d+)?)"
BOX_RX = re.compile(r"\[\s*" + r"\s*,\s*".join([_NUM] * 4) + r"\s*\]")

Box = List[float]


def parse_box(text: str, width: int, height: int) -> Optional[Box]:
    """The first [x1, y1, x2, y2] (pixels) in the model's answer -> [x, y, w, h] fractions, or None."""
    m = BOX_RX.search(text or "")
    if not m:
        return None
    x1, y1, x2, y2 = (float(v) for v in m.groups())
    x1, x2 = sorted((min(max(x1, 0), width), min(max(x2, 0), width)))
    y1, y2 = sorted((min(max(y1, 0), height), min(max(y2, 0), height)))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return [round(x1 / width, 4), round(y1 / height, 4), round((x2 - x1) / width, 4), round((y2 - y1) / height, 4)]


def describe(profile: Optional[Dict[str, Any]], name: str) -> str:
    """How the vision model is told who to find: it knows 'a dog', not the dog's name."""
    if not profile:
        return "the person" if name.lower() in ("someone", "") else f"the {name}"
    kind = profile.get("species") or ("person" if profile.get("role") else "animal")
    traits = (profile.get("traits") or "").strip().rstrip(".")
    return f"the {kind} ({traits})" if traits else f"the {kind}"


def locate_prompt(description: str) -> str:
    return f"Locate {description} in the image. Output its bbox coordinates in JSON format."


def prepare(jpeg: bytes) -> Tuple[bytes, int, int]:
    """The picture as the model is asked about it: WIDTH px wide JPEG, and its size (the answer is in pixels of this one)."""
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if im.width != WIDTH:
        im = im.resize((WIDTH, round(im.height * WIDTH / im.width)))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), im.width, im.height


def locate(jpeg: bytes, description: str, vision_url: str, timeout: float = 60) -> Optional[Box]:
    """Ask the vision model where `description` is in the picture."""
    small, width, height = prepare(jpeg)
    payload = {"model": "vision", "max_tokens": 120, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(small).decode()}},
        {"type": "text", "text": locate_prompt(description)}]}]}
    url = vision_url.rstrip("/")
    url = url if url.endswith("/chat/completions") else f"{url}/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    return parse_box(text, width, height)


def frigate_box(frigate_url: str, event_id: str) -> Optional[Box]:
    """The tracked object's box from Frigate's event record (already fractions of the frame)."""
    with urllib.request.urlopen(f"{frigate_url.rstrip('/')}/api/events/{event_id}", timeout=5) as r:
        box = ((json.load(r) or {}).get("data") or {}).get("box")
    return [round(float(v), 4) for v in box] if box and len(box) == 4 else None


def crop(jpeg: bytes, box: Box, margin: float = 0.35, min_px: int = 160) -> bytes:
    """The subject with some room around it (a face detector needs context), as JPEG."""
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    W, H = im.size
    x, y, w, h = box[0] * W, box[1] * H, box[2] * W, box[3] * H
    pad_w, pad_h = max(w * margin, (min_px - w) / 2), max(h * margin, (min_px - h) / 2)
    left, top = max(0, int(x - pad_w)), max(0, int(y - pad_h))
    right, bottom = min(W, int(x + w + pad_w)), min(H, int(y + h + pad_h))
    buf = io.BytesIO()
    im.crop((left, top, right, bottom)).save(buf, format="JPEG", quality=92)
    return buf.getvalue()


class BoxCache:
    """(source|ref|name) -> box or None, persisted as JSON."""

    def __init__(self, path: Optional[str], clock: Callable[[], float] = time.time):
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f) or {}
            except (OSError, ValueError):
                pass

    def get(self, key: str) -> Tuple[bool, Optional[Box]]:
        """(known, box). A cached miss counts as unknown again after RETRY_S."""
        with self._lock:
            rec = self._data.get(key)
        if rec is None or (rec.get("box") is None and self.clock() - rec.get("at", 0) > RETRY_S):
            return False, None
        return True, rec.get("box")

    def put(self, key: str, box: Optional[Box]) -> None:
        with self._lock:
            self._data[key] = {"box": box, "at": self.clock()}
            if len(self._data) > 2000:                     # keep the newest; old snapshots are long gone
                for k in sorted(self._data, key=lambda k: self._data[k]["at"])[:500]:
                    del self._data[k]
            snapshot = dict(self._data)
        if self.path:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f)
                os.replace(tmp, self.path)
            except OSError:
                pass
