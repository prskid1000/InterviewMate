"""Entry point: FastAPI server in a background thread + Qt overlay in the
main thread. No system tray icon — the HUD's buttons and right-click menu are
the chrome; the one global hotkey (hotkeys.toggle_hud) shows / hides it."""
import os
import sys
import threading
import webbrowser

import uvicorn

from .config import CFG

# Under pythonw.exe (no console — e.g. launched from a Scheduled Task at logon)
# sys.stdout/sys.stderr are None, so any print() or uvicorn log write crashes the
# startup. Route them to a null sink so the app runs headless cleanly.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _to_pynput(combo: str) -> str:
    """'ctrl+alt+c' -> '<ctrl>+<alt>+c' (pynput GlobalHotKeys format)."""
    out = []
    for part in str(combo).split("+"):
        p = part.strip().lower()
        if not p:
            continue
        if p == "win":
            p = "cmd"
        out.append(p if len(p) == 1 else f"<{p}>")
    return "+".join(out)


def main():
    server_cfg = CFG.get("server", {})
    host = server_cfg.get("host", "127.0.0.1")
    port = int(server_cfg.get("port", 8765))
    url = f"http://{host}:{port}"

    threading.Thread(
        target=lambda: uvicorn.run("copilot.server:app", host=host, port=port,
                                   log_level="warning"),
        daemon=True,
    ).start()
    print(f"Interview Copilot — config UI: {url}")

    if not (CFG.get("overlay", {}) or {}).get("enabled", True):
        webbrowser.open(url)
        threading.Event().wait()
        return

    from PySide6.QtWidgets import QApplication

    from .overlay import Hud
    from .settings_ui import SettingsWindow
    from .server import CO

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)

    hud = Hud(CO, url)
    settings = SettingsWindow(CO)
    hud.settings = settings

    from pynput import keyboard
    _hk = {"listener": None}

    def build_hotkeys():
        """(Re)register global hotkeys from the current CFG. Called at startup
        and again whenever the settings window rebinds a key."""
        if _hk["listener"] is not None:
            try:
                _hk["listener"].stop()
            except Exception:
                pass
            _hk["listener"] = None
        hk = CFG.get("hotkeys", {}) or {}
        # everything else is a HUD button — older configs' other keys are ignored
        actions = {"toggle_hud": hud.toggle_hud_threadsafe}
        mapping = {}
        for action, fn in actions.items():
            combo = hk.get(action)
            if combo:
                try:
                    mapping[_to_pynput(combo)] = fn
                except Exception:
                    pass
        if mapping:
            lst = keyboard.GlobalHotKeys(mapping)
            lst.start()
            _hk["listener"] = lst
        print("Hotkeys: " + ", ".join(f"{a}={hk.get(a)}" for a in actions if hk.get(a)))

    build_hotkeys()
    settings.on_hotkeys_changed = build_hotkeys

    # ASCII only: a cp1252 console can't encode ▶ and the print would kill startup
    print("Overlay running. Press Listen on the HUD to start; right-click for the menu.")
    app.exec()
    os._exit(0)


if __name__ == "__main__":
    main()
