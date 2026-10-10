"""Speech engines. STT: Parakeet TDT, Moonshine, faster-whisper. TTS: Kokoro (ONNX, fp32).

Benchmarks (docs/evals/voice-bench-2026-09-29.md): on VM 102 these run 0.03-0.3 s per short clip, and short clips are
latency-bound, so 2-4 threads are best (more threads are slower). Models load lazily except the default, to keep RAM down.
"""

import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

import vocab
from text_norm import normalize_transcript

log = logging.getLogger("voice.engines")

STT_NAMES = ["parakeet", "moonshine-base", "moonshine-tiny", "whisper-tiny", "whisper-base", "whisper-small"]
# names other clients send (the old server was asked for "base.en"): anything unknown means "the default engine"
STT_ALIASES = {"parakeet-tdt-0.6b-v2": "parakeet", "moonshine": "moonshine-base"}


class SttEngines:
    def __init__(self, models_dir: str, default: str = "parakeet", threads: int = 3, vocab_path: str = "",
                 use_prompt: bool = True, use_correction: bool = True):
        self.models_dir, self.default, self.threads = models_dir, default, threads
        # household words (vocab.py): a hint for Whisper's prompt, and near-miss correction of names for every engine
        self.names, hints = vocab.load(vocab_path) if vocab_path else ([], [])
        self.prompt = vocab.prompt_from(hints) if use_prompt else ""
        self.use_correction = use_correction
        self._engines: Dict[str, Callable[[np.ndarray], str]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._load_lock = threading.Lock()

    def resolve(self, name: Optional[str]) -> str:
        name = (name or "").strip().lower()
        name = STT_ALIASES.get(name, name)
        return name if name in STT_NAMES else self.default

    def loaded(self) -> List[str]:
        return sorted(self._engines)

    def _build(self, name: str) -> Callable[[np.ndarray], str]:
        m = self.models_dir
        if name.startswith("whisper-"):
            from faster_whisper import WhisperModel
            model = WhisperModel(name.split("-", 1)[1] + ".en", device="cpu", compute_type="int8", cpu_threads=self.threads,
                                 download_root=f"{m}/whisper")
            prompt = self.prompt or None
            return lambda x: " ".join(s.text for s in model.transcribe(
                x, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False, initial_prompt=prompt)[0])
        import sherpa_onnx as so
        if name.startswith("moonshine-"):
            d = f"{m}/sherpa-onnx-moonshine-{name.split('-', 1)[1]}-en-int8"
            rec = so.OfflineRecognizer.from_moonshine(
                preprocessor=f"{d}/preprocess.onnx", encoder=f"{d}/encode.int8.onnx",
                uncached_decoder=f"{d}/uncached_decode.int8.onnx", cached_decoder=f"{d}/cached_decode.int8.onnx",
                tokens=f"{d}/tokens.txt", num_threads=self.threads)
        else:
            d = f"{m}/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
            rec = so.OfflineRecognizer.from_transducer(
                encoder=f"{d}/encoder.int8.onnx", decoder=f"{d}/decoder.int8.onnx", joiner=f"{d}/joiner.int8.onnx",
                tokens=f"{d}/tokens.txt", num_threads=self.threads, model_type="nemo_transducer")

        def run(x: np.ndarray) -> str:
            s = rec.create_stream()
            s.accept_waveform(16000, x)
            rec.decode_stream(s)
            return s.result.text
        return run

    def get(self, name: str) -> Callable[[np.ndarray], str]:
        with self._load_lock:
            if name not in self._engines:
                t = time.time()
                fn = self._build(name)
                fn(np.zeros(8000, dtype=np.float32))            # warm-up
                self._engines[name] = fn
                self._locks[name] = threading.Lock()
                log.info("loaded STT engine %s in %.1fs", name, time.time() - t)
        return self._engines[name]

    def transcribe(self, samples: np.ndarray, name: Optional[str] = None) -> Tuple[str, str, float]:
        """(text, engine used, ms). The recognizers are not thread-safe: one call at a time per engine."""
        engine = self.resolve(name)
        fn = self.get(engine)
        t = time.time()
        with self._locks[engine]:
            text = fn(samples)
        text = normalize_transcript(text)
        if self.use_correction:
            text = vocab.correct(text, self.names)
        return text, engine, (time.time() - t) * 1000


class Tts:
    """Kokoro through ONNX (the int8 build is SLOWER on this CPU, so fp32)."""

    SAMPLE_RATE = 24000

    def __init__(self, models_dir: str, threads: int = 4):
        from kokoro_onnx import Kokoro
        self._lock = threading.Lock()
        self._k = Kokoro(f"{models_dir}/tts/kokoro-v1.0.onnx", f"{models_dir}/tts/voices-v1.0.bin")
        self.voices = sorted(self._k.get_voices())
        self.create("Warm up.", self.voices[0] if self.voices else "bm_george", 1.0)

    @staticmethod
    def language_for(voice: str) -> str:
        return "en-gb" if voice[:2] in ("bm", "bf") else "en-us"

    def create(self, text: str, voice: str = "bm_george", speed: float = 1.0) -> Tuple[np.ndarray, float]:
        if voice not in self.voices and self.voices:
            voice = "bm_george" if "bm_george" in self.voices else self.voices[0]
        t = time.time()
        with self._lock:
            samples, _sr = self._k.create(text[:800], voice=voice, speed=max(0.5, min(2.0, float(speed))),
                                          lang=self.language_for(voice))
        return samples, (time.time() - t) * 1000
