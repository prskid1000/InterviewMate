"""FastAPI server + orchestrator.

Manual flow (buttons on the HUD / browser UI):
  ▶ Listen  opens the mic ("candidate") and the WASAPI loopback ("interviewer").
            Each channel's audio is cut every `audio.chunk_seconds` at its
            quietest point and transcribed in the background, so the work is
            mostly done before you ask. Nothing is sent to the LLM yet.
  Answer    closes the current turn: both channels' remaining audio is cut and
            transcribed, every chunk of the turn is merged by the time it was
            spoken ("Interviewer: … / Me: …") and sent; the answer streams back
            (it continues / improves my attempt if I'd started answering).
            Listening carries on — the next turn starts at that moment.
  Cancel    aborts the answer (closes the provider's HTTP stream).
  ■ Stop    closes both devices; audio already captured still belongs to the
            open turn, so Answer after Stop works.

Threads: two audio readers → per-channel chunk buffers (`_cap_lock`) → one
STT worker per channel → ONE turn-manager thread that owns turns, the
transcript and history (`_mq`), so nothing races over conversation state.
With speakers the mic hears the interviewer, so the mic is gated while the
loopback is loud (`audio.echo_gate`) and "me" lines that repeat the
interviewer are dropped.
"""
import asyncio
import difflib
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
from .stt import make_stt, voxtype_probe

app = FastAPI()

SR = 16000
MIN_SAMPLES = 8000          # a chunk shorter than 0.5 s isn't worth transcribing
CUT_WINDOW_SEC = 4.0        # cut a background chunk at the quietest frame in its last 4 s
GATE_HOLD_SEC = 0.3         # echo gate: mic stays muted this long after the loopback quiets
TRIM_PAD_SEC = 0.25         # silence kept around speech when trimming a chunk
KEEPALIVE_SEC = 120         # < telecode's 150 s idle unload, so a local model stays warm

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


def _words(text: str) -> list[str]:
    return [w.strip(".,!?;:'\"-").lower() for w in (text or "").split() if w.strip(".,!?;:'\"-")]


def _is_echo(me: str, interviewer_texts) -> bool:
    """A "me" line that mostly repeats something the interviewer said — the mic
    picking the speakers up (the gate can't catch every edge)."""
    mw = _words(me)
    if len(mw) < 3:
        return False
    for it in interviewer_texts:
        iw = _words(it)
        if not iw:
            continue
        sm = difflib.SequenceMatcher(None, mw, iw, autojunk=False)
        covered = sum(b.size for b in sm.get_matching_blocks())
        if covered / len(mw) >= 0.6:
            return True
    return False


def _rms_frames(audio: np.ndarray, frame: int = 480) -> np.ndarray:
    n = len(audio) // frame
    if n == 0:
        return np.array([float(np.sqrt(np.mean(audio ** 2)))]) if len(audio) else np.zeros(0)
    return np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))


class Copilot:
    def __init__(self):
        self.loop = None
        self.events: asyncio.Queue | None = None
        self.clients: set[WebSocket] = set()
        self.listeners: list = []            # in-process subscribers (overlay)
        self.levels_me = deque(maxlen=18)    # live mic level (you)
        self.levels_int = deque(maxlen=18)   # live loopback level (interviewer)

        self.profiles = profiles_mod.list_profiles()
        cfg_prof = CFG.get("profile")
        self.active_profile = cfg_prof if cfg_prof in self.profiles else next(iter(self.profiles), None)
        self.chain = ProviderChain(CFG.get("llm", {}))
        self._stt_error = None      # why STT failed to init, if it did
        self.audio = None
        self._read_audio_cfg()

        # one STT backend per channel (the built-in model is shared between them)
        self.stt_int = None
        self.stt_me = None
        self._stt_cfg_key = None    # detects a real STT settings change
        self._failing_over = False  # one VoxType→local swap at a time
        self._q_int: queue.Queue = queue.Queue()
        self._q_me: queue.Queue = queue.Queue()

        # capture side — touched by the audio reader threads, guarded by _cap_lock
        self._cap_lock = threading.Lock()
        self._cap = {"interviewer": {"frames": [], "t0": 0.0},
                     "candidate":  {"frames": [], "t0": 0.0}}
        self._turn = 1                       # the open turn's id
        self._submitted: dict[int, dict[str, int]] = {}   # turn -> channel -> chunks sent to STT
        self._int_loud_until = 0.0           # echo gate
        self.listening = False

        # turn manager — the only thread that touches turns, transcript, history
        self._mq: queue.Queue = queue.Queue()
        self._lines: dict[int, list] = {}    # turn -> [(t0, speaker, text)]
        self._received: dict[int, dict[str, int]] = {}
        self._closing: dict[int, tuple] = {} # turn -> (mode, submitted counts)
        self._recent_int: deque = deque(maxlen=12)   # interviewer lines, for echo checks
        self._pending = 0                    # chunks waiting for STT (for the HUD)

        # sessions/tabs — each is an independent conversation with its own
        # transcript + exchange history. The active session's context is what
        # the AI answers from.
        self._sid = 0
        self.sessions: list[dict] = [self._blank_session()]
        self.active = 0
        self.qid = 0
        self.cancel_event: threading.Event | None = None
        self.answering = False
        self._last_llm = 0.0

    def _read_audio_cfg(self):
        a = CFG.get("audio", {}) or {}
        self.chunk_sec = max(8.0, min(28.0, float(a.get("chunk_seconds", 20))))
        self.echo_gate = bool(a.get("echo_gate", True))
        self.speech_threshold = float(a.get("speech_threshold", 0.02))

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

    # session changes run on the manager thread too, so they never interleave
    # with a turn being composed
    def new_session(self):
        self._mq.put(("call", self._new_session))

    def switch_session(self, idx: int):
        self._mq.put(("call", lambda: self._switch_session(idx)))

    def close_session(self, idx: int):
        self._mq.put(("call", lambda: self._close_session(idx)))

    def clear(self):
        self._mq.put(("call", self._clear))

    def _new_session(self):
        self.sessions.append(self._blank_session())
        self.active = len(self.sessions) - 1
        self._post_sessions(); self._post_active()

    def _switch_session(self, idx: int):
        if 0 <= idx < len(self.sessions) and idx != self.active:
            self.active = idx
            self._post_sessions(); self._post_active()

    def _close_session(self, idx: int):
        if len(self.sessions) <= 1 or not (0 <= idx < len(self.sessions)):
            return
        self.sessions.pop(idx)
        self.active = min(self.active if idx > self.active else self.active - 1,
                          len(self.sessions) - 1)
        self.active = max(0, self.active)
        self._post_sessions(); self._post_active()

    def _clear(self):
        self.transcript.clear()
        self.exchanges.clear()
        self.status("Session cleared — history is empty.")
        self.post({"type": "cleared"})

    # ---------- lifecycle ----------

    def startup(self, loop):
        self.loop = loop
        self.events = asyncio.Queue()
        threading.Thread(target=self._manager, daemon=True, name="turn-manager").start()
        threading.Thread(target=self._keepalive, daemon=True, name="llm-keepalive").start()
        threading.Thread(target=self._init_stt, daemon=True, name="stt-init").start()

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

    def _post_state(self):
        self.post({"type": "state", "listening": self.listening,
                   "answering": self.answering, "pending": self._pending})

    def _stt_key(self) -> str:
        return json.dumps(CFG.get("stt", {}) or {}, sort_keys=True)

    def _stt_label(self) -> str:
        dev = getattr(self.stt_int, "device", "?")
        if dev == "voxtype":
            return "VoxType speech API"
        model = (CFG.get("stt", {}) or {}).get("model") or "openai/whisper-large-v3"
        return f"Whisper {model} on {dev.upper()}"

    def _init_stt(self):
        """Build both channels' backends — VoxType's API when it's up, otherwise
        our own Whisper (`stt.engine` decides; see stt.make_stt). Safe to call
        again after a settings change: the STT workers start only once and
        read the current backend per chunk."""
        stt_cfg = CFG.get("stt", {}) or {}
        self._stt_cfg_key = self._stt_key()
        try:
            # one backend per channel: VoxType gets two HTTP clients; the built-in
            # model hands both channels the same shared instance (one copy in RAM)
            self.stt_int = make_stt(stt_cfg, self.status)
            self.stt_me = make_stt(stt_cfg)
            self._stt_error = None
        except Exception as e:
            self.stt_int = self.stt_me = None
            self._stt_error = str(e)
            self.status(f"Speech engine unavailable: {e}", "error")
            return
        if stt_cfg.get("device", "cuda") == "cuda" and getattr(self.stt_int, "device", "") == "cpu":
            self.status(f"GPU unavailable — using CPU (slow for {stt_cfg.get('model') or 'openai/whisper-large-v3'}). "
                        f"{self.stt_int.fallback_reason}", "error")
        self._start_stt_workers()
        self.status(f"Ready — {self._stt_label()}. Press ▶ Listen to start.")

    def _start_stt_workers(self):
        if getattr(self, "_workers_up", False):
            return
        self._workers_up = True
        threading.Thread(target=self._stt_worker, args=("interviewer", self._q_int),
                         daemon=True, name="stt-interviewer").start()
        threading.Thread(target=self._stt_worker, args=("candidate", self._q_me),
                         daemon=True, name="stt-candidate").start()

    # ---------- listening (buttons) ----------

    def start_listening(self):
        if self.listening:
            return
        try:
            from .audio import AudioEngine
            a = CFG.get("audio", {}) or {}
            self.audio = AudioEngine(self._on_chunk,
                                     capture_mic=a.get("capture_mic", True),
                                     capture_system=a.get("capture_system_audio", True))
            self.audio.start()
        except Exception as e:
            self.audio = None
            self.status(f"Audio capture unavailable: {e}. Typed questions still work.", "error")
            return
        self.listening = True
        self.post({"type": "devices", "devices": self.audio.devices, "errors": self.audio.errors})
        for ch, err in self.audio.errors.items():
            self.status(f"Audio channel '{ch}' unavailable: {err}", "error")
        desc = ", ".join(f"{k} = {v}" for k, v in self.audio.devices.items()) or "no devices"
        self.status(f"Listening — {desc}. Press Answer when you want help.")
        self._post_state()
        threading.Thread(target=self._warm_llm, daemon=True, name="llm-warmup").start()

    def stop_listening(self):
        if not self.listening:
            return
        self.listening = False
        aud, self.audio = self.audio, None
        if aud is not None:
            aud.stop()
        with self._cap_lock:                 # what was heard still belongs to the turn
            for ch in self._cap:
                self._cut(ch, at_quiet=False)
        self.levels_me.clear(); self.levels_int.clear()
        self.status("Stopped listening. Answer still uses what was captured.")
        self._post_state()

    def toggle_listening(self):
        self.stop_listening() if self.listening else self.start_listening()

    def _on_chunk(self, channel: str, samples: np.ndarray):
        """Audio reader threads. Meters, echo gate, and the per-channel chunk
        buffer (cut in the background every `chunk_sec`)."""
        lvl = min(1.0, float(np.sqrt(np.mean(samples ** 2))) * 14.0)
        now = time.time()
        if channel == "interviewer":
            self.levels_int.append(lvl)
            if lvl >= self.speech_threshold:
                self._int_loud_until = now + GATE_HOLD_SEC
        else:
            self.levels_me.append(lvl)
            if self.echo_gate and now < self._int_loud_until:
                samples = np.zeros_like(samples)   # the mic is hearing the speakers
        if not self.listening:
            return
        with self._cap_lock:
            cb = self._cap[channel]
            if not cb["frames"]:
                cb["t0"] = now - len(samples) / SR
            cb["frames"].append(samples)
            if sum(len(f) for f in cb["frames"]) >= self.chunk_sec * SR:
                self._cut(channel, at_quiet=True)

    def _cut(self, channel: str, at_quiet: bool):
        """Send the channel's buffered audio to STT as one chunk of the open
        turn. `at_quiet` cuts at the quietest frame of the last few seconds (so
        a word isn't split) and keeps the rest buffered. Caller holds _cap_lock."""
        cb = self._cap[channel]
        frames = cb["frames"]
        if not frames:
            return
        k = len(frames)
        if at_quiet:
            lo = max(0, k - int(CUT_WINDOW_SEC * SR / max(1, len(frames[-1]))))
            rms = [float(np.sqrt(np.mean(f ** 2))) for f in frames[lo:]]
            k = lo + int(np.argmin(rms)) + 1
        chunk, rest = frames[:k], frames[k:]
        audio = np.concatenate(chunk)
        t0 = cb["t0"]
        cb["frames"] = rest
        cb["t0"] = t0 + len(audio) / SR
        sub = self._submitted.setdefault(self._turn, {"interviewer": 0, "candidate": 0})
        sub[channel] += 1
        self._pending += 1
        (self._q_int if channel == "interviewer" else self._q_me).put((self._turn, t0, audio))
        self._post_state()

    def _stt_worker(self, channel: str, q: queue.Queue):
        """One per channel. Every chunk produces exactly one result (possibly
        empty) so the turn manager can tell when a turn is fully transcribed."""
        while True:
            turn, t0, audio = q.get()
            text = ""
            trimmed, lead = self._trim_silence(audio)
            t0 += lead                       # order lines by when speech began, not the chunk
            stt = self.stt_int if channel == "interviewer" else self.stt_me
            if trimmed is not None and stt is not None:
                try:
                    text = stt.transcribe(trimmed)
                except Exception as e:
                    self.status(f"Transcription failed — {e}", "error")
                    self._maybe_failover(stt)
            elif trimmed is not None:
                self.status(f"Speech model not ready — {self._stt_error or 'loading…'}", "error")
            self._mq.put(("text", channel, turn, t0, (text or "").strip()))

    def _trim_silence(self, audio: np.ndarray):
        """(speech, seconds of leading silence cut). Speech is None if the chunk
        has none at all (Whisper invents "Thank you." on silence)."""
        if len(audio) < MIN_SAMPLES:
            return None, 0.0
        lv = _rms_frames(audio) * 14.0
        loud = np.flatnonzero(lv >= self.speech_threshold)
        if loud.size == 0:
            return None, 0.0
        pad = int(TRIM_PAD_SEC * SR / 480)
        a = max(0, loud[0] - pad) * 480
        b = min(len(audio), (loud[-1] + 1 + pad) * 480)
        out = audio[a:b]
        out = out if len(out) >= MIN_SAMPLES else np.pad(out, (0, MIN_SAMPLES - len(out)))
        return out, a / SR

    def _maybe_failover(self, stt):
        """engine=auto and VoxType vanished mid-interview (app closed, engine
        crash) → load the built-in model once, in the background, instead of
        losing transcription for the rest of the session."""
        cfg = CFG.get("stt", {}) or {}
        if getattr(stt, "device", "") != "voxtype" or self._failing_over:
            return
        if (cfg.get("engine") or "auto").strip().lower() != "auto":
            return
        if voxtype_probe(cfg.get("voxtype_url") or "")[0]:
            return                       # still up — that was a one-off error
        self._failing_over = True
        self.status("VoxType is gone — switching to the built-in speech model…", "error")
        threading.Thread(target=self._swap_to_local, args=(cfg,), daemon=True,
                         name="stt-failover").start()

    def _swap_to_local(self, cfg: dict):
        local = dict(cfg, engine="local")
        try:
            self.stt_int = make_stt(local, self.status)
            self.stt_me = make_stt(local)
            self._stt_error = None
            # we're now running something other than what `stt` asks for, so let
            # the next Save & Apply re-select (picks VoxType up again if it's back)
            self._stt_cfg_key = None
            self.status(f"Transcribing with {self._stt_label()}.")
        except Exception as e:
            self._stt_error = str(e)
            self.status(f"Built-in speech model failed to load: {e}", "error")
        finally:
            self._failing_over = False

    # ---------- turns (buttons) ----------

    def ask_now(self):
        self.close_turn("answer")

    def close_turn(self, mode: str):
        """Answer: cut both channels and close the open turn; the manager sends
        it once every chunk of it is transcribed."""
        with self._cap_lock:
            for ch in self._cap:
                self._cut(ch, at_quiet=False)
            turn = self._turn
            self._turn += 1
            sub = dict(self._submitted.pop(turn, {"interviewer": 0, "candidate": 0}))
        self._mq.put(("close", turn, mode, sub))

    def cancel(self):
        if self.cancel_event:
            self.cancel_event.set()

    # ---------- turn manager (single owner of conversation state) ----------

    def _manager(self):
        while True:
            ev = self._mq.get()
            try:
                kind = ev[0]
                if kind == "text":
                    self._on_text(*ev[1:])
                elif kind == "close":
                    _, turn, mode, sub = ev
                    self._closing[turn] = (mode, sub)
                    self._try_finish()
                elif kind == "say":          # typed in the browser UI
                    _, speaker, text, turn = ev
                    self._add_transcript(speaker, text)
                    self._lines.setdefault(turn, []).append((time.time(), speaker, text))
                    if speaker == "interviewer":
                        self._recent_int.append(text)
                elif kind == "call":
                    ev[1]()
            except Exception as e:           # never let one bad event kill the manager
                self.status(f"Internal error: {e}", "error")

    def typed(self, speaker: str, text: str):
        """A line typed in the browser UI joins the open turn like speech does
        (turn id taken now, before an Answer can close it)."""
        with self._cap_lock:
            turn = self._turn
        self._mq.put(("say", speaker, text, turn))

    def _on_text(self, channel: str, turn: int, t0: float, text: str):
        with self._cap_lock:
            self._pending = max(0, self._pending - 1)
        rec = self._received.setdefault(turn, {"interviewer": 0, "candidate": 0})
        rec[channel] += 1
        if _meaningful(text):
            if channel == "interviewer":
                self._recent_int.append(text)
                self._lines.setdefault(turn, []).append((t0, "interviewer", text))
                self._add_transcript("interviewer", text)
            elif not _is_echo(text, self._recent_int):
                self._lines.setdefault(turn, []).append((t0, "me", text))
                self._add_transcript("me", text)
        self._post_state()
        self._try_finish()

    def _try_finish(self):
        """Send closed turns, oldest first, once all their chunks are back."""
        for turn in sorted(self._closing):
            mode, sub = self._closing[turn]
            rec = self._received.get(turn, {"interviewer": 0, "candidate": 0})
            if any(rec.get(ch, 0) < n for ch, n in sub.items()):
                self.status("Transcribing the last few seconds…")
                return                       # keep order: later turns wait
            del self._closing[turn]
            self._received.pop(turn, None)
            lines = sorted(self._lines.pop(turn, []))
            # a "me" line can arrive before the interviewer line it echoes
            ints = [t for _, sp, t in lines if sp == "interviewer"]
            lines = [ln for ln in lines if ln[1] != "me" or not _is_echo(ln[2], ints)]
            self._send_turn(mode, lines)

    @staticmethod
    def _render_lines(lines) -> str:
        out, last = [], None
        for _, sp, text in lines:
            who = "Interviewer" if sp == "interviewer" else "Me"
            if out and who == last:
                out[-1] += " " + text
            else:
                out.append(f"{who}: {text}")
            last = who
        return "\n".join(out)

    def _send_turn(self, mode: str, lines):
        convo = self._render_lines(lines)
        if not convo:
            self.status("Nothing captured since the last answer.")
            return
        ask = ("Give me what to say now. If I already started answering, continue and "
               "improve it rather than repeating it.")
        user = f"Conversation since my last answer:\n{convo}\n\n{ask}"
        self.answer(user, convo, mode)

    def _add_transcript(self, speaker: str, text: str):
        if not text:
            return
        entry = {"speaker": speaker, "text": text, "ts": time.time()}
        self.transcript.append(entry)
        self.post({"type": "transcript", **entry})

    # ---------- answering ----------

    def answer(self, user: str, convo: str, mode: str = "answer"):
        if self.cancel_event:
            self.cancel_event.set()
        cancel = self.cancel_event = threading.Event()
        self.qid += 1
        threading.Thread(target=self._answer, args=(self.qid, user, convo, mode, cancel),
                         daemon=True, name="llm-answer").start()

    def _system_prompt(self):
        self.profiles = profiles_mod.list_profiles()  # pick up prompt edits live
        prof = self.profiles.get(self.active_profile)
        if prof:
            system = profiles_mod.render(prof)
            system += profiles_mod.context_block(self.active_profile)
            temp = (prof.get("meta") or {}).get("temperature")
        else:
            system = "You are a real-time interview assistant. Give concise, speakable answers."
            temp = None
        return system, temp

    def _answer(self, qid: int, user: str, convo: str, mode: str, cancel: threading.Event):
        system, temp = self._system_prompt()
        msgs = [{"role": "system", "content": system}]
        n = int(CFG.get("llm", {}).get("history_exchanges", 12))
        exchanges = list(self.exchanges)
        for ex in exchanges[-n:]:
            msgs.append({"role": "user", "content": ex["q"]})
            msgs.append({"role": "assistant", "content": ex["a"] or "(no suggestion given)"})
        msgs.append({"role": "user", "content": user})

        self.answering = True
        self._post_state()
        self.post({"type": "answer_start", "qid": qid, "mode": mode,
                   "question": convo.splitlines()[-1] if convo else ""})
        acc = []
        stream = self.chain.stream(msgs, temperature=temp)
        try:
            for provider, delta in stream:
                if cancel.is_set():
                    break
                acc.append(delta)
                self.post({"type": "answer_delta", "qid": qid, "text": delta, "provider": provider})
        except Exception as e:
            self.post({"type": "answer_error", "qid": qid, "text": str(e)})
        finally:
            stream.close()                   # cancel → the provider's HTTP stream is dropped
            self._last_llm = time.time()
        cancelled = cancel.is_set()
        self.post({"type": "answer_done", "qid": qid, "cancelled": cancelled})
        if self.cancel_event is cancel:
            self.answering = False
            self._post_state()
        ai_text = "".join(acc).strip()
        if ai_text:
            if cancelled:
                ai_text += " …(cancelled)"
            # history: this turn's conversation → the suggestion I was given
            self._mq.put(("call", lambda: self._record(convo, ai_text)))

    def _record(self, convo: str, ai_text: str):
        self.transcript.append({"speaker": "ai", "text": ai_text, "ts": time.time()})
        self.exchanges.append({"q": convo, "a": ai_text})

    # ---------- keeping a local model warm ----------

    def _warm_llm(self):
        """1-token request on ▶ Listen: a cold local model (telecode loads in
        minutes) loads now, not on the first Answer."""
        try:
            stream = self.chain.stream([{"role": "user", "content": "hi"}], max_tokens=1)
            for _ in stream:
                break
            stream.close()
            self._last_llm = time.time()
        except Exception:
            pass                             # the real Answer reports provider errors

    def _keepalive(self):
        while True:
            time.sleep(15)
            if (self.listening and not self.answering
                    and time.time() - self._last_llm > KEEPALIVE_SEC):
                self._warm_llm()

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
        self._read_audio_cfg()
        # rebuild STT only if its config changed, or if it's currently down /
        # failed over (a local model load is slow, so never do it speculatively)
        # — off-thread, the STT workers stay up
        if self.stt_int is None or self._stt_key() != self._stt_cfg_key:
            threading.Thread(target=self._init_stt, daemon=True, name="stt-rebuild").start()
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
        "listening": CO.listening,
        "answering": CO.answering,
        "transcript": CO.transcript[-200:],
    }, ensure_ascii=False))
    try:
        while True:
            m = json.loads(await ws.receive_text())
            t = m.get("type")
            if t == "listen":
                CO.start_listening() if m.get("on", True) else CO.stop_listening()
            elif t == "answer":
                CO.ask_now()
            elif t == "cancel":
                CO.cancel()
            elif t in ("ask", "say"):        # typed: interviewer's question / what I said
                text = (m.get("text") or "").strip()
                if text:
                    CO.typed("interviewer" if t == "ask" else "me", text)
                    if t == "ask":
                        CO.ask_now()
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
