"""Frameless always-on-top overlay.

Two pieces:
  Hud        — ONE compact bar merging the status indicator and both live
               level meters (INTERVIEWER + YOU). Click the status dot to
               record; drag the bar to move; right-click for the menu.
  AnswerCard — separate panel that streams ONLY the AI answer. Resizable
               from every edge and corner; size + position persisted.
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
    QHBoxLayout, QLabel, QMenu, QScrollArea, QVBoxLayout, QWidget,
)

from .uikit import ResizableMixin, load_state, save_state

_OVERLAY_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
)

INT_COLOR = QColor(242, 184, 75)     # interviewer — amber
ME_COLOR = QColor(52, 201, 142)      # you — green

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


class AnswerCard(ResizableMixin, QWidget):
    """Streams the AI answer. Resizable from all edges/corners."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(_OVERLAY_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.init_resize(min_w=260, min_h=140)
        st = load_state()
        self.resize(int(st.get("answer_w", 480)), int(st.get("answer_h", 300)))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 12)
        lay.setSpacing(7)

        self._hdr = QLabel("AI answers · one continuous session")
        self._hdr.setStyleSheet(
            "color:#5b6a86; font:600 10px 'Segoe UI'; letter-spacing:0.06em; background:transparent;")
        lay.addWidget(self._hdr)

        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._body.setStyleSheet("color:#dbe2ec; font:13px 'Segoe UI'; background:transparent;")

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._body)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;}"
            "QScrollBar:vertical{background:transparent;width:8px;}"
            "QScrollBar::handle:vertical{background:#3a4563;border-radius:4px;min-height:24px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}")
        self._scroll.viewport().setStyleSheet("background:transparent;")
        lay.addWidget(self._scroll, 1)

        self._foot = QLabel()
        self._foot.setStyleSheet("color:#8a96aa; font:10px 'Segoe UI'; background:transparent;")
        lay.addWidget(self._foot)

        self._committed = ""      # HTML of finished Q&As this session
        self._cur_q = ""
        self._raw = ""
        self._dirty = False
        self._drag: QPoint | None = None
        self._render = QTimer(self)
        self._render.setInterval(120)
        self._render.timeout.connect(self._flush)

    # streaming (Qt thread) --------------------------------------------

    def reset(self):
        """New session — wipe the accumulated conversation."""
        self._committed = ""
        self._cur_q = ""
        self._raw = ""
        self._body.setText("")
        self._foot.setText("")

    def begin(self, question: str):
        self._commit()                 # fold the previous answer into history
        self._cur_q = question
        self._raw = ""
        self._render.start()
        self._render_now(thinking=True)
        self.show()
        self.raise_()

    def add(self, delta: str, provider: str | None):
        self._raw += delta
        self._dirty = True
        if provider:
            self._foot.setText(f"via {provider}")

    def finish(self):
        self._commit()
        self._render_now()
        self._render.stop()

    def error(self, text: str):
        self._raw += f"\n**Error:** {text}"
        self._render_now()

    def _commit(self):
        if self._cur_q or self._raw.strip():
            self._committed += self._block(self._cur_q, self._raw)
            self._committed += "<div style='border-top:1px solid #232c3a;margin:11px 0;'></div>"
        self._cur_q = ""
        self._raw = ""

    @staticmethod
    def _block(q: str, raw: str) -> str:
        head = (f"<div style='color:#f2b84b;font-weight:600;margin-bottom:4px;'>{html.escape(q)}</div>"
                if q else "")
        return head + _md_html(raw)

    def _flush(self):
        if self._dirty:
            self._render_now()

    def _render_now(self, thinking: bool = False):
        self._dirty = False
        if thinking and not self._raw:
            cur = self._block(self._cur_q, "") + "<i style='color:#8a96aa'>thinking…</i>"
        else:
            cur = self._block(self._cur_q, self._raw) if (self._cur_q or self._raw) else ""
        self._body.setText(self._committed + cur)
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # chrome ------------------------------------------------------------

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(13, 17, 23, 246)))
        p.setPen(QPen(QColor(42, 51, 66), 1.0))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 13, 13)
        p.end()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        save_state(answer_w=self.width(), answer_h=self.height())

    def place_near(self, hud: QWidget):
        g = hud.frameGeometry()
        screen = hud.screen()
        if screen is None:
            return
        sg = screen.availableGeometry()
        x = min(max(sg.x() + 8, g.center().x() - self.width() // 2),
                sg.x() + sg.width() - self.width() - 8)
        y = g.top() - self.height() - 10
        if y < sg.y() + 8:
            y = g.bottom() + 10
        self.move(x, y)

    def mousePressEvent(self, e: QMouseEvent):
        if self.rz_press(e):
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e: QMouseEvent):
        if self.rz_move(e):
            return
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self.rz_release()
        self._drag = None


class Hud(ResizableMixin, QWidget):
    """Single bar: status glyph on the left + both live meters. Merged from
    the old separate pill and meter widgets."""

    event_sig = Signal(dict)
    ui_sig = Signal(str)

    MIN_W, MIN_H = 250, 58

    def __init__(self, co, config_url: str):
        super().__init__()
        self.co = co
        self.config_url = config_url
        self.settings = None
        self.setWindowFlags(_OVERLAY_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.init_resize(min_w=self.MIN_W, min_h=self.MIN_H)

        st = load_state()
        self.resize(int(st.get("hud_w", 330)), int(st.get("hud_h", self.MIN_H)))

        self._state = "idle"
        self._phase = 0
        self._drag: QPoint | None = None
        self._moved = False
        self._int_disp = 0.0
        self._me_disp = 0.0

        self.card = AnswerCard()

        self.event_sig.connect(self._on_event)
        self.ui_sig.connect(self._on_ui)
        co.listeners.append(self.event_sig.emit)

        self._tick = QTimer(self)
        self._tick.setInterval(50)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        self._place()
        self.show()

    # events ------------------------------------------------------------

    def _set(self, state: str):
        self._state = state
        self.update()

    def _on_event(self, m: dict):
        t = m.get("type")
        if t == "recording":
            # keep the previous answer visible while recording the next question
            self._set("recording" if m.get("on") else "processing")
        elif t == "answer_start":
            self._set("answering")
            was_visible = self.card.isVisible()
            self.card.begin(m.get("question", ""))
            if not was_visible:
                self.card.place_near(self)
        elif t == "cleared":
            self.card.reset()
        elif t == "answer_delta":
            self.card.add(m.get("text", ""), m.get("provider"))
        elif t == "answer_done":
            self.card.finish()
            self._set("idle")
        elif t == "answer_error":
            self.card.error(m.get("text", ""))
            self._flash_error()
        elif t == "pipeline_idle":
            self._set("idle")
        elif t == "status" and m.get("level") == "error":
            self._flash_error()

    def _flash_error(self):
        self._set("error")
        QTimer.singleShot(2500, lambda: self._set("idle") if self._state == "error" else None)

    def _on_ui(self, cmd: str):
        if cmd == "toggle_answer":
            self._toggle_card()
        elif cmd == "toggle_hud":
            self._toggle_hud()
        elif cmd == "toggle_settings" and self.settings is not None:
            self.settings.toggle()
        elif cmd == "toggle_record":
            self._toggle_recording()

    def _toggle_hud(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def toggle_answer_threadsafe(self):
        self.ui_sig.emit("toggle_answer")

    def toggle_hud_threadsafe(self):
        self.ui_sig.emit("toggle_hud")

    def toggle_settings_threadsafe(self):
        self.ui_sig.emit("toggle_settings")

    def toggle_record_threadsafe(self):
        self.ui_sig.emit("toggle_record")

    # placement ---------------------------------------------------------

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
        save_state(hud_w=self.width(), hud_h=self.height())

    # mouse -------------------------------------------------------------

    def mousePressEvent(self, e: QMouseEvent):
        if self.rz_press(e):
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
            self._press_x = e.position().x()

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
            if not self._moved and e.button() == Qt.MouseButton.LeftButton \
                    and getattr(self, "_press_x", 99) < 52:
                self._toggle_recording()   # click the status zone to record
        self._drag = None

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#151a24;border:1px solid #1e2636;border-radius:8px;"
            "padding:6px;color:#e6ebf2;font-size:12.5px;font-family:'Segoe UI';}"
            "QMenu::item{padding:7px 22px 7px 12px;border-radius:5px;margin:1px 2px;}"
            "QMenu::item:selected{background:#1a2030;}"
            "QMenu::separator{height:1px;background:#1e2636;margin:4px 6px;}")
        rec = menu.addAction("Stop recording" if self.co.recording else "Start recording")
        rec.triggered.connect(self._toggle_recording)
        ans = menu.addAction("Hide answer" if self.card.isVisible() else "Show last answer")
        ans.triggered.connect(self._toggle_card)
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
        self.co.record_stop() if self.co.recording else self.co.record_start()

    def _toggle_card(self):
        if self.card.isVisible():
            self.card.hide()
        else:
            self.card.place_near(self)
            self.card.show()

    # painting ----------------------------------------------------------

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        # shell — subtle state-tinted border
        p.setBrush(QBrush(QColor(13, 17, 23, 247)))
        p.setPen(QPen(_ACCENT.get(self._state, _ACCENT["idle"]), 1.4))
        p.drawRoundedRect(QRectF(0.7, 0.7, W - 1.4, H - 1.4), 14, 14)

        # left status zone
        self._draw_status(p, 27, H / 2)
        # divider
        p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
        p.drawLine(51, 14, 51, H - 14)

        # meters
        lx = 62
        bx = lx + 78
        bw = W - bx - 14
        self._meter(p, lx, bx, bw, H * 0.32, "INTERVIEWER", self._int_disp, INT_COLOR)
        self._meter(p, lx, bx, bw, H * 0.68, "YOU", self._me_disp, ME_COLOR)
        p.end()

    def _meter(self, p, lx, bx, bw, cy, label, lvl, color):
        p.setPen(QPen(QColor(150, 162, 180)))
        f = p.font(); f.setPointSizeF(7.0); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(lx, cy - 8, 74, 16), Qt.AlignmentFlag.AlignVCenter, label)
        bh = 7.0
        # track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(255, 255, 255, 16)))
        p.drawRoundedRect(QRectF(bx, cy - bh / 2, bw, bh), bh / 2, bh / 2)
        # fill w/ gradient + soft glow
        w = max(0.0, bw * min(1.0, lvl))
        if w > 1:
            glow = QColor(color); glow.setAlpha(70)
            p.setBrush(QBrush(glow))
            p.drawRoundedRect(QRectF(bx, cy - bh / 2 - 1.2, w, bh + 2.4), (bh + 2.4) / 2, (bh + 2.4) / 2)
            grad = QLinearGradient(bx, 0, bx + bw, 0)
            c0 = QColor(color); c0.setAlpha(210)
            grad.setColorAt(0.0, c0); grad.setColorAt(1.0, color)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(bx, cy - bh / 2, w, bh), bh / 2, bh / 2)

    # ── status glyphs (from voxtype pill) ──

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
