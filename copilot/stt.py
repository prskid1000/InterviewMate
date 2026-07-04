"""User-defined speech-to-text provider chain — no hardcoded providers.

Each STT provider in config is custom:
  {name, api_type: local|openai|gemini, base_url, api_key (or api_key_env),
   model, enabled}

  local  — faster-whisper, offline & unlimited (model = tiny|base|small|…)
  openai — any OpenAI-compatible /audio/transcriptions endpoint (Groq, OpenAI,
           self-hosted whisper) — base_url + key + model
  gemini — Gemini generateContent with inline audio — base_url + key + model

Providers are tried top-first; the first that returns text wins. Heavy local
models are built lazily on first use.
"""
import base64
import io
import os
import threading
import wave

import numpy as np


def _resolve_key(entry: dict) -> str:
    return (entry.get("api_key") or os.getenv(entry.get("api_key_env", "") or "") or "").strip()


def _wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class LocalSTT:
    """Offline faster-whisper, CPU only. Default model base.en — the accuracy/
    latency sweet spot on CPU (~0.7s for a short clip) and portable to any
    laptop (no GPU/CUDA, no API key). The model is loaded + warmed at init."""

    def __init__(self, model="base.en", language="en"):
        from faster_whisper import WhisperModel
        self.language = language or None
        self.lock = threading.Lock()
        self.model = WhisperModel(model, device="cpu", compute_type="int8")
        self.device = "cpu"
        # warm up so the first real transcription isn't slow
        try:
            list(self.model.transcribe(np.zeros(8000, np.float32),
                                       language=self.language, beam_size=1)[0])
        except Exception:
            pass

    def transcribe(self, audio: np.ndarray) -> str:
        with self.lock:
            segments, _ = self.model.transcribe(
                audio, language=self.language, beam_size=1, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()


class OpenAISTT:
    def __init__(self, base_url, api_key, model, language="en"):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url, api_key=api_key or "none")
        self.model = model
        self.language = language or None

    def transcribe(self, audio: np.ndarray) -> str:
        kwargs = {"model": self.model,
                  "file": ("utterance.wav", io.BytesIO(_wav_bytes(audio)))}
        if self.language:
            kwargs["language"] = self.language
        r = self.client.audio.transcriptions.create(**kwargs)
        return (r.text or "").strip()


class GeminiSTT:
    def __init__(self, base_url, api_key, model, language="en"):
        import httpx
        if not api_key:
            raise RuntimeError("Gemini needs an API key — add it in Settings → STT (or set GEMINI_API_KEY in .env)")
        self.base = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.key = api_key
        self.model = model or "gemini-2.5-flash-lite"
        self.language = language or "en"
        self.http = httpx.Client(timeout=60)

    def transcribe(self, audio: np.ndarray) -> str:
        b64 = base64.b64encode(_wav_bytes(audio)).decode()
        prompt = (f"Transcribe this audio verbatim (language: {self.language}). "
                  "Return ONLY the spoken words with punctuation — no commentary.")
        r = self.http.post(
            f"{self.base}/models/{self.model}:generateContent",
            params={"key": self.key},
            json={"contents": [{"parts": [
                {"inline_data": {"mime_type": "audio/wav", "data": b64}},
                {"text": prompt}]}],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}},
        )
        if r.status_code != 200:
            # surface Gemini's real reason (invalid key / model / payload)
            try:
                msg = r.json().get("error", {}).get("message", "") or r.text
            except Exception:
                msg = r.text
            raise RuntimeError(f"HTTP {r.status_code} — {msg.strip()[:300]}")
        data = r.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            return ""
        return " ".join(p.get("text", "") for p in parts).strip()


class SttChain:
    """Tries enabled STT providers in order; first non-empty result wins.
    Backends are built lazily so a heavy local model only loads if reached."""

    def __init__(self, providers: list[dict], language="en"):
        self._specs = []
        for p in providers:
            if not p.get("enabled", True):
                continue
            self._specs.append({
                "name": (p.get("name") or p.get("api_type") or "stt").strip(),
                "api_type": (p.get("api_type") or "local").strip(),
                "base_url": (p.get("base_url") or "").strip(),
                "key": _resolve_key(p),
                "model": (p.get("model") or "").strip(),
                "backend": None,
            })
        self.language = language
        self.device = self._specs[0]["name"] if self._specs else "none"

    def _build(self, spec):
        t = spec["api_type"]
        base = spec["base_url"]
        # Gemini has no OpenAI-style /audio/transcriptions endpoint — always use
        # the native generateContent path, whatever api_type was picked. This
        # prevents a 404 when a Gemini base is added as an "openai" provider.
        if "generativelanguage.googleapis" in base:
            native = base.split("/openai")[0].rstrip("/")   # .../v1beta/openai/ -> .../v1beta
            return GeminiSTT(native, spec["key"], spec["model"] or "gemini-2.5-flash-lite",
                             language=self.language)
        if t == "local":
            return LocalSTT(model=spec["model"] or "base", language=self.language)
        if t == "openai":
            return OpenAISTT(spec["base_url"], spec["key"], spec["model"] or "whisper-large-v3-turbo",
                             language=self.language)
        if t == "gemini":
            return GeminiSTT(spec["base_url"], spec["key"], spec["model"] or "gemini-2.5-flash-lite",
                             language=self.language)
        raise RuntimeError(f"unknown STT api_type: {t}")

    def transcribe(self, audio: np.ndarray) -> str:
        if not self._specs:
            raise RuntimeError("No speech-to-text provider configured (Settings → Speech-to-text).")
        errors = []
        for spec in self._specs:
            try:
                if spec["backend"] is None:
                    spec["backend"] = self._build(spec)
                    self.device = getattr(spec["backend"], "device", spec["name"])
                return spec["backend"].transcribe(audio)
            except Exception as ex:
                errors.append(f"{spec['name']}: {str(ex)[:300]}")
        raise RuntimeError("All STT providers failed — " + "; ".join(errors))


def make_stt(cfg: dict):
    """STT is fixed to local CPU Whisper (base.en) — offline, no API key,
    portable. The cloud STT classes above are kept for reference but unused."""
    cfg = cfg or {}
    return LocalSTT(model=cfg.get("model", "base.en"), language=cfg.get("language", "en"))
