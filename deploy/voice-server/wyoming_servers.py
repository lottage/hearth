"""Wyoming protocol servers so Home Assistant's Assist pipeline can use the same engines (STT and TTS on their own ports).

Home Assistant adds each as a Wyoming integration (host, port); nothing here changes HA by itself.
"""

import asyncio
import logging
from functools import partial
from typing import List

import numpy as np
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

log = logging.getLogger("voice.wyoming")
_ATTRIBUTION = Attribution(name="StoneSage voice-server", url="")
_CHUNK_SAMPLES = 1024


class SttHandler(AsyncEventHandler):
    def __init__(self, stt, info: Info, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stt, self.info, self._audio = stt, info, bytearray()
        self._rate = 16000

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info.event())
        elif AudioStart.is_type(event.type):
            self._audio.clear()
            self._rate = AudioStart.from_event(event).rate
        elif AudioChunk.is_type(event.type):
            self._audio.extend(AudioChunk.from_event(event).audio)
        elif AudioStop.is_type(event.type):
            x = np.frombuffer(bytes(self._audio), dtype="<i2").astype(np.float32) / 32768.0
            if self._rate != 16000 and len(x):
                x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / self._rate)), np.arange(len(x)), x).astype(np.float32)
            text, engine, ms = await asyncio.to_thread(self.stt.transcribe, x, None)
            log.info("wyoming STT %s: %.2fs in %.0f ms -> %r", engine, len(x) / 16000, ms, text[:80])
            await self.write_event(Transcript(text=text).event())
            self._audio.clear()
        return True


class TtsHandler(AsyncEventHandler):
    def __init__(self, tts, info: Info, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tts, self.info = tts, info

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info.event())
        elif Synthesize.is_type(event.type):
            req = Synthesize.from_event(event)
            voice = (req.voice.name if req.voice and req.voice.name else "bm_george")
            samples, ms = await asyncio.to_thread(self.tts.create, req.text, voice, 1.0)
            log.info("wyoming TTS %s: %d chars in %.0f ms", voice, len(req.text), ms)
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            rate = self.tts.SAMPLE_RATE
            await self.write_event(AudioStart(rate=rate, width=2, channels=1).event())
            step = _CHUNK_SAMPLES * 2
            for i in range(0, len(pcm), step):
                await self.write_event(AudioChunk(rate=rate, width=2, channels=1, audio=pcm[i:i + step]).event())
            await self.write_event(AudioStop().event())
        return True


async def start(stt, tts, stt_port: int, tts_port: int) -> List[asyncio.Task]:
    stt_info = Info(asr=[AsrProgram(
        name="stonesage-stt", description="Parakeet / Moonshine / Whisper on VM 102", attribution=_ATTRIBUTION, installed=True,
        version="1", models=[AsrModel(name=stt.default, description=stt.default, attribution=_ATTRIBUTION, installed=True,
                                      version="1", languages=["en"])])])
    tts_info = Info(tts=[TtsProgram(
        name="stonesage-tts", description="Kokoro (ONNX) on VM 102", attribution=_ATTRIBUTION, installed=True, version="1",
        voices=[TtsVoice(name=v, description=v, attribution=_ATTRIBUTION, installed=True, version="1",
                         languages=["en-GB" if v[:2] in ("bm", "bf") else "en-US"]) for v in tts.voices])])
    tasks = []
    for port, factory in ((stt_port, partial(SttHandler, stt, stt_info)), (tts_port, partial(TtsHandler, tts, tts_info))):
        server = AsyncServer.from_uri(f"tcp://0.0.0.0:{port}")
        tasks.append(asyncio.create_task(server.run(factory)))
        log.info("Wyoming server on :%d", port)
    return tasks
