"""
Vanta IDE — desktop shell.

Runs the Vanta HTTP server in-process and hosts the UI inside a native
WebView2 window. No browser chrome, no tabs, no address bar — its own window,
its own icon, its own close behaviour.

    python ide/desktop.py                    # native window, last project
    python ide/desktop.py C:\\work            # native window, that project
    python ide/desktop.py --web              # old behaviour: default browser
    python ide/desktop.py --selftest         # hidden window, DOM checks, exit

If pywebview / WebView2 is unavailable the shell degrades to an Edge
`--app` window (still chromeless), then to the default browser.

The window owns the server: closing it kills the shell sessions and the
AirLLM worker. If another Vanta server already answers on the port, the
window attaches to it instead and leaves it running.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = HERE if os.path.isdir(os.path.join(HERE, "web")) else os.path.dirname(HERE)
sys.path.insert(0, HERE)

import server as core  # noqa: E402

ICON = os.path.join(HERE, "assets", "vanta.ico")
LOG = os.path.join(PROJECT, "logs", "desktop.log")
LOCK = threading.Lock()

DEFAULT_SIZE = (1560, 970)
MIN_SIZE = (980, 620)


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #

def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    with LOCK:
        try:
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def probe(port: int, host: str = "127.0.0.1") -> bool:
    """True when a compatible Vanta server is already listening on host:port."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/ping", timeout=1.5) as r:
            data = json.loads(r.read(4096) or b"{}")
        if not data.get("vanta"):
            return False
        if core.is_admin() and not data.get("admin"):
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def remember_root(path: str):
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isdir(root):
        log(f"ignoring --root: not a directory: {root}")
        return
    state = core.read_state()
    recent = [r for r in state.get("recent", []) if os.path.normcase(r) != os.path.normcase(root)]
    state["recent"], state["lastRoot"] = [root] + recent[:11], root
    core.write_state(state)


def get_storage_dir(subdir: str = "") -> str:
    """Find a writable directory for webview state / browser profile."""
    candidates = []
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(os.path.join(local_app, "VantaIDE", subdir) if subdir else os.path.join(local_app, "VantaIDE"))
    candidates.append(os.path.join(PROJECT, ".vanta", subdir) if subdir else os.path.join(PROJECT, ".vanta"))
    import tempfile
    candidates.append(os.path.join(tempfile.gettempdir(), "VantaIDE", subdir) if subdir else os.path.join(tempfile.gettempdir(), "VantaIDE"))

    for target in candidates:
        try:
            os.makedirs(target, exist_ok=True)
            test_file = os.path.join(target, ".write_test")
            with open(test_file, "w") as f:
                f.write("1")
            os.remove(test_file)
            return target
        except OSError:
            continue
    fallback = os.path.join(tempfile.gettempdir(), "VantaIDE", subdir) if subdir else os.path.join(tempfile.gettempdir(), "VantaIDE")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
    return fallback


def dark_frame(hwnd: int):
    """Ask DWM for a dark caption so the native frame matches the app."""
    try:
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(on), 4)  # immersive dark
        cap = ctypes.c_int(10 | (12 << 8) | (15 << 16))                           # #0a0c0f
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 35, ctypes.byref(cap), 4)
        txt = ctypes.c_int(207 | (216 << 8) | (227 << 16))                        # #cfd8e3
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 36, ctypes.byref(txt), 4)
        bdr = ctypes.c_int(31 | (42 << 8) | (51 << 16))                           # #1f2a33
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 34, ctypes.byref(bdr), 4)
    except Exception as exc:  # noqa: BLE001
        log(f"DWM dark frame unavailable: {exc}")


def find_edge() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------- #
# JS bridge
# --------------------------------------------------------------------------- #

class Api:
    """window.pywebview.api.* — every public method is callable from the page."""

    def __init__(self, shell: "Shell"):
        self._shell = shell

    def app_info(self):
        return self._shell.info()

    def pick_folder(self, start: str = ""):
        return self._shell.pick_folder(start)

    def set_title(self, title: str = ""):
        if self._shell.window and title:
            try:
                self._shell.window.set_title(title[:120])
            except Exception as exc:  # noqa: BLE001
                log(f"set_title failed: {exc}")
        return True

    def minimize(self):
        self._shell.window.minimize()
        return True

    def toggle_maximize(self):
        return self._shell.toggle_maximize()

    def toggle_fullscreen(self):
        self._shell.window.toggle_fullscreen()
        return True

    def close_window(self):
        self._shell.request_close()
        return True

    def confirm_close(self):
        self._shell.force_close()
        return True

    def open_external(self, url: str = ""):
        if url.startswith(("http://", "https://")):
            webbrowser.open(url)
        return True

    def reveal(self, path: str = ""):
        self._shell.reveal(path)
        return True

    def log(self, msg: str = ""):
        log(f"[page] {msg}")
        return True

    def sync_state(self, payload=None):
        if isinstance(payload, dict):
            for key in ("dirty", "tabs", "root", "name"):
                if key in payload:
                    self._shell.page_state[key] = payload[key]
        return True


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #

class Shell:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.srv = None
        self.port = args.port
        self.url = None
        self.owns_server = False
        self.window = None
        self.destroying = False
        self.maximized = False
        self.selftest = None
        self.page_state = {"dirty": 0, "tabs": 0, "root": None, "name": None}

    # ------------------------------ server ------------------------------- #

    def ensure_server(self):
        host = self.args.host
        probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        if probe(self.args.port, probe_host):
            log(f"attaching to existing server on {probe_host}:{self.args.port}")
            self.port, self.url = self.args.port, f"http://{probe_host}:{self.args.port}"
            return
        last = None
        for want in (self.args.port, 0, 0):
            try:
                self.srv = core.build_server(host, want)
                break
            except OSError as exc:
                last = exc
                log(f"bind {host}:{want} failed: {exc}")
                time.sleep(0.3)
        if self.srv is None:
            raise RuntimeError(f"could not bind a port: {last}")
        self.port = self.srv.server_address[1]
        self.url = f"http://{probe_host}:{self.port}"
        self.owns_server = True
        threading.Thread(target=self.srv.serve_forever, name="vanta-http", daemon=True).start()
        ts = core.tailscale_ip()
        ts_note = f" tailscale=http://{ts}:{self.port}" if ts else ""
        log(f"server {self.url}{ts_note} root={self.args.root or core.read_state().get('lastRoot')}")

    # ------------------------------ window ------------------------------- #

    def info(self):
        return {
            "desktop": True,
            "port": self.port,
            "url": self.url,
            "root": core.read_state().get("lastRoot"),
            "pid": os.getpid(),
            "ownsServer": self.owns_server,
            "frameless": bool(self.args.frameless),
            "version": "1.1",
        }

    def pick_folder(self, start: str = ""):
        import webview
        start = start or core.read_state().get("lastRoot") or os.path.expanduser("~")
        try:
            picked = self.window.create_file_dialog(
                webview.FileDialog.FOLDER, directory=start, allow_multiple=False)
        except Exception as exc:  # noqa: BLE001
            log(f"folder dialog failed: {exc}")
            return ""
        if not picked:
            return ""
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        return str(path or "")

    def toggle_maximize(self):
        try:
            if self.maximized:
                self.window.restore()
            else:
                self.window.maximize()
        except Exception as exc:  # noqa: BLE001
            log(f"maximize failed: {exc}")
            return False
        self.maximized = not self.maximized
        return self.maximized

    def reveal(self, path: str):
        if not path or not os.path.exists(path):
            return
        try:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        except Exception as exc:  # noqa: BLE001
            log(f"reveal failed: {exc}")

    def request_close(self):
        """Close requested from the page (after its own unsaved-changes guard)."""
        self.destroying = True
        try:
            self.window.destroy()
        except Exception as exc:  # noqa: BLE001
            log(f"destroy failed: {exc}")

    def force_close(self):
        self.destroying = True
        try:
            self.window.destroy()
        except Exception as exc:  # noqa: BLE001
            log(f"destroy failed: {exc}")

    # ------------------------------ events ------------------------------- #

    def on_shown(self):
        try:
            hwnd = int(self.window.native.Handle.ToInt64())
            dark_frame(hwnd)
        except Exception as exc:  # noqa: BLE001
            log(f"native handle unavailable: {exc}")
        # taskbar / title icon, best effort through WinForms
        try:
            import clr  # noqa: F401
            from System.Drawing import Icon as SdIcon
            form = self.window.native
            form.Icon = SdIcon(os.path.abspath(ICON))
            form.ShowIcon = True
        except Exception as exc:  # noqa: BLE001
            log(f"window icon skipped: {exc}")

    def on_loaded(self):
        log("page loaded")
        try:
            self.window.evaluate_js(
                "document.documentElement.dataset.shell = 'desktop'; true")
        except Exception:
            pass
        if self.args.selftest:
            threading.Thread(target=self._selftest, daemon=True).start()

    def on_closing(self):
        if self.destroying:
            return True
        # Never call into JS from this handler: the WinForms UI thread is blocked
        # here, so evaluate_js would deadlock. The page pushes its dirty count
        # through the bridge instead (sync_state).
        dirty = int(self.page_state.get("dirty") or 0)
        if not dirty:
            return True
        log(f"close blocked: {dirty} unsaved tab(s)")
        threading.Thread(target=self._ask_close, daemon=True).start()
        return False

    def _ask_close(self):
        time.sleep(0.1)
        try:
            self.window.evaluate_js("window.__vantaAskClose && window.__vantaAskClose()")
        except Exception as exc:  # noqa: BLE001
            log(f"close prompt failed: {exc}")

    def on_closed(self):
        log("window closed")

    def _selftest(self):
        time.sleep(3.0)
        # the editor only mounts a view once a model is attached
        try:
            self.window.evaluate_js("window.__vantaOpenFile && window.__vantaOpenFile('ide/web/app.js')")
        except Exception as exc:  # noqa: BLE001
            log(f"selftest open failed: {exc}")
        time.sleep(1.8)
        checks = {
            "monaco": "!!document.querySelector('.monaco-editor')",
            "editors": "monaco.editor.getEditors().length",
            "domNode": "(function(){var e=monaco.editor.getEditors()[0];"
                       "if(!e||!e.getDomNode()) return 'none';var n=e.getDomNode();"
                       "return 'parent='+(n.parentElement?n.parentElement.id:'?')+' h='+n.offsetHeight"
                       "+' lines='+e.getModel().getLineCount();})()",
            "menubar": "document.querySelectorAll('#menubar button').length",
            "tree": "document.querySelectorAll('#tree .node').length",
            "terminal": "!!(window.__vantaState && window.__vantaState().term)",
            "bridge": "!!(window.pywebview && window.pywebview.api)",
            "errors": "JSON.stringify(window.__vantaErrors || [])",
            "state": "JSON.stringify(window.__vantaState ? window.__vantaState() : null)",
        }
        # close guard: dirty the active model, let the bridge sync, then ask
        try:
            self.window.evaluate_js(
                "(function(){var e=monaco.editor.getEditors()[0];"
                "if(e&&e.getModel()) e.getModel().setValue(e.getModel().getValue()+'\\n');})()")
        except Exception as exc:  # noqa: BLE001
            log(f"selftest dirty failed: {exc}")
        time.sleep(1.2)
        blocked = self.on_closing() is False
        out = {}
        for name, expr in checks.items():
            try:
                out[name] = self.window.evaluate_js(expr)
            except Exception as exc:  # noqa: BLE001
                out[name] = f"ERR {exc}"
        out["pageDirty"] = self.page_state.get("dirty")
        out["closeBlocked"] = blocked
        self.selftest = out
        log(f"selftest {json.dumps(out)}")
        print(json.dumps(out, indent=2))
        self.destroying = True
        try:
            self.window.destroy()
        except Exception:
            pass

    # ------------------------------- run --------------------------------- #

    def run(self) -> int:
        self.ensure_server()
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            log(f"pywebview unavailable ({exc}) — falling back")
            return self.fallback()

        api = Api(self)
        state_dir = get_storage_dir()
        self.window = webview.create_window(
            "Vanta IDE",
            url=self.url,
            js_api=api,
            width=self.args.width,
            height=self.args.height,
            min_size=MIN_SIZE,
            background_color="#0a0c0f",
            text_select=True,
            confirm_close=False,
            frameless=bool(self.args.frameless),
            easy_drag=False,
            shadow=True,
            hidden=bool(self.args.selftest),
        )
        self.window.events.shown += self.on_shown
        self.window.events.loaded += self.on_loaded
        self.window.events.closing += self.on_closing
        self.window.events.closed += self.on_closed
        try:
            webview.start(debug=bool(self.args.debug), storage_path=state_dir)
        except Exception as exc:  # noqa: BLE001
            # Deliberately no teardown here: the fallback window points at this
            # same server, so the process has to stay alive for it.
            log(f"webview crashed: {exc}\n{traceback.format_exc()}")
            return self.fallback()
        self.teardown()
        return 0

    def fallback(self) -> int:
        """Chromeless Edge app window; last resort, the default browser.

        The server stays up for as long as that window is open, then shuts down
        with it — same contract as the WebView2 shell.
        """
        if not self.url:
            return 1
        edge = find_edge()
        if edge and not self.args.web:
            try:
                profile = get_storage_dir("edge")
                proc = subprocess.Popen([
                    edge, f"--app={self.url}", f"--user-data-dir={profile}",
                    "--no-first-run", "--no-default-browser-check",
                    f"--window-size={self.args.width},{self.args.height}",
                ])
                log(f"edge app window -> {self.url} (pid {proc.pid})")
                self._wait_for_close(proc)
                self.teardown()
                return 0
            except Exception as exc:  # noqa: BLE001
                log(f"edge launch failed: {exc}")

        webbrowser.open(self.url)
        log(f"browser -> {self.url}")
        try:
            while True:
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        self.teardown()
        return 0

    @staticmethod
    def _wait_for_close(proc: subprocess.Popen):
        """Hold the process open until the window is gone (or ctrl+c)."""
        try:
            proc.wait()
        except KeyboardInterrupt:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        log("fallback window closed")

    def teardown(self):
        core.shutdown_children()
        if self.srv is not None and self.owns_server:
            try:
                self.srv.shutdown()
                self.srv.server_close()
            except Exception:
                pass
            log("server stopped")


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def main() -> int:
    if sys.stdout is None:  # pythonw
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(LOG, "a", encoding="utf-8")

    ap = argparse.ArgumentParser(description="Vanta IDE desktop shell")
    ap.add_argument("path", nargs="?", help="project folder to open")
    ap.add_argument("--root", default=None, help="project folder to open")
    ap.add_argument("--port", type=int, default=core.DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--width", type=int, default=DEFAULT_SIZE[0])
    ap.add_argument("--height", type=int, default=DEFAULT_SIZE[1])
    ap.add_argument("--frameless", action="store_true", help="no native frame (no OS resize edges)")
    ap.add_argument("--web", action="store_true", help="open in the default browser instead")
    ap.add_argument("--debug", action="store_true", help="webview devtools")
    ap.add_argument("--selftest", action="store_true", help="hidden window + DOM checks, then exit")
    args = ap.parse_args()

    root = args.root or args.path
    if root:
        remember_root(root)
        args.root = os.path.abspath(root) if os.path.isdir(root) else None

    shell = Shell(args)
    if args.web:
        shell.ensure_server()
        webbrowser.open(shell.url)
        print(f"Vanta IDE  →  {shell.url}   (ctrl+c to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            shell.teardown()
        return 0

    try:
        code = shell.run()
    except Exception as exc:  # noqa: BLE001
        log(f"fatal: {exc}\n{traceback.format_exc()}")
        print(f"Vanta IDE failed to start: {exc}", file=sys.stderr)
        return 1

    if args.selftest:
        print(json.dumps(shell.selftest or {}, indent=2))
        ok = bool(shell.selftest) and shell.selftest.get("monaco") is True
        return 0 if ok else 2
    return code


if __name__ == "__main__":
    sys.exit(main())
