"""Audio cache for the voice: what Computer says most is short and repeats ("Living Room TV Lights off."), so its audio is
kept on disk and served in milliseconds instead of being synthesised again (0.5-1 s each on the voice server).

Keyed by (voice, speed, exact text): the page sends the same cleaned text every time, so a hit is exact. Least recently used
files are dropped past MAX_BYTES. `warm()` makes the reflex confirmations for every device ahead of time.
"""

import hashlib
import logging
import os
import threading
import time
from typing import Callable, Iterable, List, Optional, Tuple

logger = logging.getLogger("StoneSage.tts_cache")

MAX_BYTES = 150 * 1024 * 1024
MAX_TEXT = 200        # longer text is a one-off reply, not worth keeping

_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "warmed": 0}


def _path(cache_dir: str, text: str, voice: str, speed: float) -> str:
    key = hashlib.sha256(f"{voice}|{speed:.3f}|{text}".encode("utf-8")).hexdigest()[:32]
    return os.path.join(cache_dir, key + ".wav")


def stats() -> dict:
    return dict(_stats)


def get_or_make(cache_dir: str, text: str, voice: str, speed: float,
                synth: Callable[[str, str, float], Optional[bytes]]) -> Tuple[Optional[bytes], bool]:
    """(audio, from_cache). Text that is too long is synthesised and not kept."""
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT:
        return synth(text, voice, speed), False
    os.makedirs(cache_dir, exist_ok=True)
    p = _path(cache_dir, text, voice, speed)
    try:
        with open(p, "rb") as f:
            audio = f.read()
        if audio:
            os.utime(p, None)                       # recently used
            _stats["hits"] += 1
            return audio, True
    except OSError:
        pass
    _stats["misses"] += 1
    audio = synth(text, voice, speed)
    if audio:
        tmp = p + f".{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(audio)
        os.replace(tmp, p)
        _trim(cache_dir)
    return audio, False


def _trim(cache_dir: str) -> None:
    with _lock:
        files = []
        for n in os.listdir(cache_dir):
            if n.endswith(".wav"):
                fp = os.path.join(cache_dir, n)
                try:
                    st = os.stat(fp)
                    files.append((st.st_mtime, st.st_size, fp))
                except OSError:
                    pass
        total = sum(f[1] for f in files)
        for _mt, size, fp in sorted(files):        # oldest use first
            if total <= MAX_BYTES:
                break
            try:
                os.remove(fp)
                total -= size
            except OSError:
                pass


def reflex_lines(names_states: Iterable[Tuple[str, str]], extra: Iterable[str] = ()) -> List[str]:
    """Every line a reflex order or a canned answer can produce for these devices, without duplicates."""
    from stonesage.courage.agent import already_text, switched_text
    lines: List[str] = []
    for name, _current in names_states:
        for state in ("on", "off"):
            lines.append(switched_text(name, state))
            lines.append(already_text(name, state))
    lines.extend(extra)
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def warm(cache_dir: str, lines: Iterable[str], voice: str, speed: float,
         synth: Callable[[str, str, float], Optional[bytes]], pause_s: float = 0.05) -> int:
    """Make any missing clip. Returns how many were made. Sequential and gentle: the voice server is shared with live turns."""
    made = 0
    for line in lines:
        audio, cached = get_or_make(cache_dir, line, voice, speed, synth)
        if audio and not cached:
            made += 1
            _stats["warmed"] += 1
        time.sleep(pause_s)
    return made
