"""Microphone + WASAPI loopback capture, delivered as 16 kHz mono float32 chunks.

Channels:
  "candidate"   -> default microphone (your voice)
  "interviewer" -> WASAPI loopback of the default output device (whatever the
                   meeting app is playing: Google Meet, Zoom, Teams, anything)
"""
import threading

import numpy as np
import pyaudiowpatch as pyaudio

TARGET_RATE = 16000


def list_devices() -> list[dict]:
    p = pyaudio.PyAudio()
    try:
        out = []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            out.append({
                "index": d["index"], "name": d["name"],
                "inputs": d["maxInputChannels"], "rate": d["defaultSampleRate"],
                "loopback": bool(d.get("isLoopbackDevice")),
            })
        return out
    finally:
        p.terminate()


class AudioEngine:
    def __init__(self, on_chunk, capture_mic=True, capture_system=True, chunk_ms=30):
        """on_chunk(channel: str, samples: np.float32 @ 16 kHz) — called from reader threads."""
        self.on_chunk = on_chunk
        self.chunk_ms = chunk_ms
        self.p = pyaudio.PyAudio()
        self.running = False
        self.threads: list[threading.Thread] = []
        self.streams = []
        self.devices: dict[str, str] = {}
        self.errors: dict[str, str] = {}

        if capture_mic:
            try:
                self._open("candidate", self.p.get_default_input_device_info())
            except Exception as e:
                self.errors["candidate"] = str(e)
        if capture_system:
            try:
                self._open("interviewer", self.p.get_default_wasapi_loopback())
            except Exception as e:
                self.errors["interviewer"] = str(e)

    def _open(self, channel: str, info: dict):
        rate = int(info["defaultSampleRate"])
        ch = max(1, min(2, int(info["maxInputChannels"])))
        frames = max(160, int(rate * self.chunk_ms / 1000))
        stream = self.p.open(
            format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
            input_device_index=int(info["index"]), frames_per_buffer=frames,
        )
        self.streams.append(stream)
        self.devices[channel] = str(info["name"])
        t = threading.Thread(
            target=self._reader, args=(channel, stream, rate, ch, frames),
            daemon=True, name=f"audio-{channel}",
        )
        self.threads.append(t)

    def start(self):
        self.running = True
        for t in self.threads:
            t.start()

    def stop(self):
        self.running = False
        for s in self.streams:
            try:
                s.stop_stream(); s.close()
            except Exception:
                pass
        self.p.terminate()

    def _reader(self, channel, stream, rate, ch, frames):
        while self.running:
            try:
                data = stream.read(frames, exception_on_overflow=False)
            except Exception:
                if self.running:
                    self.errors[channel] = "stream read failed"
                return
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            if ch > 1:
                x = x.reshape(-1, ch).mean(axis=1)
            if rate != TARGET_RATE:
                n = max(1, int(len(x) * TARGET_RATE / rate))
                x = np.interp(
                    np.linspace(0, len(x) - 1, n), np.arange(len(x)), x
                ).astype(np.float32)
            self.on_chunk(channel, x)
