"""Speech-to-text backends.

Two engines matter in practice (picked by `stt.engine` in config.yaml):

  voxtype — VoxType's embedded OpenAI-compatible server (default :6600). Its
            Whisper is already resident on the GPU, so we reuse it instead of
            loading a second copy of large-v3 into VRAM.
  local   — our own faster-whisper (large-v3 on CUDA float16, CPU int8 fallback).

`engine: auto` (the default) probes VoxType and only spins up the local GPU
model when VoxType isn't there. See `make_stt`.

The user-defined cloud provider chain below (openai / gemini api types) is kept
for reference and is not offered in the UI.
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


VOXTYPE_URL = "http://127.0.0.1:6600"


def voxtype_probe(base_url: str = VOXTYPE_URL, timeout: float = 1.5) -> tuple[bool, str]:
    """Is VoxType's embedded STT server up? Cheap GET /health — a few ms on
    localhost, so it's safe to call on every STT (re)build. Returns
    (available, detail); detail is the failure reason, or the engine's
    readiness when it is available."""
    import httpx
    try:
        r = httpx.get(f"{(base_url or VOXTYPE_URL).rstrip('/')}/health", timeout=timeout)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        st = (r.json().get("stt") or {})
    except Exception:
        return False, "unexpected /health payload"
    if st.get("error"):
        return False, str(st["error"])[:200]
    return True, "model resident" if st.get("ready") else "loads on first use"


class VoxTypeSTT:
    """VoxType's embedded OpenAI-compatible STT server (default :6600).

    Nothing is loaded here — VoxType already holds Whisper on the GPU, so this
    costs no extra VRAM and no model-load wait at startup. Concurrent requests
    serialize inside VoxType's engine worker; localhost HTTP framing is a few ms
    against a multi-second utterance, so the two channels still keep up."""

    def __init__(self, base_url=VOXTYPE_URL, language="en", timeout=180.0):
        import httpx
        self.base = (base_url or VOXTYPE_URL).rstrip("/")
        self.language = language or None
        self.device = "voxtype"
        self.fallback_reason = None
        self.http = httpx.Client(timeout=timeout)

    def transcribe(self, audio: np.ndarray) -> str:
        data = {"response_format": "text"}
        if self.language:
            data["language"] = self.language
        r = self.http.post(
            f"{self.base}/v1/audio/transcriptions",
            files={"file": ("utterance.wav", _wav_bytes(audio), "audio/wav")},
            data=data,
        )
        if r.status_code != 200:
            raise RuntimeError(f"VoxType HTTP {r.status_code} — {r.text.strip()[:200]}")
        return r.text.strip()


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


def make_stt(cfg: dict, on_status=None):
    """Build one channel's STT backend from the `stt` config block.

    `engine`: auto (default — VoxType if it's up, else our own GPU model) |
    voxtype (fail loudly if it isn't up) | local (never touch VoxType).
    Offline either way — no API key. `on_status(text)` gets a one-line note
    about which engine won, for the HUD."""
    cfg = cfg or {}
    engine = (cfg.get("engine") or "auto").strip().lower()
    url = cfg.get("voxtype_url") or VOXTYPE_URL
    lang = cfg.get("language", "en")
    say = on_status if callable(on_status) else (lambda *_: None)

    if engine in ("auto", "voxtype"):
        ok, detail = voxtype_probe(url)
        if ok:
            say(f"Using VoxType speech API at {url} — {detail}.")
            return VoxTypeSTT(url, language=lang)
        if engine == "voxtype":
            raise RuntimeError(f"VoxType speech API not reachable at {url} — {detail}")
        say(f"VoxType not running ({detail}) — loading the built-in GPU model instead.")

    model = cfg.get("model", "large-v3")
    device = cfg.get("device", "cuda")
    ctype = cfg.get("compute_type") or ("float16" if device == "cuda" else "int8")
    beam = cfg.get("beam_size", 5)
    say(f"Loading built-in speech model '{model}' on {device} ({ctype}, beam={beam})…")
    return LocalSTT(model=model, language=lang, device=device,
                    compute_type=ctype, beam_size=beam)
