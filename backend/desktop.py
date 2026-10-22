"""ClipFactory desktop application: the local server plus a native window (Microsoft Edge WebView2).

    ClipFactory.exe                 normal start
    ClipFactory.exe --updated       started by the installer after an update
    ClipFactory.exe --selftest      start, wait until the UI has loaded in the window, exit (0 = OK)
    ClipFactory.exe --server-only   no window (diagnostics)

Only one copy runs at a time: starting it again brings the existing window to the front.
If WebView2 is unavailable, the UI opens in an Edge app window, and as the last resort in the browser.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

MUTEX_NAME = "ClipFactoryAppMutex"  # also checked by the installer (packaging/installer.iss)
log = logging.getLogger("clipfactory.desktop")


# ---------------------------------------------------------------------------------------------- helpers

def _fix_streams(log_dir: Path | None) -> None:
    """A windowed exe has no console: send stray prints to a file instead of crashing on None."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    target = None
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            target = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            target = None
    target = target or open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = target
    if sys.stderr is None:
        sys.stderr = target


def message_box(text: str, title: str = "ClipFactory", error: bool = True) -> None:
    if sys.platform == "win32":
        import ctypes

        flags = 0x00040000 | (0x10 if error else 0x40)  # MB_TOPMOST | MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def ask_yes_no(text: str, title: str = "ClipFactory") -> bool:
    if sys.platform != "win32":
        return True
    import ctypes

    return ctypes.windll.user32.MessageBoxW(None, text, title, 0x00040000 | 0x04 | 0x30 | 0x100) == 6  # Yes


class SingleInstance:
    """Named mutex on Windows (the installer waits on the same name); lock file elsewhere."""

    def __init__(self) -> None:
        self.handle = None
        self.already_running = False

    def acquire(self) -> bool:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            self.handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
            self.already_running = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
            return not self.already_running
        return True

    def release(self) -> None:
        if self.handle and sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


def _local_request(port: int, path: str, method: str = "GET", timeout: float = 2.0) -> dict | None:
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=b"{}" if method == "POST" else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------- window state

class WindowState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = {"width": 1360, "height": 860, "x": None, "y": None, "maximized": False}
        try:
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.data["width"] = max(1024, min(int(self.data.get("width") or 1360), 7680))
        self.data["height"] = max(680, min(int(self.data.get("height") or 860), 4320))
        for k in ("x", "y"):  # never restore a window far off-screen
            v = self.data.get(k)
            self.data[k] = v if isinstance(v, int) and -100 < v < 6000 else None

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data), encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------- the app

class DesktopApp:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.selftest = "--selftest" in args
        self.server_only = "--server-only" in args
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.window = None
        self.quit_event = threading.Event()
        self.installer_to_run: Path | None = None
        self.exit_code = 0

    # ---- server
    def start_server(self) -> None:
        import uvicorn

        from backend import config
        from backend.main import _port_free, app

        port = config.PORT
        if not _port_free(config.HOST, port):
            for candidate in range(port + 1, port + 30):
                if _port_free(config.HOST, candidate):
                    port = candidate
                    break
            else:
                raise RuntimeError(f"Нет свободного порта рядом с {port}.")
        config.PORT = port
        cfg = uvicorn.Config(app, host=config.HOST, port=port, log_level="warning", log_config=None,
                             timeout_graceful_shutdown=10, lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.server_thread = threading.Thread(target=self.server.run, name="server", daemon=True)
        self.server_thread.start()
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.server.started:
                break
            if not self.server_thread.is_alive():
                raise RuntimeError("Сервер ClipFactory не запустился. Подробности — в журнале.")
            time.sleep(0.05)
        else:
            raise RuntimeError("Сервер ClipFactory не ответил за 90 секунд.")
        (config.STORAGE / "runtime.json").write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            self.server_thread.join(timeout=25)

    @property
    def url(self) -> str:
        from backend import config

        return f"http://127.0.0.1:{config.PORT}/"

    # ---- hooks for the API / updater
    def request_quit(self) -> None:
        self.quit_event.set()
        if self.window is not None:
            try:
                self._force_close = True
                self.window.destroy()
            except Exception:
                log.exception("window destroy failed")

    def run_installer_after_exit(self, path: Path) -> None:
        # Called from an API request: answer it first, then close the window and hand over to the installer.
        self.installer_to_run = path
        threading.Timer(0.5, self.request_quit).start()

    def show_window(self) -> None:
        w = self.window
        if w is None:
            return
        try:
            w.restore()
            w.show()
            w.on_top = True
            w.on_top = False
        except Exception:
            log.exception("show window failed")

    # ---- window
    def _jobs_running(self) -> int:
        from backend.database import db

        try:
            return db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')") or 0
        except Exception:
            return 0

    def run_webview(self) -> bool:
        try:
            import webview
        except Exception:
            log.exception("pywebview is not available")
            return False
        from backend import config
        from backend.services.shell import shell

        state = WindowState(config.STORAGE / "window.json")
        self._force_close = False
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        try:
            webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
        except Exception:
            pass
        d = state.data
        self.window = webview.create_window(
            "ClipFactory", self.url, width=d["width"], height=d["height"], x=d["x"], y=d["y"],
            min_size=(1024, 680), background_color="#111318", text_select=True, maximized=bool(d["maximized"]))
        w = self.window

        def on_closing():
            try:
                if not d["maximized"]:  # keep the normal size so un-maximizing later looks right
                    d.update({"width": w.width, "height": w.height, "x": w.x, "y": w.y})
            except Exception:
                pass
            state.save()
            if self._force_close or self.selftest:
                return True
            n = self._jobs_running()
            if n and not ask_yes_no("Идёт обработка видео. Закрыть ClipFactory?\n\n"
                                    "Незавершённые задачи продолжатся автоматически при следующем запуске."):
                return False
            return True

        def on_maximized():
            d["maximized"] = True

        def on_restored():
            d["maximized"] = False

        w.events.closing += on_closing
        w.events.maximized += on_maximized
        w.events.restored += on_restored

        def save_dialog(name: str):
            res = w.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
            if isinstance(res, (list, tuple)):
                res = res[0] if res else None
            return res

        shell.mode = "webview"
        shell.show = self.show_window
        shell.quit = self.request_quit
        shell.save_dialog = save_dialog

        if self.selftest:
            threading.Thread(target=self._selftest_watch, daemon=True).start()
        gui = "edgechromium" if sys.platform == "win32" else None
        try:
            webview.start(gui=gui, private_mode=False, storage_path=str(config.DIRS["webview"]))
        except Exception:
            log.exception("webview failed to start")
            shell.mode = "server"
            shell.save_dialog = None
            self.window = None
            return False
        return True

    def _selftest_watch(self) -> None:
        from backend.services.shell import shell

        ok = shell.ready.wait(120)
        self.exit_code = 0 if ok else 3
        log.info("selftest: UI %s", "loaded" if ok else "did not load")
        time.sleep(1.5)
        self.request_quit()

    def run_edge_app(self) -> bool:
        """Fallback: Microsoft Edge in app mode (a plain window without browser controls)."""
        if sys.platform != "win32":
            return False
        import subprocess

        from backend import config
        from backend.services.shell import shell

        candidates = [Path(os.environ.get(v, "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
                      for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        edge = next((c for c in candidates if c.is_file()), None)
        if not edge:
            return False
        profile = config.STORAGE / "edge-profile"
        proc = subprocess.Popen([str(edge), f"--app={self.url}", f"--user-data-dir={profile}", "--no-first-run",
                                 "--disable-features=Translate", "--window-size=1360,860"])
        shell.mode = "edge"
        shell.quit = self.request_quit
        while proc.poll() is None and not self.quit_event.is_set():
            time.sleep(0.5)
        if proc.poll() is None:
            proc.terminate()
        return True

    def run_browser(self) -> None:
        import webbrowser

        from backend.services.shell import shell

        shell.mode = "browser"
        shell.quit = self.request_quit
        webbrowser.open(self.url)
        self.quit_event.wait()

    # ---- main
    def run(self) -> int:
        from backend import config
        from backend.services.updater import launch_installer, updater

        instance = SingleInstance()
        if not instance.acquire():
            info = None
            try:
                info = json.loads((config.STORAGE / "runtime.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            if not (info and _local_request(int(info["port"]), "/api/desktop/show", "POST")):
                message_box("ClipFactory уже запущен.", error=False)
            return 0
        try:
            from backend.main import setup_logging

            config.ensure_dirs()
            setup_logging()
            log.info("starting ClipFactory %s (%s)", config.APP_VERSION, " ".join(self.args))
            try:
                self.start_server()
            except Exception as exc:
                log.exception("server start failed")
                message_box(f"Не удалось запустить ClipFactory.\n\n{exc}\n\nЖурнал: {config.DIRS['logs']}")
                return 1
            updater.installer_launcher = self.run_installer_after_exit
            if self.server_only:
                from backend.services.shell import shell

                shell.mode = "browser"
                shell.quit = self.request_quit
                self.quit_event.wait()
            elif not self.run_webview():
                if not self.run_edge_app():
                    self.run_browser()
            self.stop_server()
            if self.installer_to_run is not None:
                instance.release()
                launch_installer(self.installer_to_run)
            return self.exit_code
        finally:
            instance.release()


def main(argv: list[str] | None = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    args = list(sys.argv[1:] if argv is None else argv)
    from backend import config

    _fix_streams(config.STORAGE / "logs")
    try:
        return DesktopApp(args).run()
    except Exception as exc:  # last resort: never die silently
        logging.getLogger("clipfactory").exception("fatal error")
        message_box(f"ClipFactory завершился с ошибкой:\n\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
