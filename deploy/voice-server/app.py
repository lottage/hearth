"""Voice server (VM 102): speech-to-text and text-to-speech for StoneSage, HA and the phone.

    POST /v1/audio/transcriptions   OpenAI-style multipart (file, model?) -> {"text", "engine", "ms"}
    POST /v1/audio/speech           OpenAI-style JSON (input, voice, speed, response_format wav|pcm|mp3) -> audio bytes
    GET  /health, GET /v1/voices
    Wyoming (Home Assistant): STT on WYOMING_STT_PORT, TTS on WYOMING_TTS_PORT (wyoming_servers.py)

Replaces the CPU-only voice LXC (faster-whisper base.en ~3 s per clip, Kokoro 0.9-5 s) with the same API on a much faster host:
docs/evals/voice-bench-2026-09-29.md. LAN only, no auth (like the services it replaces).
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import audio
from engines import STT_NAMES, SttEngines, Tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("voice")

MODELS = os.environ.get("VOICE_MODELS", "/opt/models/voice")
STT_DEFAULT = os.environ.get("VOICE_STT_DEFAULT", "parakeet")
STT_THREADS = int(os.environ.get("VOICE_STT_THREADS", "3"))
TTS_THREADS = int(os.environ.get("VOICE_TTS_THREADS", "4"))
VOCAB = os.environ.get("VOICE_VOCAB", "/opt/voice-server/vocab.txt")   # household names + hints (vocab.py)

stt: SttEngines
tts: Tts


@asynccontextmanager
async def lifespan(app: FastAPI):
    global stt, tts
    stt = SttEngines(MODELS, STT_DEFAULT, STT_THREADS, VOCAB)
    await asyncio.to_thread(stt.get, stt.resolve(STT_DEFAULT))
    tts = await asyncio.to_thread(Tts, MODELS, TTS_THREADS)
    servers = []
    try:
        import wyoming_servers
        servers = await wyoming_servers.start(stt, tts, int(os.environ.get("WYOMING_STT_PORT", "10301")),
                                              int(os.environ.get("WYOMING_TTS_PORT", "10201")))
    except ImportError:
        log.warning("wyoming is not installed: HTTP only")
    log.info("voice server ready: STT default %s, %d Kokoro voices", stt.default, len(tts.voices))
    yield
    for s in servers:
        s.cancel()


app = FastAPI(title="voice-server", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "stt": {"default": stt.default, "loaded": stt.loaded(), "available": STT_NAMES},
            "tts": {"engine": "kokoro-onnx-fp32", "voices": len(tts.voices)}}


@app.get("/v1/voices")
def voices():
    return {"voices": tts.voices}


@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...), model: str = Form(""), language: str = Form("en"),
                         response_format: str = Form("json")):
    data = await file.read()
    try:
        samples = await asyncio.to_thread(audio.decode_to_float32, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    text, engine, ms = await asyncio.to_thread(stt.transcribe, samples, model)
    log.info("STT %s: %.2fs of audio in %.0f ms -> %r", engine, len(samples) / 16000, ms, text[:80])
    if response_format == "text":
        return Response(text, media_type="text/plain")
    return {"text": text, "engine": engine, "ms": round(ms), "audio_s": round(len(samples) / 16000, 2)}


class SpeechRequest(BaseModel):
    model: str = "kokoro"
    input: str
    voice: str = "bm_george"
    speed: float = 1.0
    response_format: str = "wav"
    stream: bool = False


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    if not req.input.strip():
        raise HTTPException(400, "empty input")
    samples, ms = await asyncio.to_thread(tts.create, req.input, req.voice, req.speed)
    log.info("TTS %s: %d chars -> %.1fs of audio in %.0f ms", req.voice, len(req.input), len(samples) / tts.SAMPLE_RATE, ms)
    fmt = req.response_format.lower()
    if fmt == "pcm":
        body, mime = audio.to_pcm16(samples), "audio/pcm"
    elif fmt == "mp3":
        body, mime = await asyncio.to_thread(audio.to_mp3, samples, tts.SAMPLE_RATE), "audio/mpeg"
    else:
        body, mime = audio.to_wav(samples, tts.SAMPLE_RATE), "audio/wav"
    return Response(body, media_type=mime, headers={"X-Synthesis-Ms": str(round(ms))})
