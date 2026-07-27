"""User-defined LLM provider chain — no hardcoded providers.

Each provider in config is fully custom:
  {name, api_type: openai|anthropic, base_url, api_key (or api_key_env), model, enabled}

Providers are tried top-first; a provider that fails before emitting any text
falls through to the next. Works with any OpenAI-compatible endpoint (Gemini,
Groq, OpenRouter, OpenAI, Ollama, vLLM, …) or any Anthropic-compatible endpoint
— including local ones like telecode's dual-protocol proxy on 127.0.0.1:1235.

Local endpoints get a fast TCP preflight (see `local_endpoint_up`) so a
side-app that simply isn't running is skipped instantly instead of stalling an
answer, which is what makes "put the local one on top" safe.
"""
import os
import socket
import time
from urllib.parse import urlparse

from openai import OpenAI, RateLimitError

COOLDOWN_S = 60
LOCAL_PROBE_S = 0.4     # bounded: a closed local port DROPS the SYN here
LOCAL_TIMEOUT_S = 90    # per-request cap for local endpoints (SDK default: 600 s + retries)
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _resolve_key(entry: dict) -> str:
    return (entry.get("api_key") or os.getenv(entry.get("api_key_env", "") or "") or "").strip()


def is_local(base_url: str) -> bool:
    try:
        return (urlparse(base_url).hostname or "").lower() in _LOCAL_HOSTS
    except Exception:
        return False


def local_endpoint_up(base_url: str, timeout: float = LOCAL_PROBE_S) -> bool:
    """Is anything listening on a local base_url? Local side-apps (telecode's
    proxy, an Ollama, …) are often just not running, and on Windows a closed
    port drops the SYN rather than refusing — so the SDKs would retry for
    minutes. Probe first, skip the entry if it's dark."""
    u = urlparse(base_url)
    host = (u.hostname or "127.0.0.1").lower()
    if host in ("localhost", "0.0.0.0"):
        host = "127.0.0.1"      # avoid the IPv6-then-IPv4 double wait
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


class ProviderChain:
    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.max_tokens = int(cfg.get("max_tokens", 900))
        self.temperature = float(cfg.get("temperature", 0.4))
        self.disable_thinking = bool(cfg.get("disable_thinking", True))
        self.thinking_tokens = int(cfg.get("thinking_tokens", 0))
        self.cooldown: dict[str, float] = {}
        self.entries: list[dict] = []
        for p in cfg.get("providers", []):
            if not p.get("enabled", True):
                continue
            name = (p.get("name") or "").strip() or "provider"
            model = (p.get("model") or "").strip()
            base_url = (p.get("base_url") or "").strip()
            api_type = (p.get("api_type") or "openai").strip()
            key = _resolve_key(p)
            if not model or not base_url:
                continue
            self.entries.append({
                "name": name, "model": model, "api_type": api_type,
                "base_url": base_url, "key": key, "client": None,
                "local": is_local(base_url),
            })

    def available(self) -> list[str]:
        return [f"{e['name']}:{e['model']}" for e in self.entries]

    def _client(self, e: dict):
        if e["client"] is not None:
            return e["client"]
        # a local endpoint that hangs must not eat the interview — bound it and
        # let the chain fall through instead of retrying
        kw = {"timeout": LOCAL_TIMEOUT_S, "max_retries": 0} if e["local"] else {}
        if e["api_type"] == "anthropic":
            import anthropic
            e["client"] = anthropic.Anthropic(api_key=e["key"] or "none",
                                              base_url=e["base_url"], **kw)
        else:
            e["client"] = OpenAI(base_url=e["base_url"], api_key=e["key"] or "none", **kw)
        return e["client"]

    def stream(self, messages, temperature=None):
        """Yields (provider_name, text_delta)."""
        if not self.entries:
            raise RuntimeError(
                "No AI provider configured. Open Settings → AI Model and add a "
                "provider (base URL + model + key)."
            )
        temp = self.temperature if temperature is None else float(temperature)
        errors = []
        for e in self.entries:
            if time.time() < self.cooldown.get(e["name"], 0):
                errors.append(f"{e['name']}: cooling down after a rate limit")
                continue
            if e["local"] and not local_endpoint_up(e["base_url"]):
                errors.append(f"{e['name']}: not running ({e['base_url']})")
                continue
            yielded = False
            try:
                gen = (self._stream_anthropic(e, messages, temp)
                       if e["api_type"] == "anthropic"
                       else self._stream_openai(e, messages, temp))
                for tok in gen:
                    yielded = True
                    yield e["name"], tok
                if yielded:
                    return
                errors.append(f"{e['name']}: returned an empty response")
            except RateLimitError:
                self.cooldown[e["name"]] = time.time() + COOLDOWN_S
                errors.append(f"{e['name']}: rate limited (429)")
                if yielded:
                    raise RuntimeError(f"{e['name']} was rate-limited mid-answer")
            except Exception as ex:
                msg = str(ex)
                if "429" in msg or "rate" in msg.lower() or "quota" in msg.lower():
                    self.cooldown[e["name"]] = time.time() + COOLDOWN_S
                errors.append(f"{e['name']}: {type(ex).__name__}: {msg[:120]}")
                if yielded:
                    raise RuntimeError(f"{e['name']} failed mid-answer: {ex}")
        raise RuntimeError("All AI providers failed — " + "; ".join(errors))

    def _reasoning_effort(self):
        """Generic reasoning knob for OpenAI-compatible endpoints (Gemini, o-series,
        DeepSeek, …). None means don't send the param at all."""
        if self.disable_thinking:
            return "none"
        t = self.thinking_tokens
        if t <= 0:
            return None
        return "low" if t < 2048 else "medium" if t < 8192 else "high"

    def _stream_openai(self, e, messages, temp):
        kwargs = dict(model=e["model"], messages=messages, stream=True,
                      max_tokens=self.max_tokens, temperature=temp)
        eff = self._reasoning_effort()
        try:
            resp = self._client(e).chat.completions.create(
                **({**kwargs, "reasoning_effort": eff} if eff else kwargs))
        except Exception:
            if not eff:
                raise
            # provider doesn't accept reasoning_effort — retry without it
            resp = self._client(e).chat.completions.create(**kwargs)
        for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    def _stream_anthropic(self, e, messages, temp):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        conv = [{"role": m["role"], "content": m["content"]}
                for m in messages if m["role"] in ("user", "assistant")]
        kwargs = dict(model=e["model"], max_tokens=self.max_tokens, temperature=temp,
                      system=system or None, messages=conv)
        # extended thinking with an explicit token budget (Anthropic requires
        # max_tokens > budget and temperature = 1 while thinking).
        if not self.disable_thinking and self.thinking_tokens > 0:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self.thinking_tokens}
            kwargs["max_tokens"] = max(self.max_tokens, self.thinking_tokens + 512)
            kwargs["temperature"] = 1.0
        with self._client(e).messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
