import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


CONFIG_PATH = ROOT / "config.yaml"

# Written on first run if config.yaml is missing, so a fresh clone works with no
# setup and config.yaml can stay gitignored (keys never get committed).
DEFAULT_CONFIG = {
    "stt": {"language": "en"},   # transcription is local Whisper (CPU) — no key
    "overlay": {"enabled": True, "exclude_from_capture": True, "opacity": 1.0},
    "hotkeys": {
        "record_toggle": "ctrl+space", "open_config": "ctrl+alt+c",
        "toggle_answer": "ctrl+alt+h", "toggle_hud": "ctrl+alt+b", "quit": "ctrl+alt+q",
    },
    "audio": {
        "capture_mic": True, "capture_system_audio": True, "max_record_seconds": 180,
        "auto_silence": False, "silence_seconds": 1.2, "speech_threshold": 0.02,
    },
    "llm": {
        "providers": [
            {"name": "Gemini", "api_type": "openai",
             "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
             "model": "gemini-2.5-flash-lite", "api_key_env": "GEMINI_API_KEY", "enabled": True},
            {"name": "Groq", "api_type": "openai",
             "base_url": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "api_key_env": "GROQ_API_KEY", "enabled": False},
        ],
        "history_exchanges": 12, "max_tokens": 900, "temperature": 0.4,
        "disable_thinking": True, "thinking_tokens": 0,
    },
    "profile": "Interview",
    "persona": "",
    "server": {"host": "127.0.0.1", "port": 8765},
}


def save_config(cfg: dict | None = None) -> None:
    """Persist config to disk and refresh the in-memory CFG in place.
    Note: rewrites config.yaml via yaml.safe_dump (inline comments are lost)."""
    data = cfg if cfg is not None else globals().get("CFG", {})
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, CONFIG_PATH)
    cfg_live = globals().get("CFG")     # may not exist yet during first-run bootstrap
    if cfg_live is not None and data is not cfg_live:
        cfg_live.clear()
        cfg_live.update(data)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)      # bootstrap a fresh install
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CFG = load_config()
