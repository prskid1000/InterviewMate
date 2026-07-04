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
    """Shared context files — loose files at the top of context/ (not in a
    per-profile subfolder). Exposed as {{stem}} to every profile."""
    out = {}
    if CONTEXT_DIR.exists():
        for p in sorted(CONTEXT_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in (".md", ".txt"):
                try:
                    out[p.stem] = p.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
    return out


# ── per-profile generic context files ────────────────────────────────
# Each profile gets its own folder context/<profile_id>/ holding any number
# of titled files. They are injected into that profile's prompt automatically
# and are also available as {{title}} template vars.

_SAFE = re.compile(r"[^\w.-]+")


def _slug(title: str) -> str:
    s = _SAFE.sub("_", (title or "").strip()).strip("_")
    return s or "untitled"


def profile_ctx_dir(profile_id: str) -> Path:
    return CONTEXT_DIR / profile_id


def list_context_files(profile_id: str) -> list[dict]:
    d = profile_ctx_dir(profile_id)
    out = []
    if d.exists():
        for p in sorted(d.iterdir()):
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8")
                except OSError:
                    content = ""
                out.append({"title": p.stem, "name": p.name, "content": content})
    return out


def save_context_file(profile_id: str, title: str, content: str):
    d = profile_ctx_dir(profile_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{_slug(title)}.md").write_text(content, encoding="utf-8")


def delete_context_file(profile_id: str, title: str):
    p = profile_ctx_dir(profile_id) / f"{_slug(title)}.md"
    try:
        p.unlink()
    except OSError:
        pass


def set_context_files(profile_id: str, files: list[dict]):
    """Replace a profile's context folder with exactly `files` (title/content)."""
    d = profile_ctx_dir(profile_id)
    d.mkdir(parents=True, exist_ok=True)
    keep = set()
    for f in files:
        name = f"{_slug(f.get('title', ''))}.md"
        keep.add(name)
        (d / name).write_text(f.get("content", ""), encoding="utf-8")
    for p in list(d.iterdir()):        # drop removed files
        if p.is_file() and p.name not in keep:
            try:
                p.unlink()
            except OSError:
                pass


def context_block(profile_id: str) -> str:
    """A REFERENCE MATERIALS section built from the profile's context files,
    appended to the system prompt so uploads work without editing the body."""
    files = list_context_files(profile_id)
    if not files:
        return ""
    parts = ["\n\n--- REFERENCE MATERIALS ---"]
    for f in files:
        body = (f["content"] or "").strip()
        if body:
            parts.append(f"\n## {f['title']}\n{body}")
    return "".join(parts) if len(parts) > 1 else ""


def save_profile(profile_id: str, name: str, body: str, meta: dict | None = None):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    fm = dict(meta or {})
    fm["name"] = name or profile_id
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    (PROFILE_DIR / f"{_slug(profile_id)}.md").write_text(
        f"---\n{front}\n---\n\n{body.strip()}\n", encoding="utf-8")


def delete_profile(profile_id: str):
    try:
        (PROFILE_DIR / f"{_slug(profile_id)}.md").unlink()
    except OSError:
        pass
    d = profile_ctx_dir(profile_id)          # remove its context folder too
    if d.exists():
        for p in list(d.iterdir()):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def render(profile: dict, extra_vars: dict | None = None) -> str:
    pid = profile.get("id", "")
    per_profile = {f["title"]: f["content"].strip() for f in list_context_files(pid)}
    variables = {**context_vars(), **per_profile, **(extra_vars or {})}
    return _VAR.sub(lambda m: str(variables.get(m.group(1), "")), profile["body"])
