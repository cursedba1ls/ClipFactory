"""State shared between the desktop shell (backend/desktop.py) and the HTTP API.

The server never imports the GUI toolkit: the shell registers callbacks here when it owns a window.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable


class ShellState:
    def __init__(self) -> None:
        self.mode = "server"  # server | webview | edge | browser
        self.ready_at: float | None = None  # the UI finished loading in the window
        self.ready = threading.Event()
        self.show: Callable[[], None] | None = None
        self.quit: Callable[[], None] | None = None
        self.save_dialog: Callable[[str], str | None] | None = None
        self.pick_folder: Callable[[], str | None] | None = None

    @property
    def desktop(self) -> bool:
        return self.mode != "server"

    def mark_ready(self) -> None:
        self.ready_at = time.time()
        self.ready.set()


shell = ShellState()


def open_path(path: Path) -> None:
    """Show a file or folder in Windows Explorer (no-op elsewhere)."""
    import os
    import subprocess
    import sys

    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
