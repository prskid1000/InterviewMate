"""Frameless always-on-top overlay — ONE unified HUD window.

A single frameless, capture-hidden window that stacks:
  • the top BAR — status glyph + both live level meters (INTERVIEWER / YOU)
  • the answer area — streams the AI answer as one continuous session

Drag the bar to move; click the left status zone to record; right-click for
the menu; resize from any edge/corner. The answer area can be collapsed
(toggle_answer hotkey). Position, size, visibility and collapsed state all
persist across restarts.
"""
from __future__ import annotations

import html
import math
import re
import webbrowser

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QLinearGradient, QMouseEvent, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .config import CFG
from .uikit import ResizableMixin, apply_capture_exclusion, load_state, save_state


def _capture_hidden() -> bool:
    """config overlay.exclude_from_capture (default on)."""
    return bool((CFG.get("overlay", {}) or {}).get("exclude_from_capture", True))


_OVERLAY_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
)

INT_COLOR = QColor(255, 186, 48)     # interviewer — vivid amber
ME_COLOR = QColor(38, 224, 150)      # you — vivid green

_ACCENT = {
    "idle":       QColor(255, 255, 255, 22),
    "recording":  QColor(239, 68, 68, 150),
    "processing": QColor(245, 158, 11, 130),
    "answering":  QColor(167, 139, 250, 130),
    "error":      QColor(248, 113, 113, 150),
}


# ── tiny markdown → Qt rich text ─────────────────────────────────────

def _md_html(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"```\w*\n([\s\S]*?)(?:```|$)",
               r'<pre style="background:#10141a;padding:8px;border-radius:6px;">\1</pre>', s)
    s = re.sub(r"`([^`\n]+)`", r'<code style="background:#10141a;">\1</code>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    out, in_ul = [], False
    for ln in s.split("\n"):
        m = re.match(r"^\s*[-*•]\s+(.*)", ln)
        if m:
            if not in_ul:
                out.append("<ul style='margin:2px 0 2px 14px;'>")
                in_ul = True
            out.append(f"<li>{m.group(1)}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(ln if ln.startswith("<pre") else ln + "<br>")
    if in_ul:
        out.append("</ul>")
    return "".join(out)


class AnswerView(QWidget):
    """Live chat transcript inside the Hud: interviewer / you / AI messages as
    color-coded bubbles in order. Transcription streams in live; the AI answer
    updates its bubble token-by-token."""

    _MAXW = 0.82   # bubble max width as a fraction of the viewport
    # speaker -> (tag, tag color, bubble bg, right-aligned?)
    _ROLES = {
        "interviewer": ("INTERVIEWER", "#f2b84b", "rgba(242,184,75,0.13)", False),
        "me":          ("YOU",         "#34d89e", "rgba(52,201,142,0.15)", True),
        "ai":          ("AI",          "#a78bfa", "rgba(167,139,250,0.14)", False),
    }

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 10)
        lay.setSpacing(6)

        self._hdr = QLabel("Live transcript")
        self._hdr.setStyleSheet(
            "color:#5b6a86; font:600 10px 'Segoe UI'; letter-spacing:0.06em; background:transparent;")
        lay.addWidget(self._hdr)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        lay.addWidget(self._status)

        self._feed = QWidget()
        self._feed.setStyleSheet("background:transparent;")
        self._feedlay = QVBoxLayout(self._feed)
        self._feedlay.setContentsMargins(0, 0, 0, 0)
        self._feedlay.setSpacing(8)
        self._feedlay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._feed)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;}"
            "QScrollBar:vertical{background:transparent;width:8px;}"
            "QScrollBar::handle:vertical{background:#3a4563;border-radius:4px;min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
        self._scroll.viewport().setStyleSheet("background:transparent;")
        lay.addWidget(self._scroll, 1)

        self._bubbles: list[QFrame] = []   # for width updates on resize
        self._ai_body = None               # current streaming AI bubble label
        self._ai_tag = None
        self._ai_raw = ""
        self._dirty = False
        self._render = QTimer(self)
        self._render.setInterval(120)
        self._render.timeout.connect(self._flush)

    # bubbles -----------------------------------------------------------

    def _make_bubble(self, speaker: str, text: str, rich: bool):
        name, tagcolor, bg, right = self._ROLES.get(speaker, self._ROLES["ai"])
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        bub = QFrame()
        bub.setStyleSheet(f"background:{bg}; border-radius:11px;")
        bv = QVBoxLayout(bub); bv.setContentsMargins(12, 7, 12, 9); bv.setSpacing(3)
        tag = QLabel(name)
        tag.setStyleSheet(f"color:{tagcolor}; font:700 9px 'Segoe UI'; "
                          "letter-spacing:0.08em; background:transparent;")
        body = QLabel()
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        body.setStyleSheet("color:#e7edf6; font:13px 'Segoe UI'; background:transparent;")
        body.setText(_md_html(text) if rich else text)
        bv.addWidget(tag); bv.addWidget(body)
        if right:
            h.addStretch(1); h.addWidget(bub)
        else:
            h.addWidget(bub); h.addStretch(1)
        self._bubbles.append(bub)
        self._feedlay.addWidget(row)
        self._apply_width(bub)
        return tag, body

    def _apply_width(self, bub):
        w = int(self._scroll.viewport().width() * self._MAXW)
        if w > 60:
            bub.setMaximumWidth(w)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        for b in self._bubbles:
            self._apply_width(b)

    def _scroll_bottom(self):
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()))

    # events (Qt thread) -----------------------------------------------

    def add_message(self, speaker: str, text: str):
        """A live transcript line (interviewer / me) — a new bubble."""
        if not text:
            return
        self._make_bubble(speaker, text, rich=(speaker == "ai"))
        self._scroll_bottom()

    def begin(self, question: str = ""):
        """Start a fresh AI bubble that streams token-by-token."""
        self.set_status("")
        self._ai_raw = ""
        self._ai_tag, self._ai_body = self._make_bubble("ai", "", rich=True)
        self._ai_body.setText("<i style='color:#8a96aa'>thinking…</i>")
        self._render.start()
        self._scroll_bottom()

    def add(self, delta: str, provider: str | None):
        self._ai_raw += delta
        self._dirty = True
        if provider and self._ai_tag is not None:
            self._ai_tag.setText(f"AI · {provider}")

    def finish(self):
        self._render.stop()
        if self._ai_body is not None:
            self._ai_body.setText(_md_html(self._ai_raw)
                                  or "<i style='color:#8a96aa'>(no answer)</i>")
        self._ai_body = self._ai_tag = None
        self._scroll_bottom()

    def error(self, text: str):
        if self._ai_body is not None:
            self._ai_raw += f"\n**Error:** {text}"
            self._ai_body.setText(_md_html(self._ai_raw))
            self._render.stop()
            self._ai_body = self._ai_tag = None
        else:
            self.set_status(text, error=True)

    def reset(self):
        self._render.stop()
        while self._feedlay.count():
            it = self._feedlay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._bubbles = []
        self._ai_body = self._ai_tag = None
        self._ai_raw = ""
        self.set_status("")

    def load_transcript(self, msgs):
        """Rebuild the whole feed from a session's message log (on tab switch)."""
        self.reset()
        for m in msgs or []:
            sp, tx = m.get("speaker"), m.get("text", "")
            if sp and tx:
                self._make_bubble(sp, tx, rich=(sp == "ai"))
        self._scroll_bottom()

    def _flush(self):
        if self._dirty and self._ai_body is not None:
            self._dirty = False
            self._ai_body.setText(_md_html(self._ai_raw))
            self._scroll_bottom()

    def set_status(self, text: str, error: bool = False):
        if not text:
            self._status.setVisible(False)
            return
        color = "#f87171" if error else "#8a96aa"
        self._status.setStyleSheet(
            f"color:{color}; font:{'600 ' if error else ''}11px 'Segoe UI'; background:transparent;")
        self._status.setText(text)
        self._status.setVisible(True)


class SessionTabs(QWidget):
    """Row of session tabs (+ a new-tab button). The active tab's context is
    what the AI answers from; switching swaps the shown conversation."""

    _TAB = ("QPushButton{{background:{bg};color:{fg};border:1px solid {bd};"
            "border-radius:7px;padding:3px 10px;font:600 11px 'Segoe UI';}}"
            "QPushButton:hover{{border:1px solid #6ba4ff;}}")

    def __init__(self, on_switch, on_new, on_close):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._on_switch, self._on_new, self._on_close = on_switch, on_new, on_close
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(12, 6, 12, 2)
        self._lay.setSpacing(5)

    def render(self, sessions, active):
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        closable = len(sessions) > 1
        for i, s in enumerate(sessions):
            on = (i == active)
            bg = "#1b2740" if on else "rgba(255,255,255,0.04)"
            fg = "#dce6f7" if on else "#8a96aa"
            bd = "#3a568c" if on else "#232c3a"
            # tab = title button (+ a small × to close, when >1 session)
            wrap = QWidget()
            wl = QHBoxLayout(wrap); wl.setContentsMargins(0, 0, 0, 0); wl.setSpacing(0)
            tab = QPushButton(s.get("title", f"Session {i+1}"))
            tab.setCursor(Qt.CursorShape.PointingHandCursor)
            rad = "border-radius:7px;" if not closable else \
                  "border-top-left-radius:7px;border-bottom-left-radius:7px;"
            tab.setStyleSheet(
                f"QPushButton{{background:{bg};color:{fg};border:1px solid {bd};{rad}"
                f"border-right:{'1px' if not closable else '0'} solid {bd};"
                "padding:3px 10px;font:600 11px 'Segoe UI';}"
                "QPushButton:hover{color:#dce6f7;}")
            tab.clicked.connect(lambda _=False, k=i: self._on_switch(k))
            wl.addWidget(tab)
            if closable:
                x = QPushButton("×")
                x.setCursor(Qt.CursorShape.PointingHandCursor)
                x.setToolTip("Close session")
                x.setStyleSheet(
                    f"QPushButton{{background:{bg};color:#7c8aa5;border:1px solid {bd};border-left:0;"
                    "border-top-right-radius:7px;border-bottom-right-radius:7px;"
                    "padding:3px 7px 3px 3px;font:700 12px 'Segoe UI';}"
                    "QPushButton:hover{color:#f87171;}")
                x.clicked.connect(lambda _=False, k=i: self._on_close(k))
                wl.addWidget(x)
            self._lay.addWidget(wrap)
        plus = QPushButton("＋")
        plus.setCursor(Qt.CursorShape.PointingHandCursor)
        plus.setToolTip("New session")
        plus.setStyleSheet(self._TAB.format(bg="rgba(255,255,255,0.04)", fg="#8a96aa", bd="#232c3a"))
        plus.clicked.connect(lambda: self._on_new())
        self._lay.addWidget(plus)
        self._lay.addStretch(1)


class Hud(ResizableMixin, QWidget):
    """The single overlay window: status glyph + dual meters in a top BAR,
    with the streaming answer area stacked beneath it."""

    event_sig = Signal(dict)
    ui_sig = Signal(str)

    MIN_W = 280
    BAR_H = 58                 # height of the status/meter strip at the top
    DEFAULT_H = 320            # expanded height when no state is saved

    def __init__(self, co, config_url: str):
        super().__init__()
        self.co = co
        self.config_url = config_url
        self.settings = None
        self.setWindowFlags(_OVERLAY_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.init_resize(min_w=self.MIN_W, min_h=self.BAR_H)

        st = load_state()
        self._state = "idle"
        self._phase = 0
        self._drag: QPoint | None = None
        self._moved = False
        self._int_disp = 0.0
        self._me_disp = 0.0
        self._answer_expanded = bool(st.get("answer_expanded", True))

        # tabs + answer area live below the bar; the layout reserves the strip
        self.tabs = SessionTabs(self._switch_session, self._new_session, self._close_session)
        self.answer = AnswerView()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, self.BAR_H, 1, 1)
        lay.setSpacing(0)
        lay.addWidget(self.tabs)
        lay.addWidget(self.answer)
        self.tabs.setVisible(self._answer_expanded)
        self.answer.setVisible(self._answer_expanded)
        self.tabs.render(co.sessions_meta(), co.active)

        w = int(st.get("hud_w", 360))
        h = int(st.get("hud_h", self.DEFAULT_H)) if self._answer_expanded else self.BAR_H
        self.resize(w, h)

        self.event_sig.connect(self._on_event)
        self.ui_sig.connect(self._on_ui)
        co.listeners.append(self.event_sig.emit)

        self._tick = QTimer(self)
        self._tick.setInterval(50)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._place()
        self._apply_overlay_cfg()
        if bool(st.get("hud_visible", True)):
            self.show()

    def _apply_overlay_cfg(self):
        """Opacity + capture-exclusion from config (called at start and after
        Settings → Save)."""
        ov = CFG.get("overlay", {}) or {}
        self.setWindowOpacity(max(0.2, min(1.0, float(ov.get("opacity", 1.0)))))
        apply_capture_exclusion(self, _capture_hidden())

    # events ------------------------------------------------------------

    def _set(self, state: str):
        self._state = state
        self.update()

    def _on_event(self, m: dict):
        t = m.get("type")
        if t == "transcript":
            # live transcription line (interviewer / me) → a chat bubble
            self._set_answer_expanded(True)
            self.answer.add_message(m.get("speaker", ""), m.get("text", ""))
        elif t == "answer_start":
            self._set("answering")
            self._set_answer_expanded(True)        # an answer forces the panel open
            self.answer.begin(m.get("question", ""))
        elif t == "cleared":
            self.answer.reset()
        elif t == "answer_delta":
            self.answer.add(m.get("text", ""), m.get("provider"))
        elif t == "answer_done":
            self.answer.finish()
            self._set("idle")
        elif t == "answer_error":
            self.answer.error(m.get("text", ""))
            self._flash_error()
        elif t == "pipeline_idle":
            self._set("idle")
        elif t == "sessions":
            self.tabs.render(m.get("list", []), m.get("active", 0))
        elif t == "session_activated":
            self.answer.load_transcript(m.get("transcript", []))
        elif t == "settings_applied":
            self._apply_overlay_cfg()
        elif t == "status":
            is_err = m.get("level") == "error"
            self.answer.set_status(m.get("text", ""), error=is_err)
            if is_err:
                self._set_answer_expanded(True)   # make the error visible
                self._flash_error()

    def _flash_error(self):
        self._set("error")
        QTimer.singleShot(2500, lambda: self._set("idle") if self._state == "error" else None)

    def _on_ui(self, cmd: str):
        if cmd == "toggle_answer":
            self._toggle_answer()
        elif cmd == "toggle_hud":
            self._toggle_hud()
        elif cmd == "toggle_settings" and self.settings is not None:
            self.settings.toggle()
        elif cmd == "toggle_record":
            self._toggle_recording()

    def _toggle_hud(self):
        if self.isVisible():
            self.hide()
            save_state(hud_visible=False)
        else:
            self.show()
            self.raise_()
            apply_capture_exclusion(self, _capture_hidden())
            save_state(hud_visible=True)

    def toggle_answer_threadsafe(self):
        self.ui_sig.emit("toggle_answer")

    def toggle_hud_threadsafe(self):
        self.ui_sig.emit("toggle_hud")

    def toggle_settings_threadsafe(self):
        self.ui_sig.emit("toggle_settings")

    def toggle_record_threadsafe(self):
        self.ui_sig.emit("toggle_record")

    # answer collapse/expand -------------------------------------------

    def _set_answer_expanded(self, on: bool):
        if on == self._answer_expanded and self.answer.isVisible() == on:
            return
        self._answer_expanded = on
        self.tabs.setVisible(on)
        self.answer.setVisible(on)
        if on:
            h = int(load_state().get("hud_h", self.DEFAULT_H))
            self.resize(self.width(), max(h, self.BAR_H + 60))
        else:
            self.resize(self.width(), self.BAR_H)
        save_state(answer_expanded=on)

    def _toggle_answer(self):
        self._set_answer_expanded(not self._answer_expanded)

    # sessions / tabs ---------------------------------------------------

    def _new_session(self):
        self._set_answer_expanded(True)
        self.co.new_session()

    def _switch_session(self, idx: int):
        self._set_answer_expanded(True)
        self.co.switch_session(idx)

    def _close_session(self, idx: int):
        self.co.close_session(idx)

    # placement / geometry ---------------------------------------------

    def _place(self):
        s = load_state()
        if "hud_x" in s and "hud_y" in s:
            self.move(int(s["hud_x"]), int(s["hud_y"]))
        else:
            self.reset_position()

    def reset_position(self):
        screen = self.screen()
        if screen is None:
            return
        g = screen.availableGeometry()
        self.move(g.x() + (g.width() - self.width()) // 2,
                  g.y() + g.height() - self.height() - 54)
        save_state(hud_x=self.x(), hud_y=self.y())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # only remember height while expanded, so collapsing doesn't clobber it
        if self._answer_expanded:
            save_state(hud_w=self.width(), hud_h=self.height())
        else:
            save_state(hud_w=self.width())

    def showEvent(self, e):
        super().showEvent(e)
        apply_capture_exclusion(self, _capture_hidden())

    # mouse -------------------------------------------------------------

    def mousePressEvent(self, e: QMouseEvent):
        if self.rz_press(e):
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            self._press_x = e.position().x()
            self._press_y = e.position().y()

    def mouseMoveEvent(self, e: QMouseEvent):
        if self.rz_move(e):
            return
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            new = e.globalPosition().toPoint() - self._drag
            if (new - self.frameGeometry().topLeft()).manhattanLength() > 3:
                self._moved = True
            self.move(new)

    def mouseReleaseEvent(self, e: QMouseEvent):
        if self.rz_release():
            return
        if self._drag is not None:
            save_state(hud_x=self.x(), hud_y=self.y())
            # click (no drag) on the left status zone of the BAR = record toggle
            if not self._moved and e.button() == Qt.MouseButton.LeftButton \
                    and getattr(self, "_press_x", 99) < 52 \
                    and getattr(self, "_press_y", 99) < self.BAR_H:
                self._toggle_recording()
        self._drag = None

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#151a24;border:1px solid #1e2636;border-radius:8px;"
            "padding:6px;color:#e6ebf2;font-size:12.5px;font-family:'Segoe UI';}"
            "QMenu::item{padding:7px 22px 7px 12px;border-radius:5px;margin:1px 2px;}"
            "QMenu::item:selected{background:#1a2030;}"
            "QMenu::separator{height:1px;background:#1e2636;margin:4px 6px;}")
        rec = menu.addAction("Ask AI now")
        rec.triggered.connect(self._toggle_recording)
        ans = menu.addAction("Collapse answer" if self._answer_expanded else "Expand answer")
        ans.triggered.connect(self._toggle_answer)
        menu.addSeparator()
        cfg = menu.addAction("Settings…")
        cfg.triggered.connect(lambda: self.settings.toggle() if self.settings else webbrowser.open(self.config_url))
        clr = menu.addAction("Clear session")
        clr.triggered.connect(self.co.clear)
        rp = menu.addAction("Reset position")
        rp.triggered.connect(self.reset_position)
        menu.addSeparator()
        q = menu.addAction("Quit Interview Copilot")
        q.triggered.connect(lambda: __import__("os")._exit(0))
        menu.exec(e.globalPos())

    def _toggle_recording(self):
        # continuous listening never stops — the hotkey/click just sends the
        # current turn (interviewer question + my response) to the LLM.
        self.co.ask_now()

    # painting ----------------------------------------------------------

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        B = self.BAR_H

        # window shell — subtle state-tinted border over the whole window
        p.setBrush(QBrush(QColor(13, 17, 23, 247)))
        p.setPen(QPen(_ACCENT.get(self._state, _ACCENT["idle"]), 1.4))
        p.drawRoundedRect(QRectF(0.7, 0.7, W - 1.4, H - 1.4), 14, 14)

        # ── top BAR (fixed strip) ──
        self._draw_status(p, 27, B / 2)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
        p.drawLine(51, 14, 51, B - 14)

        lx = 62
        bx = lx + 78
        bw = W - bx - 14
        self._meter(p, lx, bx, bw, B * 0.32, "INTERVIEWER", self._int_disp, INT_COLOR)
        self._meter(p, lx, bx, bw, B * 0.68, "YOU", self._me_disp, ME_COLOR)

        # divider between the bar and the answer area (only when expanded)
        if self._answer_expanded and H > B + 2:
            p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
            p.drawLine(12, B, W - 12, B)
        p.end()

    def _meter(self, p, lx, bx, bw, cy, label, lvl, color):
        # channel label
        p.setPen(QPen(QColor(176, 188, 206)))
        f = p.font(); f.setPointSizeF(7.0); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(lx, cy - 8, 74, 16), Qt.AlignmentFlag.AlignVCenter, label)

        # level meter — a row of vertical bars that FILLS left→right with the
        # channel intensity. Bars up to the level are lit (vertical gradient),
        # the leading bar tapers by the fractional part, the rest is faint track.
        bar_w, gap = 3.4, 3.2
        pitch = bar_w + gap
        n = max(1, int((bw + gap) / pitch))
        max_h = 17.0
        lvl = max(0.0, min(1.0, lvl))
        lit = lvl * n                               # how many bars are lit
        top = QColor(color).lighter(165)            # bright tint at the top
        base = QColor(color)
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            x = bx + i * pitch
            # faint track behind every bar
            p.setBrush(QBrush(QColor(255, 255, 255, 20)))
            p.drawRoundedRect(QRectF(x, cy - 1.3, bar_w, 2.6), 1.1, 1.1)
            frac = min(1.0, max(0.0, lit - i))       # this bar's lit fraction
            if frac <= 0.02:
                continue
            h = max_h * (0.4 + 0.6 * frac)           # full height once fully lit; tapered head
            grad = QLinearGradient(0.0, cy - h / 2, 0.0, cy + h / 2)
            grad.setColorAt(0.0, top)
            grad.setColorAt(1.0, base)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x, cy - h / 2, bar_w, h), 1.5, 1.5)

    # ── status glyphs ──

    def _draw_status(self, p, cx, cy):
        s = self._state
        if s == "recording":
            self._g_record(p, cx, cy)
        elif s == "processing":
            self._g_processing(p, cx, cy)
        elif s == "answering":
            self._g_answering(p, cx, cy)
        elif s == "error":
            self._g_error(p, cx, cy)
        else:
            self._g_idle(p, cx, cy)

    def _g_idle(self, p, cx, cy):
        breathe = 0.7 + 0.3 * math.sin(self._phase / 22.0)
        v = int(max(60, min(220, 150 + 55 * math.sin(self._phase / 44.0))))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(v, v, v, int(235 * breathe))))
        for i, bh in enumerate((7.0, 11.0, 15.0, 11.0, 7.0)):
            h = bh * breathe
            p.drawRoundedRect(QRectF(cx - 10 + i * 5.0, cy - h / 2, 3.0, h), 1.2, 1.2)

    def _g_record(self, p, cx, cy):
        pulse = 0.55 + 0.45 * abs(math.sin(self._phase / 9.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(248, 113, 113, int(90 * pulse))))
        p.drawEllipse(QRectF(cx - 7, cy - 7, 14, 14))
        p.setBrush(QBrush(QColor(248, 113, 113)))
        p.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))

    def _g_processing(self, p, cx, cy):
        p.setPen(Qt.PenStyle.NoPen)
        base = self._phase * 0.2
        for i, (sz, a) in enumerate(((2.2, 235), (1.7, 150), (1.2, 80))):
            ang = base - i * 0.55
            p.setBrush(QBrush(QColor(245, 158, 11, a)))
            p.drawEllipse(QRectF(cx + 6.5 * math.cos(ang) - sz, cy + 6.5 * math.sin(ang) - sz, 2 * sz, 2 * sz))

    def _g_answering(self, p, cx, cy):
        tw = 0.78 + 0.22 * math.sin(self._phase / 6.0)
        rot = math.radians(self._phase * 2.6)
        cr, sr = math.cos(rot), math.sin(rot)
        arm, waist = 6.4 * tw, 1.5
        pts = ((0, -arm), (waist, -waist), (arm, 0), (waist, waist),
               (0, arm), (-waist, waist), (-arm, 0), (-waist, -waist))
        path = QPainterPath()
        for j, (x, y) in enumerate(pts):
            wx, wy = cx + x * cr - y * sr, cy + x * sr + y * cr
            path.moveTo(wx, wy) if j == 0 else path.lineTo(wx, wy)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(167, 139, 250, int(235 * tw))))
        p.drawPath(path)

    def _g_error(self, p, cx, cy):
        pulse = abs(math.sin(self._phase / 4.5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(248, 113, 113, int(70 * pulse))))
        p.drawEllipse(QRectF(cx - 7 - 2 * pulse, cy - 7 - 2 * pulse, 14 + 4 * pulse, 14 + 4 * pulse))
        path = QPainterPath()
        path.moveTo(cx + 0.5, cy - 5.5); path.lineTo(cx - 2.5, cy + 0.5)
        path.lineTo(cx - 0.5, cy + 0.5); path.lineTo(cx - 1.2, cy + 5.5)
        path.lineTo(cx + 2.5, cy - 0.5); path.lineTo(cx + 0.5, cy - 0.5)
        path.closeSubpath()
        p.setBrush(QBrush(QColor(248, 113, 113)))
        p.drawPath(path)

    def _on_tick(self):
        self._phase = (self._phase + 1) % 36000
        # smooth level decay for a polished VU feel
        it = self.co.levels_int[-1] if self.co.levels_int else 0.0
        me = self.co.levels_me[-1] if self.co.levels_me else 0.0
        self._int_disp = max(it, self._int_disp * 0.78)
        self._me_disp = max(me, self._me_disp * 0.78)
        self.update()
