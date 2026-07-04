"""Shared overlay helpers: window-state persistence + an all-edge resize
mixin for frameless windows (resize from every corner and edge, with
matching cursor feedback)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QRect, Qt

STATE_FILE = Path(__file__).resolve().parent.parent / "overlay_state.json"

# ── screen-capture exclusion (Windows) ───────────────────────────────
# SetWindowDisplayAffinity keeps the window visible on the physical
# display but removes it from any capture pipeline — screen recorders,
# and screen-share in Meet/Zoom/Teams, PrintScreen, Snipping Tool.
_WDA_NONE = 0x00
_WDA_MONITOR = 0x01               # capture shows a black box (pre-2004 fallback)
_WDA_EXCLUDEFROMCAPTURE = 0x11    # capture shows nothing (Windows 10 2004+)


def apply_capture_exclusion(widget, enabled: bool = True) -> bool:
    """Hide/show `widget` from screen capture. Returns True on success.

    No-op (returns False) off Windows. Falls back to WDA_MONITOR on
    Windows builds older than 10 2004, which renders the window black in
    captures instead of fully absent."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(widget.winId())          # forces native handle creation
        if not hwnd:
            return False
        fn = ctypes.windll.user32.SetWindowDisplayAffinity
        fn.argtypes = [wintypes.HWND, wintypes.DWORD]   # HWND is pointer-sized
        fn.restype = wintypes.BOOL
        if not enabled:
            return bool(fn(hwnd, _WDA_NONE))
        if fn(hwnd, _WDA_EXCLUDEFROMCAPTURE):
            return True
        return bool(fn(hwnd, _WDA_MONITOR))
    except Exception:
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(**kv):
    st = load_state()
    st.update(kv)
    try:
        STATE_FILE.write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


_CURSORS = {
    "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
    "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
    "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
    "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
}


class ResizableMixin:
    """Adds 8-direction edge/corner resizing to a frameless widget.

    Call `init_resize()` in __init__, then from the widget's mouse handlers:
      - press:   `if self.rz_press(e): return`
      - move:    `if self.rz_move(e): return`   (also sets the resize cursor)
      - release: `self.rz_release()`
    Interior drags fall through so the widget can still be moved.
    """
    RESIZE_MARGIN = 9

    def init_resize(self, min_w: int = 220, min_h: int = 110):
        self.setMouseTracking(True)
        self.setMinimumSize(min_w, min_h)
        self._rz_edge = ""
        self._rz_active = ""
        self._rz_origin = None
        self._rz_geo = None

    def _edge_at(self, pos) -> str:
        m = self.RESIZE_MARGIN
        w, h = self.width(), self.height()
        e = ""
        if pos.y() <= m:
            e += "t"
        elif pos.y() >= h - m:
            e += "b"
        if pos.x() <= m:
            e += "l"
        elif pos.x() >= w - m:
            e += "r"
        return e

    def rz_move(self, e) -> bool:
        if self._rz_active and (e.buttons() & Qt.MouseButton.LeftButton):
            d = e.globalPosition().toPoint() - self._rz_origin
            g = QRect(self._rz_geo)
            minw, minh = self.minimumWidth(), self.minimumHeight()
            if "l" in self._rz_active:
                g.setLeft(min(g.left() + d.x(), g.right() - minw))
            if "r" in self._rz_active:
                g.setRight(max(g.right() + d.x(), g.left() + minw))
            if "t" in self._rz_active:
                g.setTop(min(g.top() + d.y(), g.bottom() - minh))
            if "b" in self._rz_active:
                g.setBottom(max(g.bottom() + d.y(), g.top() + minh))
            self.setGeometry(g)
            return True
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            edge = self._edge_at(e.position().toPoint())
            self.setCursor(_CURSORS.get(edge, Qt.CursorShape.ArrowCursor))
        return False

    def rz_press(self, e) -> bool:
        if e.button() != Qt.MouseButton.LeftButton:
            return False
        edge = self._edge_at(e.position().toPoint())
        if edge:
            self._rz_active = edge
            self._rz_origin = e.globalPosition().toPoint()
            self._rz_geo = self.geometry()
            return True
        return False

    def rz_release(self) -> bool:
        was = bool(self._rz_active)
        self._rz_active = ""
        return was
