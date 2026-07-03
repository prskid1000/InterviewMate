"""Prompt profiles: markdown files in profiles/ with YAML frontmatter.
Template variables {{name}} come from config.yaml `vars:` plus files in
context/ (context/resume.md -> {{resume}})."""
import re
from pathlib import Path

import yaml

from .config import ROOT

PROFILE_DIR = ROOT / "profiles"
CONTEXT_DIR = ROOT / "context"

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_VAR = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


def _parse(path: Path):
    text = path.read_text(encoding="utf-8")
    meta = {}
    m = _FRONTMATTER.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        text = text[m.end():]
    return meta, text.strip()


def list_profiles() -> dict:
    out = {}
    for p in sorted(PROFILE_DIR.glob("*.md")):
        meta, body = _parse(p)
        out[p.stem] = {"id": p.stem, "name": meta.get("name", p.stem),
                       "meta": meta, "body": body}
    return out


def context_vars() -> dict:
    out = {}
    if CONTEXT_DIR.exists():
        for p in sorted(CONTEXT_DIR.iterdir()):
            if p.suffix.lower() in (".md", ".txt"):
                try:
                    out[p.stem] = p.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
    return out


def render(profile: dict, extra_vars: dict | None = None) -> str:
    variables = {**context_vars(), **(extra_vars or {})}
    return _VAR.sub(lambda m: str(variables.get(m.group(1), "")), profile["body"])
