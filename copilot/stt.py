"""Speech-to-text backends.

Two engines matter in practice (picked by `stt.engine` in config.yaml):

  voxtype — VoxType's embedded OpenAI-compatible server (default :6600). Its
            Whisper is already resident on the GPU, so we reuse it instead of
            loading a second copy of large-v3 into VRAM.
  local   — our own Whisper via HF transformers (VoxType's backend, ported:
            large-v3 on CUDA fp16, CPU fp32 fallback; shares VoxType's HF cache).

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


_WHISPER_SIZES = {"tiny", "base", "small", "medium", "large", "large-v1", "large-v2",
                  "large-v3", "large-v3-turbo", "turbo"}


def _hf_model_id(model: str) -> str:
    """Bare Whisper size (the old faster-whisper names: large-v3, small.en, …)
    -> its openai/ HF repo. Repo ids and local paths pass through unchanged."""
    m = (model or "large-v3").strip()
    if "/" in m or "\\" in m or os.path.isdir(m):
        return m
    if m == "turbo":
        m = "large-v3-turbo"
    if m in _WHISPER_SIZES or (m.endswith(".en") and m[:-3] in _WHISPER_SIZES):
        return f"openai/whisper-{m}"
    return m


class LocalSTT:
    """Offline Whisper via HF transformers — a port of VoxType's
    `_WhisperHandler`, so it shares VoxType's HF cache (no second download of
    a model VoxType already pulled) and the same knobs: device, dtype,
    attention impl, torch.compile, warm-up, task / beams / temperature /
    repetition penalty / initial prompt. Falls back to CPU if CUDA is
    unavailable. Both channels share ONE instance (see `_shared_local`)."""

    # Whisper's encoder is hard-wired to a 30 s receptive field: the feature
    # extractor pads OR TRUNCATES every input to 3000 mel frames, so a plain
    # generate() silently drops everything past 30 s. Longer audio (we record
    # up to audio.max_record_seconds) must take the sequential long-form path.
    _WINDOW_SEC = 30.0

    def __init__(self, model="openai/whisper-large-v3", language="en", device="cuda",
                 dtype="auto", attn_impl="auto", torch_compile=False, warmup=True,
                 cpu_threads=0, **opts):
        import torch
        from transformers import AutoProcessor, WhisperForConditionalGeneration
        self.lock = threading.Lock()
        self.fallback_reason = None
        self.model_id = _hf_model_id(model)
        self.set_opts(language=language, **opts)
        if int(cpu_threads or 0) > 0:
            torch.set_num_threads(int(cpu_threads))

        want = (device or "cuda").lower()
        on_cuda = want == "cuda" and torch.cuda.is_available()
        if want == "cuda" and not on_cuda:
            self.fallback_reason = "torch reports no usable CUDA device"
        self.device = "cuda" if on_cuda else "cpu"
        pref = (dtype or "auto").lower()
        self.torch_dtype = (torch.bfloat16 if pref == "bf16"
                            else torch.float32 if pref == "fp32" or not on_cuda
                            else torch.float16)

        kw = {"dtype": self.torch_dtype, "low_cpu_mem_usage": True}
        impl = (attn_impl or "auto").lower()
        if impl == "flash_attention_2" and self.torch_dtype == torch.float32:
            impl = "sdpa"                # flash-attn needs fp16/bf16
        if impl in ("sdpa", "flash_attention_2", "eager"):
            kw["attn_implementation"] = impl
        self.processor = _local_first(AutoProcessor.from_pretrained, self.model_id)
        model_ = None
        if on_cuda:
            # straight onto the GPU — skips the CPU materialization + copy
            try:
                model_ = _local_first(WhisperForConditionalGeneration.from_pretrained,
                                      self.model_id, device_map="cuda", **kw)
            except Exception:
                model_ = None
        if model_ is None:
            model_ = _local_first(WhisperForConditionalGeneration.from_pretrained,
                                  self.model_id, **kw).to(self.device)
        self.model = model_.eval()

        if torch_compile:
            # compile forward (what generate() calls each step), not the module
            # wrapper — generate() on a compiled wrapper bypasses it entirely
            eager_fwd = self.model.forward
            try:
                self.model.forward = torch.compile(
                    eager_fwd, mode="reduce-overhead" if on_cuda else "default")
                if not self._warmup():   # compile errors surface on first call
                    self.model.forward = eager_fwd
            except Exception:
                self.model.forward = eager_fwd
        if warmup:
            self._warmup()

    def set_opts(self, language="en", task="transcribe", num_beams=1, temperature=0.0,
                 repetition_penalty=1.0, initial_prompt="", **_):
        """Decode-time options — cheap to change without reloading weights."""
        self.language = (language or "").strip().lower()
        self.task = task or "transcribe"
        self.num_beams = max(1, int(num_beams or 1))
        self.temperature = float(temperature or 0.0)
        self.repetition_penalty = float(repetition_penalty or 1.0)
        self.initial_prompt = str(initial_prompt or "")

    def _warmup(self) -> bool:
        """1-token generate: triggers lazy CUDA-kernel / cuDNN autotuning (and
        any torch.compile failure) at load, not mid-interview."""
        import torch
        try:
            feats = self.processor(np.zeros(16000, np.float32), sampling_rate=16000,
                                   return_tensors="pt").input_features
            with torch.no_grad():
                self.model.generate(feats.to(self.device, dtype=self.torch_dtype),
                                    max_new_tokens=1)
            return True
        except Exception:
            return False

    def transcribe(self, audio: np.ndarray) -> str:
        import torch
        audio = np.asarray(audio, dtype=np.float32)
        if audio.size == 0 or float(np.abs(audio).max()) < 1e-3:
            return ""                    # digital silence — Whisper would hallucinate
        long_form = audio.shape[-1] > int(16000 * self._WINDOW_SEC)
        with self.lock:
            if long_form:
                # all three kwargs are load-bearing: keep the tail, pad to the
                # real length, and tell generate() where the audio ends
                inputs = self.processor(audio, sampling_rate=16000, return_tensors="pt",
                                        truncation=False, padding="longest",
                                        return_attention_mask=True)
            else:
                inputs = self.processor(audio, sampling_rate=16000, return_tensors="pt")
            feats = inputs.input_features.to(self.device, dtype=self.torch_dtype)
            gen: dict = {"task": self.task, "num_beams": self.num_beams}
            if long_form:
                # timestamps find each window's cut point; max_new_tokens is a
                # per-window budget transformers rejects here, so leave it unset
                gen["return_timestamps"] = True
                mask = getattr(inputs, "attention_mask", None)
                if mask is not None:
                    gen["attention_mask"] = mask.to(self.device)
            else:
                gen["max_new_tokens"] = 440
            if self.temperature > 0.0 and self.num_beams == 1:
                gen["do_sample"] = True
                gen["temperature"] = self.temperature
            if self.repetition_penalty > 1.0:
                gen["repetition_penalty"] = self.repetition_penalty
            if self.language and self.language != "auto":
                gen["language"] = self.language
            if self.initial_prompt:
                try:
                    gen["prompt_ids"] = self.processor.get_prompt_ids(
                        self.initial_prompt, return_tensors="pt").to(self.device)
                except Exception:
                    pass
            with torch.no_grad():
                out = self.model.generate(feats, **gen)
            text = self.processor.batch_decode(out, skip_special_tokens=True)[0]
        return (text or "").strip()


def _local_first(loader, *args, **kwargs):
    """HF cache with no network first (skips the per-load ETag check); only
    goes online — and may download — when the model isn't cached yet."""
    try:
        return loader(*args, local_files_only=True, **kwargs)
    except Exception:
        return loader(*args, **kwargs)


_LOAD_KEYS = ("model", "device", "dtype", "attn_impl", "torch_compile", "cpu_threads")
_shared: dict = {}
_shared_lock = threading.Lock()


def _shared_local(cfg: dict) -> LocalSTT:
    """One LocalSTT for both channels — a second copy of large-v3 would double
    RAM/VRAM for nothing (the instance lock serializes them, as VoxType's
    worker does). Reloads only when a load-affecting setting changed; decode
    options are pushed onto the live instance."""
    key = tuple(str(cfg.get(k, "")) for k in _LOAD_KEYS)
    with _shared_lock:
        inst = _shared.get(key)
        if inst is None:
            _shared.clear()              # drop the old weights before loading new ones
            inst = _shared[key] = LocalSTT(**cfg)
        else:
            inst.set_opts(**cfg)
        return inst


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

    local = _local_cfg(cfg)
    say(f"Loading built-in speech model '{local['model']}' on {local['device']} "
        f"({local['dtype']}, beams={local['num_beams']})…")
    return _shared_local(local)


def _local_cfg(cfg: dict) -> dict:
    """The `stt` block -> LocalSTT kwargs (defaults + legacy beam_size)."""
    return {
        "model": cfg.get("model") or "openai/whisper-large-v3",
        "language": cfg.get("language", "en"),
        "device": cfg.get("device") or "cuda",
        "dtype": cfg.get("dtype") or "auto",
        "attn_impl": cfg.get("attn_impl") or "auto",
        "torch_compile": bool(cfg.get("torch_compile", False)),
        "warmup": bool(cfg.get("warmup", True)),
        "cpu_threads": int(cfg.get("cpu_threads") or 0),
        "task": cfg.get("task") or "transcribe",
        "num_beams": int(cfg.get("num_beams") or cfg.get("beam_size") or 1),
        "temperature": float(cfg.get("temperature") or 0.0),
        "repetition_penalty": float(cfg.get("repetition_penalty") or 1.0),
        "initial_prompt": cfg.get("initial_prompt") or "",
    }
