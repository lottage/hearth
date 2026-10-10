"""Where StoneSage sends audio to be transcribed: the voice server on VM 102 first, the old voice LXC as the fallback.

Both speak the same OpenAI-style API (POST /v1/audio/transcriptions). config.json `voice`:
    {"stt_url": "http://<vm102>:8210", "stt_fallback_url": "http://<lxc>:8200", "stt_engine": "", "timeout_s": 15}
Defaults below match the deployed layout (docs/evals/voice-bench-2026-09-29.md).
"""

import json
import logging
import time
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("StoneSage.voice")

from lodestone import endpoints

DEFAULTS = {"stt_engine": "", "timeout_s": 15.0}


def settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """config.json "voice"; the server and the old voice host come from the endpoint table (lodestone/endpoints.py)."""
    config = config or {}
    legacy = endpoints.url("voice_legacy", config)
    base = {**DEFAULTS, "stt_url": endpoints.url("voice", config), "stt_fallback_url": f"{legacy}:8200" if legacy else ""}
    return {**base, **(config.get("voice") or {})}


def _post_audio(base: str, audio: bytes, mime: str, model: str, timeout: float) -> Dict[str, Any]:
    ext = "webm" if "webm" in mime else "mp4" if "mp4" in mime else "ogg" if "ogg" in mime else "wav"
    b = uuid.uuid4().hex
    body = b"".join([
        f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="recording.{ext}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),
        audio, b"\r\n",
        f'--{b}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'.encode(),
        f'--{b}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'.encode(),
        f'--{b}\r\nContent-Disposition: form-data; name="language"\r\n\r\nen\r\n'.encode(),
        f"--{b}--\r\n".encode()])
    req = urllib.request.Request(f"{base.rstrip('/')}/v1/audio/transcriptions", body,
                                 {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def transcribe(config: Dict[str, Any], audio: bytes, mime: str = "audio/webm") -> Dict[str, Any]:
    """{"text", "engine", "ms", "via"}; raises if every server failed."""
    s = settings(config)
    tried: List[str] = []
    for label, base, model in (("voice-server", s["stt_url"], s["stt_engine"] or "default"),
                               ("fallback", s["stt_fallback_url"], "base.en")):
        if not base:
            continue
        t = time.time()
        try:
            res = _post_audio(base, audio, mime, model, float(s["timeout_s"]))
            res.setdefault("ms", round((time.time() - t) * 1000))
            res["via"] = label
            if label != "voice-server":
                logger.warning("STT fell back to %s (%s)", base, "; ".join(tried))
            return res
        except Exception as e:
            tried.append(f"{label}: {type(e).__name__}: {str(e)[:80]}")
    raise RuntimeError("no speech-to-text server answered (" + "; ".join(tried) + ")")


def transcribe_with(base: str, audio: bytes, mime: str, engine: str, timeout: float = 30.0) -> Dict[str, Any]:
    """One named engine on one server: the recorder page compares them side by side."""
    t = time.time()
    res = _post_audio(base, audio, mime, engine, timeout)
    res.setdefault("ms", round((time.time() - t) * 1000))
    return res
