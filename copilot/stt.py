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


_CUDA_DLL_READY = False


def _ensure_cuda_dll_path():
    """Windows: register the pip-installed CUDA 12 runtime DLL directories
    (nvidia-cublas-cu12 / nvidia-cudnn-cu12 / nvidia-cuda-nvrtc-cu12) so
    ctranslate2 can load cublas64_12.dll etc. at runtime. os.add_dll_directory
    alone isn't enough — ctranslate2 loads cuBLAS via the classic search path,
    so we also prepend the dirs to PATH. Idempotent."""
    global _CUDA_DLL_READY
    if _CUDA_DLL_READY:
        return
    try:
        import nvidia
        base = list(getattr(nvidia, "__path__", []) or [])
    except Exception:
        base = []
    dirs = []
    if base:
        for sub in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            d = os.path.join(base[0], sub, "bin")
            if os.path.isdir(d):
                dirs.append(d)
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    _CUDA_DLL_READY = True


class LocalSTT:
    """Offline faster-whisper. Default large-v3 on the GPU (device="cuda",
    float16): large-v3 accuracy AND far faster than realtime (~13x on an
    RTX 5070 Ti after a one-time kernel JIT absorbed by the init warm-up). If
    CUDA is unavailable (no GPU / missing CUDA libs / OOM), it falls back to CPU
    int8 automatically (functional but slow for large-v3 — set a smaller
    stt.model for CPU-only machines). No API key. Loaded + warmed at init."""

    def __init__(self, model="large-v3", language="en", device="cuda",
                 compute_type=None, beam_size=5):
        from faster_whisper import WhisperModel
        self.language = language or None
        self.beam_size = int(beam_size)
        self.lock = threading.Lock()
        self.fallback_reason = None

        def _build(dev, ct):
            if dev == "cuda":
                _ensure_cuda_dll_path()
            m = WhisperModel(model, device=dev, compute_type=ct)
            # warm up — forces the CUDA kernel JIT and surfaces any missing-lib
            # error NOW (at init) so we can fall back cleanly, not mid-interview
            list(m.transcribe(np.zeros(16000, np.float32),
                              language=self.language, beam_size=1)[0])
            return m

        want_dev = (device or "cuda").lower()
        want_ct = compute_type or ("float16" if want_dev == "cuda" else "int8")
        try:
            self.model = _build(want_dev, want_ct)
            self.device, self.compute_type = want_dev, want_ct
        except Exception as e:
            if want_dev == "cuda":
                self.fallback_reason = str(e)
                self.model = _build("cpu", "int8")     # GPU unavailable → CPU
                self.device, self.compute_type = "cpu", "int8"
            else:
                raise

    def transcribe(self, audio: np.ndarray) -> str:
        with self.lock:
            segments, _ = self.model.transcribe(
                audio, language=self.language, beam_size=self.beam_size,
                vad_filter=True)
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
    """STT is local Whisper (large-v3 on GPU, CPU fallback) — offline, no API
    key. The cloud STT classes above are kept for reference but unused."""
    cfg = cfg or {}
    return LocalSTT(model=cfg.get("model", "large-v3"), language=cfg.get("language", "en"),
                    device=cfg.get("device", "cuda"),
                    compute_type=cfg.get("compute_type"),
                    beam_size=cfg.get("beam_size", 5))
