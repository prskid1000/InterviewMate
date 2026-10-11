"""Native settings window — summoned by a global hotkey (no tray icon).

Providers are fully user-defined: add any number of OpenAI-compatible or
Anthropic-compatible AI models, and any number of speech-to-text providers
(local Whisper, OpenAI-compatible audio, or Gemini). Nothing is hardcoded —
each provider is a name + api type + base URL + model + key. Save & Apply
reconfigures the live app. Native frame → resizable from every edge/corner.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from . import profiles as profiles_mod
from .config import ROOT, CFG, save_config
from .qt_theme_local import QSS
from .uikit import apply_capture_exclusion, load_state, save_state

CONTEXT_DIR = ROOT / "context"

SECTIONS = [("model", "🧠   AI Model"), ("interview", "💬   Interview"),
            ("speech", "🎙   Speech"), ("behavior", "🎛   Overlay & Audio"),
            ("about", "⌨   Hotkeys")]

STT_ENGINES = [("auto", "Auto (recommended)"), ("voxtype", "VoxType API only"),
               ("local", "Built-in model only")]

# Presets only PREFILL a new provider row — they are not a fixed chain.
# Presets list only FREE-tier providers. Paid ones (OpenAI, Anthropic, …) are
# added via "Custom…" — pick the api type and paste the base URL + model + key.
LLM_PRESETS = {
    # local first: telecode's dual-protocol proxy in front of llama.cpp. Costs
    # nothing, no quota, and the chain skips it in ~0.4 s when it isn't running,
    # so it's safe to leave at the top with a cloud provider underneath.
    "Telecode local (llama.cpp)": ("anthropic", "http://127.0.0.1:1235", "claude-sonnet-5"),
    "Gemini (free)": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash-lite"),
    "Groq (free)": ("openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "OpenRouter (free)": ("openai", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
    "Custom… (OpenAI / Anthropic)": ("openai", "", ""),
}
STT_PRESETS = {
    "Gemini (free)": ("gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash-lite"),
    "Groq Whisper (free)": ("openai", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo"),
    "Custom…": ("openai", "", ""),
}

# Everything else (listen, answer, cancel, settings, quit) is a HUD
# button or menu item — the one global hotkey just shows / hides the overlay.
HK_ACTIONS = [("toggle_hud", "Show / hide the overlay")]

_MODS = {"ctrl_l": "ctrl", "ctrl_r": "ctrl", "alt_l": "alt", "alt_r": "alt",
         "alt_gr": "alt", "shift": "shift", "shift_l": "shift", "shift_r": "shift",
         "cmd": "win", "cmd_l": "win", "cmd_r": "win"}


def _keyname(k) -> str:
    if hasattr(k, "name") and k.name:
        return _MODS.get(k.name, k.name)
    if hasattr(k, "char") and k.char:
        return k.char.lower()
    return str(k)


def _pretty_combo(combo: str) -> str:
    disp = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "space": "Space"}
    return "  +  ".join(disp.get(p, p.upper() if len(p) == 1 else p.capitalize())
                        for p in combo.split("+") if p)


# ── reusable widgets ─────────────────────────────────────────────────

def _page():
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    content = QWidget(); content.setObjectName("content")
    lay = QVBoxLayout(content)
    lay.setContentsMargins(22, 20, 22, 20)
    lay.setSpacing(15)
    scroll.setWidget(content)
    return scroll, lay


def _title(text, sub=""):
    w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
    t = QLabel(text); t.setProperty("class", "page_title"); v.addWidget(t)
    if sub:
        s = QLabel(sub); s.setProperty("class", "page_sub"); s.setWordWrap(True); v.addWidget(s)
    return w


def _card(title, sub=""):
    card = QFrame(); card.setProperty("class", "card")
    outer = QVBoxLayout(card); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(0)
    head = QWidget(); hl = QHBoxLayout(head); hl.setContentsMargins(15, 11, 15, 11); hl.setSpacing(8)
    t = QLabel(title); t.setProperty("class", "card_title"); hl.addWidget(t)
    if sub:
        s = QLabel(sub); s.setProperty("class", "card_sub"); hl.addWidget(s)
    hl.addStretch(1)
    outer.addWidget(head)
    sep = QFrame(); sep.setProperty("class", "hsep"); outer.addWidget(sep)
    body = QWidget(); bl = QVBoxLayout(body); bl.setContentsMargins(15, 13, 15, 14); bl.setSpacing(11)
    outer.addWidget(body)
    return card, bl


def _row(label, widget, help_text=""):
    w = QWidget(); h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(14)
    left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(2)
    lbl = QLabel(label); lbl.setProperty("class", "row_label"); lv.addWidget(lbl)
    if help_text:
        hp = QLabel(help_text); hp.setProperty("class", "row_help"); hp.setWordWrap(True); lv.addWidget(hp)
    left.setFixedWidth(178)
    h.addWidget(left, 0, Qt.AlignmentFlag.AlignTop)
    h.addWidget(widget, 1)
    return w


class Collapsible(QWidget):
    def __init__(self, title, expanded=False):
        super().__init__()
        self._title = title; self._exp = expanded
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(6)
        self._btn = QPushButton(self._arrow() + title)
        self._btn.setProperty("class", "collapse")
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._toggle)
        v.addWidget(self._btn)
        self._body = QWidget(); self._bl = QVBoxLayout(self._body)
        self._bl.setContentsMargins(2, 0, 2, 4); self._bl.setSpacing(11)
        self._body.setVisible(expanded)
        v.addWidget(self._body)

    def _arrow(self):
        return "▾  " if self._exp else "▸  "

    def _toggle(self):
        self._exp = not self._exp
        self._body.setVisible(self._exp)
        self._btn.setText(self._arrow() + self._title)

    def add(self, w):
        self._bl.addWidget(w)


class ProviderEditor(QWidget):
    """Editable list of custom providers. Each row: enabled, name, api type,
    base URL, model, API key, reorder, remove. Works for both AI models and
    speech-to-text (the allowed api types + presets differ)."""

    def __init__(self, api_types, presets, key_help=""):
        super().__init__()
        self.api_types = api_types
        self.presets = presets
        self.key_help = key_help
        self._rows: list[dict] = []
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(9)
        self._list = QVBoxLayout(); self._list.setSpacing(9)
        holder = QWidget(); holder.setLayout(self._list)
        v.addWidget(holder)
        add = QPushButton("＋  Add provider")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_menu)
        bar = QHBoxLayout(); bar.addWidget(add); bar.addStretch(1)
        v.addLayout(bar)

    def _add_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#151a24;border:1px solid #1e2636;border-radius:8px;padding:6px;"
            "color:#e6ebf2;font-size:12.5px;}QMenu::item{padding:6px 18px;border-radius:5px;}"
            "QMenu::item:selected{background:#1a2030;}")
        for label in self.presets:
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, l=label: self._add_preset(l))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _add_preset(self, label):
        api_type, base, model = self.presets[label]
        name = label.replace("…", "").strip() or "Custom"
        self._add_row({"name": name, "api_type": api_type, "base_url": base,
                       "model": model, "enabled": True})

    def _add_row(self, data):
        card = QFrame(); card.setProperty("class", "prow")
        outer = QVBoxLayout(card); outer.setContentsMargins(11, 9, 11, 10); outer.setSpacing(7)

        top = QHBoxLayout(); top.setSpacing(8)
        chk = QCheckBox(); chk.setChecked(bool(data.get("enabled", True))); chk.setFixedWidth(20)
        chk.setToolTip("Enabled")
        name = QLineEdit(data.get("name", "")); name.setPlaceholderText("Name")
        atype = QComboBox(); atype.addItems(self.api_types)
        if data.get("api_type") in self.api_types:
            atype.setCurrentText(data["api_type"])
        atype.setFixedWidth(108)
        up = QPushButton("↑"); dn = QPushButton("↓"); rm = QPushButton("✕")
        for btn in (up, dn, rm):
            btn.setProperty("class", "iconbtn"); btn.setFixedWidth(26)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        top.addWidget(chk); top.addWidget(name, 1); top.addWidget(atype)
        top.addWidget(up); top.addWidget(dn); top.addWidget(rm)
        outer.addLayout(top)

        base = QLineEdit(data.get("base_url", "")); base.setPlaceholderText("Base URL (https://…)")
        outer.addWidget(base)

        bottom = QHBoxLayout(); bottom.setSpacing(8)
        model = QLineEdit(data.get("model", "")); model.setPlaceholderText("Model id")
        key = QLineEdit(data.get("api_key", "")); key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        env = data.get("api_key_env")
        key.setPlaceholderText(f"API key (using .env: {env})" if env and not data.get("api_key") else "API key")
        bottom.addWidget(model, 1); bottom.addWidget(key, 1)
        outer.addLayout(bottom)

        row = {"card": card, "chk": chk, "name": name, "atype": atype,
               "base": base, "model": model, "key": key, "api_key_env": env}

        def sync_type():
            is_local = atype.currentText() == "local"
            base.setVisible(not is_local)
            key.setVisible(not is_local)
        atype.currentTextChanged.connect(lambda _=None: sync_type())
        sync_type()

        up.clicked.connect(lambda: self._move(row, -1))
        dn.clicked.connect(lambda: self._move(row, 1))
        rm.clicked.connect(lambda: self._remove(row))
        self._rows.append(row)
        self._relayout()

    def _relayout(self):
        while self._list.count():
            self._list.takeAt(0)
        for r in self._rows:
            self._list.addWidget(r["card"])

    def _move(self, row, delta):
        i = self._rows.index(row); j = i + delta
        if 0 <= j < len(self._rows):
            self._rows[i], self._rows[j] = self._rows[j], self._rows[i]
            self._relayout()

    def _remove(self, row):
        self._rows.remove(row)
        row["card"].deleteLater()
        self._relayout()

    def load(self, providers):
        for r in self._rows:
            r["card"].deleteLater()
        self._rows = []
        for p in providers or []:
            self._add_row(dict(p))

    def dump(self):
        out = []
        for r in self._rows:
            d = {"name": r["name"].text().strip() or "provider",
                 "api_type": r["atype"].currentText(),
                 "model": r["model"].text().strip(),
                 "enabled": r["chk"].isChecked()}
            if d["api_type"] != "local":
                d["base_url"] = r["base"].text().strip()
                k = r["key"].text().strip()
                if k:
                    d["api_key"] = k
                if r["api_key_env"]:
                    d["api_key_env"] = r["api_key_env"]
            out.append(d)
        return out


class ContextFilesEditor(QWidget):
    """Editable list of titled context files attached to the current profile.
    Upload any file(s) or paste text; each becomes reference material for that
    profile's answers (and a {{title}} template var)."""

    def __init__(self):
        super().__init__()
        self._files: list[dict] = []      # [{title, content}]
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(9)
        self._list = QVBoxLayout(); self._list.setSpacing(7)
        holder = QWidget(); holder.setLayout(self._list)
        v.addWidget(holder)
        self._empty = QLabel("No context files for this profile yet.")
        self._empty.setProperty("class", "row_help")
        v.addWidget(self._empty)
        bar = QHBoxLayout()
        addf = QPushButton("＋  Upload file"); addf.clicked.connect(self._add_file)
        addt = QPushButton("＋  Add text"); addt.clicked.connect(self._add_text)
        for b in (addf, addt):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        bar.addWidget(addf); bar.addWidget(addt); bar.addStretch(1)
        v.addLayout(bar)
        self._relayout()

    def _add_file(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose context file(s)", "",
            "Text & docs (*.txt *.md *.csv *.json *.log *.py);;All files (*.*)")
        for p in paths:
            try:
                content = Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._files.append({"title": Path(p).stem, "content": content})
        self._relayout()

    def _add_text(self):
        title, ok = QInputDialog.getText(self, "Add context", "Title:")
        if not ok or not title.strip():
            return
        content, ok = QInputDialog.getMultiLineText(self, "Add context", f"Content for “{title.strip()}”:", "")
        if not ok:
            return
        self._files.append({"title": title.strip(), "content": content})
        self._relayout()

    def _edit(self, i):
        f = self._files[i]
        title, ok = QInputDialog.getText(self, "Edit title", "Title:", text=f["title"])
        if not ok or not title.strip():
            return
        content, ok = QInputDialog.getMultiLineText(self, "Edit content", f"Content for “{title.strip()}”:", f["content"])
        if not ok:
            return
        self._files[i] = {"title": title.strip(), "content": content}
        self._relayout()

    def _remove(self, i):
        del self._files[i]
        self._relayout()

    def _relayout(self):
        while self._list.count():
            it = self._list.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        for i, f in enumerate(self._files):
            row = QFrame(); row.setProperty("class", "prow")
            h = QHBoxLayout(row); h.setContentsMargins(11, 7, 11, 7); h.setSpacing(8)
            lbl = QLabel(f["title"] or "untitled"); lbl.setStyleSheet("font-weight:600;")
            meta = QLabel(f"{len(f['content'])} chars"); meta.setProperty("class", "row_help")
            ed = QPushButton("Edit"); rm = QPushButton("✕")
            ed.setProperty("class", "iconbtn"); rm.setProperty("class", "iconbtn"); rm.setFixedWidth(26)
            ed.setCursor(Qt.CursorShape.PointingHandCursor); rm.setCursor(Qt.CursorShape.PointingHandCursor)
            ed.clicked.connect(lambda _=False, k=i: self._edit(k))
            rm.clicked.connect(lambda _=False, k=i: self._remove(k))
            h.addWidget(lbl, 1); h.addWidget(meta); h.addWidget(ed); h.addWidget(rm)
            self._list.addWidget(row)
        self._empty.setVisible(not self._files)

    def load(self, files):
        self._files = [{"title": f.get("title", ""), "content": f.get("content", "")}
                       for f in (files or [])]
        self._relayout()

    def dump(self):
        return [dict(f) for f in self._files if f.get("title", "").strip()]


class SettingsWindow(QWidget):
    reconfigured = Signal()
    _hk_captured = Signal(str, str)

    def __init__(self, co):
        super().__init__()
        self.co = co
        self.on_hotkeys_changed = None
        self._cap_listener = None
        self._capturing = None
        self._hk_buttons = {}
        self._hotkey_edits = {}
        self.setWindowTitle("Interview Copilot — Settings")
        self.setStyleSheet(QSS)
        self.setObjectName("window_root")
        self.setMinimumSize(440, 400)
        st = load_state()
        self.resize(int(st.get("settings_w", 740)), int(st.get("settings_h", 660)))
        self._hk_captured.connect(self._on_hk_captured)
        self._build()
        self._load()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        save_state(settings_w=self.width(), settings_h=self.height())

    def showEvent(self, e):
        super().showEvent(e)
        hidden = bool((CFG.get("overlay", {}) or {}).get("exclude_from_capture", True))
        apply_capture_exclusion(self, hidden)

    # ── build ─────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        body = QWidget(); bl = QHBoxLayout(body); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(0)
        self._sidebar = QListWidget(); self._sidebar.setObjectName("sidebar"); self._sidebar.setFixedWidth(184)
        self._sidebar.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for sid, label in SECTIONS:
            it = QListWidgetItem(label); it.setData(Qt.ItemDataRole.UserRole, sid)
            self._sidebar.addItem(it)
        self._sidebar.currentRowChanged.connect(lambda i: self._stack.setCurrentIndex(i))
        self._stack = QStackedWidget()
        for builder in (self._page_model, self._page_interview, self._page_speech,
                        self._page_behavior, self._page_about):
            self._stack.addWidget(builder())
        bl.addWidget(self._sidebar); bl.addWidget(self._stack, 1)
        root.addWidget(body, 1)

        foot = QWidget(); foot.setStyleSheet("background:#10141c;border-top:1px solid #171d28;")
        fl = QHBoxLayout(foot); fl.setContentsMargins(18, 10, 18, 10)
        self.status_lbl = QLabel(""); self.status_lbl.setStyleSheet("color:#56e0c2;font-size:11.5px;font-weight:600;")
        fl.addWidget(self.status_lbl, 1)
        close = QPushButton("Close"); close.setCursor(Qt.CursorShape.PointingHandCursor); close.clicked.connect(self.hide)
        save = QPushButton("Save && Apply"); save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.setStyleSheet(
            "QPushButton{background:#6ba4ff;color:#0c0f14;font-weight:700;border:1px solid #6ba4ff;"
            "border-radius:6px;padding:6px 18px;min-height:20px;}QPushButton:hover{background:#7db0ff;}")
        save.clicked.connect(self._save)
        fl.addWidget(close); fl.addWidget(save)
        root.addWidget(foot)
        self._sidebar.setCurrentRow(0)

    def _page_model(self):
        scroll, lay = _page()
        lay.addWidget(_title("AI Model",
                             "Add any OpenAI-compatible or Anthropic-compatible provider. Enable more "
                             "than one and they're tried top-first, so a rate-limited one falls through."))
        card, b = _card("Providers")
        self.llm_editor = ProviderEditor(["openai", "anthropic"], LLM_PRESETS)
        b.addWidget(self.llm_editor)
        lay.addWidget(card)

        gen = Collapsible("Generation options")
        self.temperature = QDoubleSpinBox(); self.temperature.setRange(0, 2); self.temperature.setSingleStep(0.1)
        gen.add(_row("Temperature", self.temperature, "Higher = more varied phrasing."))
        self.history = QSpinBox(); self.history.setRange(0, 40)
        gen.add(_row("History exchanges", self.history, "Past Q/A kept for follow-ups."))
        self.max_tokens = QSpinBox(); self.max_tokens.setRange(128, 4096); self.max_tokens.setSingleStep(64)
        gen.add(_row("Max answer tokens", self.max_tokens))
        self.disable_thinking = QCheckBox("Disable model thinking (faster answers, any provider)")
        gen.add(self.disable_thinking)
        self.thinking_tokens = QSpinBox(); self.thinking_tokens.setRange(0, 32768); self.thinking_tokens.setSingleStep(256)
        self.thinking_tokens.setSuffix(" tokens")
        gen.add(_row("Thinking tokens", self.thinking_tokens,
                     "Budget when thinking is ON. 0 = provider default. Ignored if disabled above."))
        c2, b2 = _card("Options"); b2.addWidget(gen)
        lay.addWidget(c2); lay.addStretch(1)
        return scroll

    def _page_interview(self):
        scroll, lay = _page()
        lay.addWidget(_title("Interview", "Create as many profiles as you like. Each has its own "
                                          "prompt and context files."))

        card, b = _card("Profiles")
        selrow = QWidget(); sh = QHBoxLayout(selrow); sh.setContentsMargins(0, 0, 0, 0); sh.setSpacing(8)
        self.profile = QComboBox()
        newb = QPushButton("＋ New"); delb = QPushButton("Delete")
        for btn in (newb, delb):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        newb.clicked.connect(self._profile_new)
        delb.clicked.connect(self._profile_delete)
        sh.addWidget(self.profile, 1); sh.addWidget(newb); sh.addWidget(delb)
        b.addWidget(_row("Active profile", selrow, "The selected profile is what the AI answers with."))
        self.prof_name = QLineEdit(); b.addWidget(_row("Display name", self.prof_name))
        hint = QLabel("The full system prompt for this profile — include the role, the tone/format "
                      "you want, length, and language all here. Reference any context-file title as {{title}}.")
        hint.setProperty("class", "row_help"); hint.setWordWrap(True)
        b.addWidget(hint)
        self.prof_body = QPlainTextEdit(); self.prof_body.setMinimumHeight(200)
        self.prof_body.setPlaceholderText("e.g. You are helping me in a live interview for a backend role.\n"
                                          "Answer in short, confident, speakable bullet points…")
        b.addWidget(self.prof_body)
        lay.addWidget(card)

        cc, cb = _card("Context files", "attached to this profile — uploaded or pasted")
        self.ctx_editor = ContextFilesEditor()
        cb.addWidget(self.ctx_editor)
        lay.addWidget(cc); lay.addStretch(1)

        self.profile.currentIndexChanged.connect(self._on_profile_switch)
        return scroll

    # ── profile management ─────────────────────────────────────────────

    def _sync_current_profile(self):
        """Persist the visible widgets back into the in-memory model."""
        if not getattr(self, "_cur_pid", None):
            return
        m = self._prof_model.get(self._cur_pid)
        if m is None:
            return
        m["name"] = self.prof_name.text().strip() or self._cur_pid
        m["body"] = self.prof_body.toPlainText()
        m["files"] = self.ctx_editor.dump()

    def _load_profile_into_widgets(self, pid):
        m = self._prof_model.get(pid)
        self._cur_pid = pid
        if m is None:
            return
        self.prof_name.setText(m.get("name") or pid)
        self.prof_body.setPlainText(m.get("body") or "")
        self.ctx_editor.load(m.get("files") or [])

    def _on_profile_switch(self, _idx):
        pid = self.profile.currentData()
        if not pid or pid == getattr(self, "_cur_pid", None):
            return
        self._sync_current_profile()
        self._load_profile_into_widgets(pid)

    def _refill_profile_combo(self, select_pid):
        self.profile.blockSignals(True)
        self.profile.clear()
        for pid, m in self._prof_model.items():
            self.profile.addItem(m.get("name") or pid, pid)
        idx = max(0, self.profile.findData(select_pid))
        self.profile.setCurrentIndex(idx)
        self.profile.blockSignals(False)

    def _profile_new(self):
        name, ok = QInputDialog.getText(self, "New profile", "Profile name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        pid = profiles_mod._slug(name)
        if pid in self._prof_model:
            QMessageBox.information(self, "Exists", "A profile with that name already exists.")
            return
        self._sync_current_profile()
        self._prof_model[pid] = {
            "name": name, "temp": None, "files": [],
            "body": "You are helping me in a live interview.\n"
                    "Give concise, confident, speakable answers.",
        }
        self._deleted_profiles.discard(pid)
        self._refill_profile_combo(pid)
        self._load_profile_into_widgets(pid)

    def _profile_delete(self):
        pid = self.profile.currentData()
        if not pid:
            return
        if len(self._prof_model) <= 1:
            QMessageBox.information(self, "Keep one", "At least one profile is required.")
            return
        if QMessageBox.question(self, "Delete profile", f"Delete profile “{pid}” and its context files?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._prof_model.pop(pid, None)
        self._deleted_profiles.add(pid)
        self._cur_pid = None
        nxt = next(iter(self._prof_model))
        self._refill_profile_combo(nxt)
        self._load_profile_into_widgets(nxt)

    def _page_speech(self):
        scroll, lay = _page()
        lay.addWidget(_title("Speech-to-text",
                             "Transcription runs offline either way. VoxType already keeps Whisper "
                             "loaded on the GPU, so borrowing its API costs no extra VRAM and no "
                             "model-load wait — worth preferring when VoxType is running."))

        ec, eb = _card("Engine")
        self.stt_engine = QComboBox()
        for val, label in STT_ENGINES:
            self.stt_engine.addItem(label, val)
        eb.addWidget(_row("Engine", self.stt_engine,
                          "Auto uses VoxType when it's running and loads the built-in model when it "
                          "isn't — including a mid-session switch if VoxType goes away."))
        urlrow = QWidget(); uh = QHBoxLayout(urlrow); uh.setContentsMargins(0, 0, 0, 0); uh.setSpacing(8)
        self.stt_vox_url = QLineEdit(); self.stt_vox_url.setPlaceholderText("http://127.0.0.1:6600")
        test = QPushButton("Test"); test.setCursor(Qt.CursorShape.PointingHandCursor)
        test.clicked.connect(self._test_voxtype)
        uh.addWidget(self.stt_vox_url, 1); uh.addWidget(test)
        eb.addWidget(_row("VoxType URL", urlrow,
                          "VoxType → Settings → OpenAI HTTP Server must be enabled (default port 6600)."))
        self.stt_vox_status = QLabel(""); self.stt_vox_status.setProperty("class", "row_help")
        self.stt_vox_status.setWordWrap(True)
        eb.addWidget(self.stt_vox_status)
        lay.addWidget(ec)

        mc, mb = _card("Built-in model", "Local engine / Auto fallback — VoxType's Whisper backend")
        self.stt_model = QLineEdit(); self.stt_model.setPlaceholderText("openai/whisper-large-v3")
        mb.addWidget(_row("Whisper model", self.stt_model,
                          "HF repo id or local path. Already cached if VoxType uses it. "
                          "CPU-only machines: openai/whisper-small.en."))
        self.stt_device = QComboBox(); self.stt_device.addItem("GPU (CUDA)", "cuda"); self.stt_device.addItem("CPU", "cpu")
        mb.addWidget(_row("Device", self.stt_device, "GPU falls back to CPU automatically if CUDA is unavailable."))
        self.stt_dtype = QComboBox()
        for val, label in (("auto", "Auto"), ("fp16", "fp16 (GPU fast)"),
                           ("bf16", "bf16 (Ampere+)"), ("fp32", "fp32 (accurate)")):
            self.stt_dtype.addItem(label, val)
        mb.addWidget(_row("Precision", self.stt_dtype, "auto = fp16 on GPU, fp32 on CPU."))
        self.stt_threads = QSpinBox(); self.stt_threads.setRange(0, os.cpu_count() or 64)
        self.stt_threads.setSpecialValueText("Auto (all cores)")
        mb.addWidget(_row("CPU threads", self.stt_threads,
                          "Torch threads on CPU. Auto = one per physical core."))
        self.stt_lang = QLineEdit(); self.stt_lang.setPlaceholderText("en")
        mb.addWidget(_row("Language", self.stt_lang, "ISO code sent to both engines. Blank = auto-detect."))
        self.stt_task = QComboBox()
        self.stt_task.addItem("Transcribe", "transcribe"); self.stt_task.addItem("Translate → English", "translate")
        mb.addWidget(_row("Task", self.stt_task, "translate = English output regardless of the spoken language."))

        dec = Collapsible("Decoding")
        self.stt_beam = QSpinBox(); self.stt_beam.setRange(1, 10)
        dec.add(_row("Beams", self.stt_beam, "1 = greedy, fastest. Higher = lower WER but ~N× slower."))
        self.stt_temp = QDoubleSpinBox(); self.stt_temp.setRange(0, 1); self.stt_temp.setSingleStep(0.05)
        dec.add(_row("Temperature", self.stt_temp,
                     "0 = deterministic (recommended). 0.2–0.4 helps noisy mics where greedy stalls."))
        self.stt_rep = QDoubleSpinBox(); self.stt_rep.setRange(1, 2); self.stt_rep.setSingleStep(0.05)
        dec.add(_row("Repetition penalty", self.stt_rep, "1.0 = off. 1.1–1.3 if it loops on filler sounds."))
        self.stt_prompt = QLineEdit(); self.stt_prompt.setPlaceholderText("e.g. Kubernetes, gRPC, PostgreSQL")
        dec.add(_row("Initial prompt", self.stt_prompt, "Biases decoding toward jargon, acronyms, names."))
        mb.addWidget(dec)

        perf = Collapsible("Performance")
        self.stt_attn = QComboBox()
        for val, label in (("auto", "Auto"), ("sdpa", "SDPA"), ("flash_attention_2", "Flash-Attn 2"),
                           ("eager", "Eager (compat)")):
            self.stt_attn.addItem(label, val)
        perf.add(_row("Attention", self.stt_attn,
                      "auto = transformers picks (SDPA). Flash-Attn 2 needs fp16/bf16 + the flash-attn wheel."))
        self.stt_warmup = QCheckBox("Warm up on load (first real transcription isn't slow)")
        perf.add(self.stt_warmup)
        self.stt_compile = QCheckBox("torch.compile (~20–40% faster steady-state; slow first load)")
        perf.add(self.stt_compile)
        mb.addWidget(perf)
        lay.addWidget(mc); lay.addStretch(1)
        return scroll

    def _test_voxtype(self):
        from .stt import voxtype_probe
        url = self.stt_vox_url.text().strip() or "http://127.0.0.1:6600"
        ok, detail = voxtype_probe(url)
        self.stt_vox_status.setText(("✓ Reachable — " if ok else "✗ Not reachable — ") + detail)
        self.stt_vox_status.setStyleSheet(f"color:{'#56e0c2' if ok else '#ff8080'};font-size:11.5px;")

    def _page_behavior(self):
        scroll, lay = _page()
        lay.addWidget(_title("Overlay & Audio",
                             "How the HUD looks and how recording is triggered."))

        oc, ob = _card("Overlay", "appearance & privacy of the HUD window")
        self.opacity = QDoubleSpinBox()
        self.opacity.setRange(0.30, 1.00); self.opacity.setSingleStep(0.05); self.opacity.setDecimals(2)
        ob.addWidget(_row("HUD opacity", self.opacity,
                          "1.00 = solid, lower = more see-through."))
        self.hide_capture = QCheckBox("Hide HUD from screen recording & screen-share")
        ob.addWidget(self.hide_capture)
        hint = QLabel("Stays visible on your screen but is invisible to OBS, Google "
                      "Meet / Zoom / Teams share, and screenshots (Windows).")
        hint.setProperty("class", "row_help"); hint.setWordWrap(True); ob.addWidget(hint)
        lay.addWidget(oc)

        ac, ab = _card("Listening", "manual: ▶ Listen, then Answer / Improve when you want help")
        self.echo_gate = QCheckBox("Echo gate — mute my mic while the interviewer is talking "
                                   "(needed on speakers, harmless on headphones)")
        ab.addWidget(self.echo_gate)
        self.chunk_secs = QSpinBox(); self.chunk_secs.setRange(8, 28); self.chunk_secs.setSuffix(" s")
        ab.addWidget(_row("Background chunk", self.chunk_secs,
                          "While listening, audio is transcribed in chunks this long so Answer only "
                          "waits for the last few seconds. Max 28 (Whisper's window is 30)."))
        self.silence_sens = QDoubleSpinBox()
        self.silence_sens.setRange(0.005, 0.20); self.silence_sens.setSingleStep(0.005); self.silence_sens.setDecimals(3)
        ab.addWidget(_row("Speech threshold", self.silence_sens,
                          "Level above which audio counts as speech — used to trim silence and for the "
                          "echo gate. Lower = more sensitive."))
        lay.addWidget(ac); lay.addStretch(1)
        return scroll

    def _page_about(self):
        scroll, lay = _page()
        lay.addWidget(_title("Hotkeys",
                             "Global — work even while the meeting window is focused. Click Change, "
                             "then press the new combo (Esc cancels)."))
        card, b = _card("Shortcuts")
        self._hk_buttons = {}
        for action, label in HK_ACTIONS:
            r = QWidget(); h = QHBoxLayout(r); h.setContentsMargins(0, 2, 0, 2); h.setSpacing(10)
            a = QLabel(label); a.setStyleSheet("font-size:12.5px;font-weight:600;")
            btn = QPushButton("—")
            btn.setStyleSheet(
                "QPushButton{background:#0d1118;border:1px solid #1e2636;border-radius:6px;"
                "color:#6ba4ff;font-family:Consolas,monospace;font-size:12px;font-weight:700;"
                "padding:6px 14px;min-width:150px;}QPushButton:hover{border:1px solid #6ba4ff;}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, ac=action: self._start_capture(ac))
            self._hk_buttons[action] = btn
            h.addWidget(a, 1); h.addWidget(btn)
            b.addWidget(r)
        lay.addWidget(card); lay.addStretch(1)
        return scroll

    # ── hotkey capture ─────────────────────────────────────────────────

    def _start_capture(self, action):
        if self._cap_listener is not None:
            return
        self._capturing = action
        self._hk_buttons[action].setText("press keys…")
        from pynput import keyboard
        mods: set[str] = set()

        def on_press(k):
            n = _keyname(k)
            if n in ("ctrl", "alt", "shift", "win"):
                mods.add(n); return
            if n == "esc":
                self._hk_captured.emit(action, ""); return False
            order = [m for m in ("ctrl", "alt", "shift", "win") if m in mods]
            self._hk_captured.emit(action, "+".join(order + [n])); return False

        self._cap_listener = keyboard.Listener(on_press=on_press)
        self._cap_listener.start()

    def _on_hk_captured(self, action, combo):
        if self._cap_listener is not None:
            try:
                self._cap_listener.stop()
            except Exception:
                pass
            self._cap_listener = None
        self._capturing = None
        if combo:
            self._hotkey_edits[action] = combo
        self._hk_buttons[action].setText(_pretty_combo(self._hotkey_edits.get(action, "")) or "—")

    # ── load / save ────────────────────────────────────────────────────

    def _load(self):
        llm = CFG.get("llm", {}) or {}
        self.llm_editor.load(llm.get("providers", []))
        self.temperature.setValue(float(llm.get("temperature", 0.4)))
        self.history.setValue(int(llm.get("history_exchanges", 12)))
        self.max_tokens.setValue(int(llm.get("max_tokens", 900)))
        self.disable_thinking.setChecked(bool(llm.get("disable_thinking", True)))
        self.thinking_tokens.setValue(int(llm.get("thinking_tokens", 0)))

        st = CFG.get("stt", {}) or {}
        eng = (st.get("engine") or "auto").strip().lower()
        self.stt_engine.setCurrentIndex(max(0, self.stt_engine.findData(eng)))
        self.stt_vox_url.setText(st.get("voxtype_url") or "http://127.0.0.1:6600")
        self.stt_vox_status.setText("")
        self.stt_model.setText(st.get("model") or "openai/whisper-large-v3")
        self.stt_device.setCurrentIndex(max(0, self.stt_device.findData(st.get("device") or "cuda")))
        self.stt_dtype.setCurrentIndex(max(0, self.stt_dtype.findData(st.get("dtype") or "auto")))
        self.stt_threads.setValue(int(st.get("cpu_threads") or 0))
        self.stt_lang.setText(st.get("language") or "")
        self.stt_task.setCurrentIndex(max(0, self.stt_task.findData(st.get("task") or "transcribe")))
        self.stt_beam.setValue(int(st.get("num_beams") or st.get("beam_size") or 1))
        self.stt_temp.setValue(float(st.get("temperature") or 0.0))
        self.stt_rep.setValue(float(st.get("repetition_penalty") or 1.0))
        self.stt_prompt.setText(st.get("initial_prompt") or "")
        self.stt_attn.setCurrentIndex(max(0, self.stt_attn.findData(st.get("attn_impl") or "auto")))
        self.stt_warmup.setChecked(bool(st.get("warmup", True)))
        self.stt_compile.setChecked(bool(st.get("torch_compile", False)))

        ov = CFG.get("overlay", {}) or {}
        self.opacity.setValue(float(ov.get("opacity", 1.0)))
        self.hide_capture.setChecked(bool(ov.get("exclude_from_capture", True)))
        au = CFG.get("audio", {}) or {}
        self.echo_gate.setChecked(bool(au.get("echo_gate", True)))
        self.chunk_secs.setValue(int(au.get("chunk_seconds", 20)))
        self.silence_sens.setValue(float(au.get("speech_threshold", 0.02)))

        # profiles → in-memory model {id: {name, body, temp, files}}
        self._prof_model = {}
        self._deleted_profiles = set()
        self._cur_pid = None
        for pid, p in profiles_mod.list_profiles().items():
            self._prof_model[pid] = {
                "name": p.get("name") or pid,
                "body": p.get("body") or "",
                "temp": (p.get("meta") or {}).get("temperature"),
                "files": profiles_mod.list_context_files(pid),
            }
        if not self._prof_model:      # ensure at least one profile exists
            self._prof_model["default"] = {"name": "default", "body":
                "You are helping me in a live interview. Give concise, speakable answers.",
                "temp": None, "files": []}
        # legacy global persona folds into each profile's prompt (one-time),
        # then it's cleared on save — prompt + persona are one field now.
        persona = (CFG.get("persona") or "").strip()
        if persona:
            for m in self._prof_model.values():
                body = (m.get("body") or "").rstrip()
                m["body"] = f"{body}\n\n{persona}".strip() if body else persona

        want = CFG.get("profile", "")
        if want not in self._prof_model:
            want = next(iter(self._prof_model))
        self._refill_profile_combo(want)
        self._load_profile_into_widgets(want)

        self._hotkey_edits = dict(CFG.get("hotkeys", {}) or {})
        for action, btn in self._hk_buttons.items():
            btn.setText(_pretty_combo(self._hotkey_edits.get(action, "")) or "—")

    def _save(self):
        cfg = dict(CFG)
        cfg["llm"] = {"providers": self.llm_editor.dump(),
                      "history_exchanges": self.history.value(),
                      "max_tokens": self.max_tokens.value(),
                      "temperature": round(self.temperature.value(), 2),
                      "disable_thinking": self.disable_thinking.isChecked(),
                      "thinking_tokens": self.thinking_tokens.value()}
        # persist all profiles + their context files
        self._sync_current_profile()
        for pid in self._deleted_profiles:
            profiles_mod.delete_profile(pid)
        self._deleted_profiles.clear()
        for pid, m in self._prof_model.items():
            meta = {}
            if m.get("temp"):
                meta["temperature"] = m["temp"]
            profiles_mod.save_profile(pid, m.get("name") or pid, m.get("body") or "", meta)
            profiles_mod.set_context_files(pid, m.get("files") or [])
        cfg["profile"] = self.profile.currentData() or self._cur_pid
        cfg["persona"] = ""          # merged into each profile's prompt
        cfg.pop("vars", None)
        cfg["hotkeys"] = dict(self._hotkey_edits)
        cfg["overlay"] = {**(CFG.get("overlay", {}) or {}),
                          "opacity": round(self.opacity.value(), 2),
                          "exclude_from_capture": self.hide_capture.isChecked()}
        # drop the faster-whisper-era keys (compute_type / beam_size)
        st_old = {k: v for k, v in (CFG.get("stt", {}) or {}).items()
                  if k not in ("compute_type", "beam_size")}
        cfg["stt"] = {**st_old,
                      "engine": self.stt_engine.currentData() or "auto",
                      "voxtype_url": self.stt_vox_url.text().strip() or "http://127.0.0.1:6600",
                      "language": self.stt_lang.text().strip(),
                      "model": self.stt_model.text().strip() or "openai/whisper-large-v3",
                      "device": self.stt_device.currentData() or "cuda",
                      "dtype": self.stt_dtype.currentData() or "auto",
                      "attn_impl": self.stt_attn.currentData() or "auto",
                      "torch_compile": self.stt_compile.isChecked(),
                      "warmup": self.stt_warmup.isChecked(),
                      "cpu_threads": self.stt_threads.value(),
                      "task": self.stt_task.currentData() or "transcribe",
                      "num_beams": self.stt_beam.value(),
                      "temperature": round(self.stt_temp.value(), 2),
                      "repetition_penalty": round(self.stt_rep.value(), 2),
                      "initial_prompt": self.stt_prompt.text().strip()}
        cfg["audio"] = {**(CFG.get("audio", {}) or {}),
                        "echo_gate": self.echo_gate.isChecked(),
                        "chunk_seconds": self.chunk_secs.value(),
                        "speech_threshold": round(self.silence_sens.value(), 3)}

        save_config(cfg)
        self.co.apply_settings()
        if callable(self.on_hotkeys_changed):
            self.on_hotkeys_changed()
        self.status_lbl.setText("✓ Saved & applied.")
        self.reconfigured.emit()

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self._load(); self.show(); self.raise_(); self.activateWindow()
