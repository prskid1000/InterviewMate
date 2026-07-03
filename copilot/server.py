"""FastAPI server + orchestrator.

Flow (all manual, no auto-detection):
  [Start recording] -> both channels buffer (loopback=interviewer, mic=me)
  [Stop]            -> each channel transcribed -> shown in transcript
                    -> LLM streams a suggested answer, aware of the full
                       Q/A history so deep follow-up grilling stays consistent.
"""
import asyncio
import json
import threading
import time
from collections import deque

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from . import profiles as profiles_mod
from .config import CFG, ROOT
from .llm import ProviderChain
from .stt import make_stt

app = FastAPI()

MIN_SAMPLES = 8000  # ignore channel buffers shorter than 0.5 s


class Copilot:
    def __init__(self):
        self.loop = None
        self.events: asyncio.Queue | None = None
        self.clients: set[WebSocket] = set()
        self.listeners: list = []            # in-process subscribers (overlay)
        self.levels_me = deque(maxlen=18)    # live mic level (you)
        self.levels_int = deque(maxlen=18)   # live loopback level (interviewer)
        self.levels = self.levels_me         # back-compat alias

        self.profiles = profiles_mod.list_profiles()
        cfg_prof = CFG.get("profile")
        self.active_profile = cfg_prof if cfg_prof in self.profiles else next(iter(self.profiles), None)
        self.chain = ProviderChain(CFG.get("llm", {}))
        self.stt = None
        self.audio = None

        self.recording = False
        self.rec_lock = threading.Lock()
        self.rec_bufs = {"interviewer": [], "candidate": []}
        self.rec_started = 0.0
        self.max_rec = int(CFG.get("audio", {}).get("max_record_seconds", 180))

        self.transcript: list[dict] = []
        self.exchanges: list[dict] = []
        self.qid = 0
        self.cancel_event: threading.Event | None = None

    # ---------- lifecycle ----------

    def startup(self, loop):
        self.loop = loop
        self.events = asyncio.Queue()
        threading.Thread(target=self._init_backends, daemon=True).start()

    def post(self, msg: dict):
        if self.loop and self.events is not None:
            self.loop.call_soon_threadsafe(self.events.put_nowait, msg)
        for fn in list(self.listeners):
            try:
                fn(msg)
            except Exception:
                pass

    def status(self, text: str, level: str = "info"):
        self.post({"type": "status", "level": level, "text": text})

    def _init_backends(self):
        self._init_stt()
        self._init_audio()

    def _init_stt(self):
        try:
            self.status("Preparing speech-to-text…")
            self.stt = make_stt(CFG.get("stt", {}))
            self.status(f"Speech-to-text ready ({getattr(self.stt, 'device', '?')}).")
        except Exception as e:
            self.stt = None
            self.status(f"STT unavailable: {e}", "error")

    def _init_audio(self):
        try:
            from .audio import AudioEngine
            a = CFG.get("audio", {})
            self.audio = AudioEngine(
                self._on_chunk,
                capture_mic=a.get("capture_mic", True),
                capture_system=a.get("capture_system_audio", True),
            )
            self.audio.start()
            self.post({"type": "devices", "devices": self.audio.devices, "errors": self.audio.errors})
            desc = ", ".join(f"{k} = {v}" for k, v in self.audio.devices.items()) or "none"
            self.status(f"Audio ready — {desc}")
            for ch, err in self.audio.errors.items():
                self.status(f"Audio channel '{ch}' unavailable: {err}", "error")
        except Exception as e:
            self.status(f"Audio capture unavailable: {e}. Manual text input still works.", "error")

    # ---------- recording ----------

    def _on_chunk(self, channel: str, samples: np.ndarray):
        # Live levels update ALWAYS so the meter confirms both sources are
        # being picked up before you record; audio only buffers while recording.
        rms = float(np.sqrt(np.mean(samples ** 2)))
        lvl = min(1.0, rms * 14.0)
        if channel == "candidate":
            self.levels_me.append(lvl)
        elif channel == "interviewer":
            self.levels_int.append(lvl)
        if not self.recording:
            return
        buf = self.rec_bufs.get(channel)
        if buf is not None:
            buf.append(samples)
        if time.time() - self.rec_started > self.max_rec:
            self.status(f"Recording auto-stopped after {self.max_rec}s.")
            self.record_stop()

    def record_start(self):
        with self.rec_lock:
            if self.recording:
                return
            self.rec_bufs = {"interviewer": [], "candidate": []}
            self.rec_started = time.time()
            self.recording = True
        self.post({"type": "recording", "on": True})

    def record_stop(self):
        with self.rec_lock:
            if not self.recording:
                return
            self.recording = False
        self.post({"type": "recording", "on": False})
        threading.Thread(target=self._process_recording, daemon=True).start()

    def _process_recording(self):
        if self.stt is None:
            self.status("Speech model is not ready yet — wait a moment and retry.", "error")
            self.post({"type": "pipeline_idle"})
            return
        bufs = self.rec_bufs
        q_audio = np.concatenate(bufs["interviewer"]) if bufs["interviewer"] else np.zeros(0, np.float32)
        a_audio = np.concatenate(bufs["candidate"]) if bufs["candidate"] else np.zeros(0, np.float32)
        self.status("Transcribing...")
        qtext = atext = ""
        try:
            if len(q_audio) >= MIN_SAMPLES:
                qtext = self.stt.transcribe(q_audio)
            if len(a_audio) >= MIN_SAMPLES:
                atext = self.stt.transcribe(a_audio)
        except Exception as e:
            self.status(f"Transcription failed: {e}", "error")
            self.post({"type": "pipeline_idle"})
            return
        if not qtext and not atext:
            self.status("No speech detected in that recording.")
            self.post({"type": "pipeline_idle"})
            return
        self._add_transcript("interviewer", qtext)
        self._add_transcript("me", atext)
        self.answer(qtext, atext)

    def _add_transcript(self, speaker: str, text: str):
        if not text:
            return
        entry = {"speaker": speaker, "text": text, "ts": time.time()}
        self.transcript.append(entry)
        self.post({"type": "transcript", **entry})

    # ---------- answering ----------

    def answer(self, qtext: str, atext: str):
        if self.cancel_event:
            self.cancel_event.set()
        cancel = self.cancel_event = threading.Event()
        self.qid += 1
        threading.Thread(
            target=self._answer, args=(self.qid, qtext, atext, cancel), daemon=True
        ).start()

    @staticmethod
    def _build_user_msg(qtext: str, atext: str) -> str:
        parts = []
        if qtext:
            parts.append(f"Interviewer asked: {qtext}")
        if atext:
            parts.append(f"My answer attempt (what I actually said): {atext}")
        if qtext and not atext:
            parts.append("I have not answered yet — give me the answer to speak.")
        elif qtext and atext:
            parts.append("Improve/complete my answer — what should I say now?")
        elif atext:
            parts.append("Based on what I said, how should I strengthen or continue it?")
        return "\n".join(parts)

    def _answer(self, qid: int, qtext: str, atext: str, cancel: threading.Event):
        self.profiles = profiles_mod.list_profiles()  # pick up prompt edits live
        prof = self.profiles.get(self.active_profile)
        if prof:
            system = profiles_mod.render(prof, CFG.get("vars", {}))
            temp = (prof.get("meta") or {}).get("temperature")
        else:
            system = "You are a real-time interview assistant. Give concise, speakable answers."
            temp = None
        persona = (CFG.get("persona") or "").strip()
        if persona:
            system += "\n\n--- RESPONSE STYLE (persona) ---\n" + persona
        msgs = [{"role": "system", "content": system}]
        n = int(CFG.get("llm", {}).get("history_exchanges", 12))
        for ex in self.exchanges[-n:]:
            msgs.append({"role": "user", "content": ex["q"]})
            msgs.append({"role": "assistant", "content": ex["a"] or "(no suggestion given)"})
        user = self._build_user_msg(qtext, atext)
        msgs.append({"role": "user", "content": user})

        self.post({"type": "answer_start", "qid": qid, "question": qtext or atext})
        acc = []
        try:
            for provider, delta in self.chain.stream(msgs, temperature=temp):
                if cancel.is_set():
                    break
                acc.append(delta)
                self.post({"type": "answer_delta", "qid": qid, "text": delta, "provider": provider})
        except Exception as e:
            self.post({"type": "answer_error", "qid": qid, "text": str(e)})
        self.post({"type": "answer_done", "qid": qid})
        # store even partial answers so follow-up questions keep full context
        self.exchanges.append({"q": user, "a": "".join(acc)})

    def clear(self):
        self.transcript.clear()
        self.exchanges.clear()
        self.status("Session cleared — history is empty.")
        self.post({"type": "cleared"})

    def apply_settings(self):
        """Re-read CFG after the settings window saves: rebuild the LLM chain,
        reload profiles, and rebuild STT in the background if needed."""
        from .config import load_config
        CFG.clear()
        CFG.update(load_config())
        self.chain = ProviderChain(CFG.get("llm", {}))
        self.profiles = profiles_mod.list_profiles()
        p = CFG.get("profile")
        if p in self.profiles:
            self.active_profile = p
        self.max_rec = int(CFG.get("audio", {}).get("max_record_seconds", 180))
        threading.Thread(target=self._init_stt, daemon=True).start()
        self.status("Settings applied.")
        self.post({"type": "settings_applied"})


CO = Copilot()


async def _pump():
    while True:
        msg = await CO.events.get()
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(CO.clients):
            try:
                await ws.send_text(data)
            except Exception:
                CO.clients.discard(ws)


@app.on_event("startup")
async def _startup():
    CO.startup(asyncio.get_running_loop())
    asyncio.create_task(_pump())


@app.get("/")
async def index():
    return HTMLResponse((ROOT / "ui" / "index.html").read_text(encoding="utf-8"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CO.clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "hello",
        "profiles": [{"id": k, "name": v["name"]} for k, v in CO.profiles.items()],
        "active_profile": CO.active_profile,
        "providers": CO.chain.available(),
        "recording": CO.recording,
        "transcript": CO.transcript[-200:],
    }, ensure_ascii=False))
    try:
        while True:
            m = json.loads(await ws.receive_text())
            t = m.get("type")
            if t == "record_start":
                CO.record_start()
            elif t == "record_stop":
                CO.record_stop()
            elif t == "ask":  # manual: interviewer's question typed in
                text = (m.get("text") or "").strip()
                if text:
                    CO._add_transcript("interviewer", text)
                    CO.answer(text, "")
            elif t == "say":  # manual: what I said / want to say
                text = (m.get("text") or "").strip()
                if text:
                    CO._add_transcript("me", text)
                    CO.answer("", text)
            elif t == "set_profile":
                name = m.get("name")
                CO.profiles = profiles_mod.list_profiles()  # pick up edits on disk
                if name in CO.profiles:
                    CO.active_profile = name
                    CO.status(f"Profile switched to: {CO.profiles[name]['name']}")
            elif t == "clear":
                CO.clear()
    except WebSocketDisconnect:
        pass
    finally:
        CO.clients.discard(ws)
