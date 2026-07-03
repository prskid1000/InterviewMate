"""Native settings window — summoned by a global hotkey (no tray icon).

Providers are fully user-defined: add any number of OpenAI-compatible or
Anthropic-compatible AI models, and any number of speech-to-text providers
(local Whisper, OpenAI-compatible audio, or Gemini). Nothing is hardcoded —
each provider is a name + api type + base URL + model + key. Save & Apply
reconfigures the live app. Native frame → resizable from every edge/corner.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .config import ROOT, CFG, save_config
from .qt_theme_local import QSS
from .uikit import load_state, save_state

CONTEXT_DIR = ROOT / "context"

SECTIONS = [("speech", "🎙   Speech-to-text"), ("model", "🧠   AI Model"),
            ("interview", "💬   Interview"), ("about", "⌨   Hotkeys")]

# Presets only PREFILL a new provider row — they are not a fixed chain.
# Presets list only FREE-tier providers. Paid ones (OpenAI, Anthropic, …) are
# added via "Custom…" — pick the api type and paste the base URL + model + key.
LLM_PRESETS = {
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

HK_ACTIONS = [("record_toggle", "Start / stop recording"),
              ("open_config", "Open settings window"),
              ("toggle_answer", "Show / hide answer card"),
              ("toggle_hud", "Show / hide HUD palette"),
              ("quit", "Quit the app")]

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

    # ── build ─────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        body = QWidget(); bl = QHBoxLayout(body); bl.setContentsMargins(0, 0, 0, 0); bl.setSpacing(0)
        self._sidebar = QListWidget(); self._sidebar.setObjectName("sidebar"); self._sidebar.setFixedWidth(184)
        for sid, label in SECTIONS:
            it = QListWidgetItem(label); it.setData(Qt.ItemDataRole.UserRole, sid)
            self._sidebar.addItem(it)
        self._sidebar.currentRowChanged.connect(lambda i: self._stack.setCurrentIndex(i))
        self._stack = QStackedWidget()
        for builder in (self._page_speech, self._page_model, self._page_interview, self._page_about):
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

    def _page_speech(self):
        scroll, lay = _page()
        lay.addWidget(_title("Speech-to-text",
                             "Add any transcription provider — offline Whisper, any OpenAI-compatible "
                             "audio endpoint, or Gemini. Tried top-first; first result wins."))
        card, b = _card("Providers")
        self.stt_editor = ProviderEditor(["openai", "gemini"], STT_PRESETS)
        b.addWidget(self.stt_editor)
        lay.addWidget(card)
        adv = Collapsible("Advanced")
        self.stt_lang = QLineEdit()
        adv.add(_row("Language", self.stt_lang, "ISO code, e.g. en, hi."))
        c2, b2 = _card("Options"); b2.addWidget(adv)
        lay.addWidget(c2); lay.addStretch(1)
        return scroll

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
        self.disable_thinking = QCheckBox("Disable Gemini 2.5 thinking (faster answers)")
        gen.add(self.disable_thinking)
        c2, b2 = _card("Options"); b2.addWidget(gen)
        lay.addWidget(c2); lay.addStretch(1)
        return scroll

    def _page_interview(self):
        scroll, lay = _page()
        lay.addWidget(_title("Interview", "Your profile and the context the AI answers from."))
        card, b = _card("Profile & role")
        self.profile = QComboBox()
        for p in sorted(ROOT.glob("profiles/*.md")):
            self.profile.addItem(p.stem)
        b.addWidget(_row("Active profile", self.profile, "Switch anytime, even mid-interview."))
        self.var_role = QLineEdit(); b.addWidget(_row("Role  ({{role}})", self.var_role))
        self.var_subject = QLineEdit(); b.addWidget(_row("Subject  ({{subject}})", self.var_subject))
        lay.addWidget(card)

        pcard, pb = _card("Persona", "how the AI responds to every question")
        hint = QLabel("Tone, format, length and language of the suggested answers. "
                      "Applied on top of the selected profile.")
        hint.setProperty("class", "row_help"); hint.setWordWrap(True)
        pb.addWidget(hint)
        self.persona = QPlainTextEdit(); self.persona.setMinimumHeight(130)
        pb.addWidget(self.persona)
        lay.addWidget(pcard)

        rc = Collapsible("Resume  ({{resume}})")
        self.resume = QPlainTextEdit(); self.resume.setMinimumHeight(120); rc.add(self.resume)
        jc = Collapsible("Job description  ({{job_description}})")
        self.jd = QPlainTextEdit(); self.jd.setMinimumHeight(100); jc.add(self.jd)
        cc, cb = _card("Context files"); cb.addWidget(rc); cb.addWidget(jc)
        lay.addWidget(cc); lay.addStretch(1)
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
        stt = CFG.get("stt", {}) or {}
        self.stt_editor.load(stt.get("providers", []))
        self.stt_lang.setText(stt.get("language", "en"))

        llm = CFG.get("llm", {}) or {}
        self.llm_editor.load(llm.get("providers", []))
        self.temperature.setValue(float(llm.get("temperature", 0.4)))
        self.history.setValue(int(llm.get("history_exchanges", 12)))
        self.max_tokens.setValue(int(llm.get("max_tokens", 900)))
        self.disable_thinking.setChecked(bool(llm.get("disable_thinking", True)))

        self.profile.setCurrentText(CFG.get("profile", "technical"))
        self.persona.setPlainText(CFG.get("persona", "") or "")
        v = CFG.get("vars", {}) or {}
        self.var_role.setText(v.get("role", ""))
        self.var_subject.setText(v.get("subject", ""))
        for widget, fname in ((self.resume, "resume.md"), (self.jd, "job_description.md")):
            fp = CONTEXT_DIR / fname
            widget.setPlainText(fp.read_text(encoding="utf-8") if fp.exists() else "")

        self._hotkey_edits = dict(CFG.get("hotkeys", {}) or {})
        for action, btn in self._hk_buttons.items():
            btn.setText(_pretty_combo(self._hotkey_edits.get(action, "")) or "—")

    def _save(self):
        cfg = dict(CFG)
        cfg["stt"] = {"language": self.stt_lang.text().strip() or "en",
                      "providers": self.stt_editor.dump()}
        cfg["llm"] = {"providers": self.llm_editor.dump(),
                      "history_exchanges": self.history.value(),
                      "max_tokens": self.max_tokens.value(),
                      "temperature": round(self.temperature.value(), 2),
                      "disable_thinking": self.disable_thinking.isChecked()}
        cfg["profile"] = self.profile.currentText()
        cfg["persona"] = self.persona.toPlainText().strip()
        cfg["vars"] = {"role": self.var_role.text().strip(), "subject": self.var_subject.text().strip()}
        cfg["hotkeys"] = dict(self._hotkey_edits)

        CONTEXT_DIR.mkdir(exist_ok=True)
        (CONTEXT_DIR / "resume.md").write_text(self.resume.toPlainText(), encoding="utf-8")
        (CONTEXT_DIR / "job_description.md").write_text(self.jd.toPlainText(), encoding="utf-8")

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
