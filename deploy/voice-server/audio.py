"""Audio in and out: any container the browser or phone records -> float32 mono 16 kHz, and PCM -> wav / mp3 bytes."""

import io
import os
import subprocess
import tempfile

import numpy as np
import soundfile as sf


def decode_to_float32(data: bytes, sample_rate: int = 16000) -> np.ndarray:
    """Decode webm/opus, mp4/aac, wav, mp3... through ffmpeg. Goes via a temp file: an mp4 from a phone keeps its index at the
    end of the file, which ffmpeg cannot read from a pipe."""
    if len(data) < 100:
        raise ValueError("audio too short")
    fd, path = tempfile.mkstemp(suffix=".audio")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        p = subprocess.run(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", path, "-f", "f32le", "-ac", "1",
                            "-ar", str(sample_rate), "pipe:1"], capture_output=True, timeout=30)
    finally:
        os.unlink(path)
    if p.returncode != 0 or not p.stdout:
        raise ValueError("could not decode audio: " + p.stderr.decode("utf-8", "replace")[:200])
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def to_mp3(samples: np.ndarray, sample_rate: int) -> bytes:
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-nostdin", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
                        "-i", "pipe:0", "-f", "mp3", "pipe:1"], input=to_pcm16(samples), capture_output=True, timeout=30)
    if p.returncode != 0:
        raise ValueError("mp3 encode failed")
    return p.stdout
