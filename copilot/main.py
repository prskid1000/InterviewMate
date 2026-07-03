"""Entry point: FastAPI server in a background thread + Qt overlay in the
main thread. No system tray icon — the pill's right-click menu and the
global hotkeys from config.yaml are the only chrome."""
import os
import threading
import webbrowser

import uvicorn

from .config import CFG


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
        actions = {
            "record_toggle": hud.toggle_record_threadsafe,
            "open_config": hud.toggle_settings_threadsafe,
            "toggle_answer": hud.toggle_answer_threadsafe,
            "toggle_hud": hud.toggle_hud_threadsafe,
            "quit": lambda: os._exit(0),
        }
        mapping = {}
        for action, fn in actions.items():
            combo = hk.get(action) or (hk.get("hide_answer") if action == "toggle_answer" else None)
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

    print("Overlay running. Click the HUD status dot to record; right-click for the menu.")
    app.exec()
    os._exit(0)


if __name__ == "__main__":
    main()
