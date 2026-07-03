import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


CONFIG_PATH = ROOT / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CFG = load_config()


def save_config(cfg: dict | None = None) -> None:
    """Persist config to disk and refresh the in-memory CFG in place.
    Note: rewrites config.yaml via yaml.safe_dump (inline comments are lost)."""
    data = cfg if cfg is not None else CFG
    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, CONFIG_PATH)
    if data is not CFG:
        CFG.clear()
        CFG.update(data)
