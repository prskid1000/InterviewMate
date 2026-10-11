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
    # transcription is offline either way — no key.
    #   engine: auto    → use VoxType's embedded speech API if it's running
    #                     (its Whisper is already on the GPU, so we skip loading
    #                     a second copy), else load our own model below
    #           voxtype → VoxType only; error if it isn't reachable
    #           local   → always load our own model
    # local mode: VoxType's transformers Whisper backend, ported (same HF cache,
    # same knobs). model = HF repo id or local path. dtype auto = fp16 on CUDA,
    # fp32 on CPU; falls back to CPU if CUDA is unavailable (slow for large-v3 —
    # use openai/whisper-small.en on CPU-only machines). cpu_threads 0 = torch
    # default (one per physical core). (Audio preprocessing was tried and
    # reverted — it hurt accuracy; Whisper prefers the raw signal.)
    "stt": {"engine": "auto", "voxtype_url": "http://127.0.0.1:6600",
            "language": "en", "model": "openai/whisper-large-v3", "device": "cuda",
            "dtype": "auto", "attn_impl": "auto", "torch_compile": False,
            "warmup": True, "cpu_threads": 0,
            "task": "transcribe", "num_beams": 1, "temperature": 0.0,
            "repetition_penalty": 1.0, "initial_prompt": ""},
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
            {"name": "Telecode local (llama.cpp)", "api_type": "anthropic",
             "base_url": "http://127.0.0.1:1235", "model": "qwen3.6-35b",
             "api_key": "local", "enabled": True},
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
