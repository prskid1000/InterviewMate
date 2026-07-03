"""User-defined LLM provider chain — no hardcoded providers.

Each provider in config is fully custom:
  {name, api_type: openai|anthropic, base_url, api_key (or api_key_env), model, enabled}

Providers are tried top-first; a provider that fails before emitting any text
falls through to the next. Works with any OpenAI-compatible endpoint (Gemini,
Groq, OpenRouter, OpenAI, Ollama, vLLM, …) or any Anthropic-compatible endpoint.
"""
import os
import time

from openai import OpenAI, RateLimitError

COOLDOWN_S = 60


def _resolve_key(entry: dict) -> str:
    return (entry.get("api_key") or os.getenv(entry.get("api_key_env", "") or "") or "").strip()


class ProviderChain:
    def __init__(self, cfg: dict):
        cfg = cfg or {}
        self.max_tokens = int(cfg.get("max_tokens", 900))
        self.temperature = float(cfg.get("temperature", 0.4))
        self.disable_thinking = bool(cfg.get("disable_thinking", True))
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
            })

    def available(self) -> list[str]:
        return [f"{e['name']}:{e['model']}" for e in self.entries]

    def _client(self, e: dict):
        if e["client"] is not None:
            return e["client"]
        if e["api_type"] == "anthropic":
            import anthropic
            e["client"] = anthropic.Anthropic(api_key=e["key"] or "none", base_url=e["base_url"])
        else:
            e["client"] = OpenAI(base_url=e["base_url"], api_key=e["key"] or "none")
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

    def _stream_openai(self, e, messages, temp):
        kwargs = dict(model=e["model"], messages=messages, stream=True,
                      max_tokens=self.max_tokens, temperature=temp)
        # Gemini 2.5 thinks by default — turn it off for lower latency.
        if self.disable_thinking and "generativelanguage.googleapis" in e["base_url"]:
            kwargs["reasoning_effort"] = "none"
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
        with self._client(e).messages.stream(
            model=e["model"], max_tokens=self.max_tokens, temperature=temp,
            system=system or None, messages=conv,
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
