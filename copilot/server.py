"""FastAPI server + orchestrator.

Flow (always-on, dual-channel):
  Both channels are continuously VAD-segmented and transcribed live, in
  parallel (loopback=interviewer, mic=me), by local CPU Whisper. Transcription
  is IDENTICAL in manual and auto mode — the only difference is WHEN we send to
  the LLM: auto = when the interviewer finishes a question; manual = when you
  hit the hotkey. History records YOUR actual spoken response as the answer of
  record, so follow-ups stay grounded in what you really said.
"""
import asyncio
import json
import queue
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

# Whisper hallucinates these on near-silence; ignore segments that are only these.
_FILLER = {"you", "and", "the", "uh", "um", "umm", "hmm", "mm", "so", "okay", "ok",
           "yeah", "yep", "thanks", "thank", "bye", "a", "oh", "hi", "mhm"}


def _meaningful(text: str) -> bool:
    """True if the transcript carries real content (not blank / pure filler)."""
    t = (text or "").strip()
    if len(t) < 2:
        return False
    words = [w.strip(".,!?;:'\"-").lower() for w in t.split()]
    content = [w for w in words if w and w not in _FILLER]
    return sum(len(w) for w in content) >= 3


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
        self._stt_error = None      # why STT failed to init, if it did
        self.audio = None

        a = CFG.get("audio", {}) or {}
        self.max_rec = int(a.get("max_record_seconds", 180))
        self.auto_silence = bool(a.get("auto_silence", False))   # auto-send vs manual send
        self.silence_seconds = float(a.get("silence_seconds", 1.2))
        self.speech_threshold = float(a.get("speech_threshold", 0.02))
        self.min_words = 3                       # ignore filler blips as questions

        # two parallel STT engines, one per channel
        self.stt_int = None
        self.stt_me = None
        self._q_int: queue.Queue = queue.Queue()
        self._q_me: queue.Queue = queue.Queue()
        # per-channel VAD segmenter state
        self._seg = {"interviewer": {"buf": [], "speaking": False, "last": 0.0},
                     "candidate":  {"buf": [], "speaking": False, "last": 0.0}}
        self._cur_q = ""        # latest interviewer question
        self._cur_resp = ""     # my response accumulated since that question
        self._last_q = ""       # de-dupe auto triggers
        self.recording = True   # continuous listening (kept name for HUD compat)

        # sessions/tabs — each is an independent conversation with its own
        # transcript + exchange history. The active session's context is what
        # the AI answers from.
        self._sid = 0
        self.sessions: list[dict] = [self._blank_session()]
        self.active = 0
        self.qid = 0
        self.cancel_event: threading.Event | None = None

    def _blank_session(self, title: str | None = None) -> dict:
        self._sid += 1
        return {"id": self._sid, "title": title or f"Session {self._sid}",
                "transcript": [], "exchanges": []}

    # active-session proxies so the rest of the code is unchanged
    @property
    def transcript(self) -> list:
        return self.sessions[self.active]["transcript"]

    @property
    def exchanges(self) -> list:
        return self.sessions[self.active]["exchanges"]

    def sessions_meta(self) -> list:
        return [{"id": s["id"], "title": s["title"]} for s in self.sessions]

    def _post_sessions(self):
        self.post({"type": "sessions", "list": self.sessions_meta(), "active": self.active})

    def _post_active(self):
        self.post({"type": "session_activated", "active": self.active,
                   "transcript": list(self.transcript)})

    def _reset_turn(self):
        """Clear the live turn accumulators (they're not carried across sessions)."""
        self._cur_q = self._cur_resp = self._last_q = ""

    def new_session(self):
        self.sessions.append(self._blank_session())
        self.active = len(self.sessions) - 1
        self._reset_turn()
        self._post_sessions(); self._post_active()

    def switch_session(self, idx: int):
        if 0 <= idx < len(self.sessions) and idx != self.active:
            self.active = idx
            self._reset_turn()
            self._post_sessions(); self._post_active()

    def close_session(self, idx: int):
        if len(self.sessions) <= 1 or not (0 <= idx < len(self.sessions)):
            return
        self.sessions.pop(idx)
        self._reset_turn()
        self.active = min(self.active if idx > self.active else self.active - 1,
                          len(self.sessions) - 1)
        self.active = max(0, self.active)
        self._post_sessions(); self._post_active()

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
            self.status("Loading local speech model (CPU)…")
            from .stt import LocalSTT
            stt_cfg = CFG.get("stt", {}) or {}
            lang = stt_cfg.get("language", "en")
            model = stt_cfg.get("model", "large-v3")
            self.status(f"Loading local speech model '{model}' (CPU)…")
            # two instances so both channels transcribe truly in parallel
            self.stt_int = LocalSTT(model=model, language=lang)
            self.stt_me = LocalSTT(model=model, language=lang)
            self.stt = self.stt_me     # back-compat alias
            self._stt_error = None
            threading.Thread(target=self._seg_worker, args=("interviewer", self._q_int, self.stt_int),
                             daemon=True, name="stt-interviewer").start()
            threading.Thread(target=self._seg_worker, args=("candidate", self._q_me, self.stt_me),
                             daemon=True, name="stt-candidate").start()
            mode = "Auto-answer on." if self.auto_silence else "Press the hotkey to ask."
            self.status(f"Listening — local Whisper (CPU). {mode}")
        except Exception as e:
            self.stt = self.stt_int = self.stt_me = None
            self._stt_error = str(e)
            self.status(f"Local speech model failed to load: {e}", "error")

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

    # ---------- continuous listening ----------

    def _seg_len(self, buf) -> int:
        return sum(len(b) for b in buf)

    def _on_chunk(self, channel: str, samples: np.ndarray):
        """Called from each audio reader thread. Updates live meters and runs a
        per-channel VAD segmenter; completed speech segments go to that
        channel's transcription worker. Identical in manual and auto mode."""
        rms = float(np.sqrt(np.mean(samples ** 2)))
        lvl = min(1.0, rms * 14.0)
        if channel == "candidate":
            self.levels_me.append(lvl)
        elif channel == "interviewer":
            self.levels_int.append(lvl)

        st = self._seg.get(channel)
        if st is None or self.stt_int is None:   # models not loaded yet
            return
        q = self._q_int if channel == "interviewer" else self._q_me
        now = time.time()
        speech = lvl >= self.speech_threshold
        if speech:
            st["speaking"] = True
            st["last"] = now
            st["buf"].append(samples)
        elif st["speaking"]:
            st["buf"].append(samples)               # keep trailing silence
            if now - st["last"] > self.silence_seconds:
                self._flush_segment(channel, st, q)
        # safety cap so one long stretch still gets transcribed
        if st["speaking"] and self._seg_len(st["buf"]) > 16000 * self.max_rec:
            self._flush_segment(channel, st, q)

    def _flush_segment(self, channel, st, q):
        buf = st["buf"]
        st["buf"] = []
        st["speaking"] = False
        if self._seg_len(buf) >= MIN_SAMPLES:
            q.put(np.concatenate(buf))

    def _seg_worker(self, channel: str, q: queue.Queue, stt):
        """One per channel — transcribes completed segments in parallel."""
        while True:
            audio = q.get()
            if audio is None:
                return
            try:
                text = stt.transcribe(audio)
            except Exception as e:
                self.status(f"Transcription failed — {e}", "error")
                continue
            if _meaningful(text):        # drop blank / filler / silence hallucinations
                self._on_segment(channel, text)

    def _on_segment(self, channel: str, text: str):
        if channel == "interviewer":
            self._add_transcript("interviewer", text)
            # a new question closes the previous turn — record MY real answer
            if self._cur_q and self._cur_resp.strip():
                self.exchanges.append({"q": self._cur_q, "a": self._cur_resp.strip(),
                                       "qd": self._cur_q})
            self._cur_q = text
            self._cur_resp = ""
            # auto mode: send as soon as the interviewer finishes (with guardrails)
            if self.auto_silence and len(text.split()) >= self.min_words and text != self._last_q:
                self._last_q = text
                self._trigger_answer()
        else:  # candidate — this is what I actually say; context, not a trigger
            self._add_transcript("me", text)
            self._cur_resp = (self._cur_resp + " " + text).strip()

    def _trigger_answer(self):
        """Send the current turn (interviewer question + my response so far) to
        the LLM. Called automatically in auto mode and by the hotkey in manual."""
        if self.stt_int is None:
            self.status(f"Speech model not ready — {self._stt_error or 'loading…'}", "error")
            return
        # don't send on a blank/filler turn (e.g. first press in a new session)
        if not (_meaningful(self._cur_q) or _meaningful(self._cur_resp)):
            self.status("Nothing to send yet — waiting for the interviewer's question.")
            return
        self.answer(self._cur_q, self._cur_resp)

    def ask_now(self):
        """Manual trigger (hotkey / status-dot). Recording never stops; this
        only controls WHEN we send to the LLM."""
        self._trigger_answer()

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
            system += profiles_mod.context_block(self.active_profile)
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
        # persist the AI message into the session log (for rebuild on tab switch);
        # no display event — the overlay already streamed this bubble live.
        ai_text = "".join(acc).strip()
        if ai_text:
            self.transcript.append({"speaker": "ai", "text": ai_text, "ts": time.time()})
        # NB: conversation *history* (for follow-up context) is recorded in
        # _on_segment using MY actual spoken response, not this AI suggestion.

    def clear(self):
        self.transcript.clear()
        self.exchanges.clear()
        self._cur_q = self._cur_resp = self._last_q = ""
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
        a = CFG.get("audio", {}) or {}
        self.max_rec = int(a.get("max_record_seconds", 180))
        self.auto_silence = bool(a.get("auto_silence", False))
        self.silence_seconds = float(a.get("silence_seconds", 1.2))
        self.speech_threshold = float(a.get("speech_threshold", 0.02))
        # STT is fixed local Whisper — no rebuild needed on settings change.
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
            if t in ("record_start", "record_stop", "ask_now"):
                CO.ask_now()   # continuous listening; this just sends to the LLM
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
