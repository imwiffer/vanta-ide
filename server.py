"""
Vanta IDE — lightweight local IDE server.

Python 3.10+, stdlib only. Serves the Monaco frontend and a small JSON API for
filesystem, search, git, a persistent PowerShell session, and an optional
AirLLM chat worker.

    python ide/server.py                 # http://127.0.0.1:7777
    python ide/server.py --port 9000 --root C:\\work

Binds to 127.0.0.1 by default. It can read/write any file the current user can
reach, and executes shell commands — treat the port as you would a terminal.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

try:  # optional, for fast OS-level file watching
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAVE_WATCHDOG = True
except Exception:  # noqa: BLE001
    HAVE_WATCHDOG = False

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE_FILE = os.path.join(HERE, ".state.json")
AI_CFG_FILE = os.path.join(HERE, ".ai.json")
DEFAULT_PORT = 7777

sys.path.insert(0, os.path.join(HERE, "tools"))
import agent_tools  # noqa: E402
import debug_bridge  # noqa: E402
import plugin_api  # noqa: E402
import interop  # noqa: E402
import remotes  # noqa: E402
import updater  # noqa: E402

# Started from pythonw.exe there is no console, so every child process that is
# not explicitly windowless pops a console for a frame — that is the random
# flashing. Nothing in this file spawns a process without going through here.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0


def hidden_kwargs(extra_flags: int = 0) -> dict:
    """Popen/run kwargs that keep a child's console off screen."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                                  # SW_HIDE
    return {"creationflags": NO_WINDOW | extra_flags, "startupinfo": si}

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".next", ".nuxt", "dist", "build", "out", ".idea", ".vs",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".gradle",
    "target", "bin", "obj", ".tox", "site-packages", ".turbo",
}

# Optional shared-secret auth. When set, every /api call except /api/ping must
# carry it (header X-Vanta-Token or ?token=). Desktop injects it into the page;
# browser mode can append ?token=... to the URL.
AUTH_TOKEN = ""
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RESULTS = 400
MAX_LISTED_FILES = 40000

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".xml", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue",
    ".svelte", ".py", ".pyi", ".pyw", ".rb", ".php", ".go", ".rs", ".java",
    ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
    ".hh", ".cs", ".m", ".mm", ".lua", ".pl", ".r", ".jl", ".sh", ".bash",
    ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".graphql",
    ".gql", ".proto", ".dockerfile", ".gitignore", ".gitattributes",
    ".editorconfig", ".lock", ".srt", ".vtt", ".tex", ".asm", ".s", ".wat",
    ".zig", ".nim", ".dart", ".ex", ".exs", ".erl", ".hs", ".clj", ".scala",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def tailscale_ip() -> str | None:
    try:
        import socket
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            parts = ip.split(".")
            if len(parts) == 4 and parts[0] == "100" and 64 <= int(parts[1]) <= 127:
                return ip
    except Exception:
        pass
    return None


def tailscale_peers() -> list[dict]:
    """Return Tailscale peer nodes (e.g. VPS instances on the tailnet)."""
    peers: list[dict] = []
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=4, **hidden_kwargs()
        )
        if out.returncode == 0 and out.stdout:
            data = json.loads(out.stdout)
            for _, node in (data.get("Peer") or {}).items():
                ips = [ip for ip in (node.get("TailscaleIPs") or []) if "." in ip]
                if ips:
                    peers.append({
                        "host": node.get("HostName") or node.get("DNSName") or ips[0],
                        "ip": ips[0],
                        "online": bool(node.get("Online")),
                        "os": node.get("OS") or "",
                        "ollamaUrl": f"http://{ips[0]}:11434/v1",
                    })
    except Exception:
        pass
    peers.sort(key=lambda p: (not p["online"], p["host"].lower()))
    return peers


def json_response(handler: BaseHTTPRequestHandler, obj, status: int = 200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8", "replace")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def error(handler, msg: str, status: int = 400):
    json_response(handler, {"ok": False, "error": msg}, status)


def git_out(root: str, args: list[str]) -> tuple[int, str, str]:
    """Run git and return (returncode, stdout, stderr) so callers can show errors."""
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=120, **hidden_kwargs())
        return out.returncode, out.stdout, out.stderr
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def safe_join(root: str, rel: str) -> str:
    """Resolve rel inside root, refusing traversal."""
    root_abs = os.path.abspath(root)
    rel = (rel or "").replace("/", os.sep)
    candidate = os.path.abspath(os.path.join(root_abs, rel.lstrip("\\/")))
    a, b = os.path.normcase(root_abs), os.path.normcase(candidate)
    try:
        inside = os.path.commonpath([a, b]) == a
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("path escapes project root")
    return candidate


def is_texty(path: str, sample: bytes | None = None) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return True
    name = os.path.basename(path).lower()
    if name in {"dockerfile", "makefile", "cmakelists.txt", "license", "procfile"}:
        return True
    if sample is not None:
        return b"\x00" not in sample
    return ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
                       ".pdf", ".zip", ".gz", ".7z", ".rar", ".exe", ".dll",
                       ".so", ".dylib", ".bin", ".pyc", ".class", ".jar",
                       ".mp3", ".mp4", ".wav", ".mov", ".avi", ".woff", ".woff2",
                       ".ttf", ".otf", ".db", ".sqlite", ".onnx", ".pt", ".safetensors"}


def read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"recent": [], "lastRoot": None}


def write_state(state: dict):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def walk_files(root: str, limit: int = MAX_LISTED_FILES) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in IGNORED_DIRS
                             and not d.startswith("."))
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(rel.replace(os.sep, "/"))
            if len(out) >= limit:
                return out
    return out


def git(root: str, args: list[str]) -> str | None:
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=60, **hidden_kwargs())
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


# --------------------------------------------------------------------------- #
# terminal sessions
# --------------------------------------------------------------------------- #

class Terminal:
    """One persistent PowerShell process driven by a sentinel-delimited protocol."""

    SHELL = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    MARK = "__VANTA_DONE__"

    def __init__(self, cwd: str):
        self.id = uuid.uuid4().hex[:12]
        self.cwd = cwd
        self.buf: deque[str] = deque(maxlen=20000)
        self.lock = threading.Lock()
        self.cursor = 0
        self.alive = False
        self.busy = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.proc: subprocess.Popen | None = None
        self._start()

    def _start(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.proc = subprocess.Popen(
            [self.SHELL, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self.cwd, bufsize=0, **hidden_kwargs(NEW_GROUP),
            env={**os.environ, "TERM": "dumb", "NO_COLOR": "1",
                 "VIRTUAL_ENV_PROMPT": ""},
        )
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        adm = " [Administrator]" if is_admin() else ""
        self._emit(f"* shell ready{adm} — {self.cwd}\r\n")

    def _emit(self, text: str):
        with self.lock:
            self.buf.append(text)
            self.cursor += len(text)

    def _reader(self):
        assert self.proc and self.proc.stdout
        pending = ""
        while True:
            try:
                chunk = self.proc.stdout.read(8192)
            except Exception:
                break
            if not chunk:
                break
            pending += ANSI_RE.sub("", self.decoder.decode(chunk))
            visible: list[str] = []
            while True:
                i = pending.find(self.MARK)
                if i < 0:
                    hold = 0
                    for k in range(1, len(self.MARK)):
                        if pending.endswith(self.MARK[:k]):
                            hold = k
                    if hold:
                        visible.append(pending[:-hold])
                        pending = pending[-hold:]
                    else:
                        visible.append(pending)
                        pending = ""
                    break
                visible.append(pending[:i])
                nl = pending.find("\n", i)
                if nl < 0:
                    pending = pending[i:]
                    break
                cwd = pending[i + len(self.MARK):nl].strip()
                if cwd:
                    self.cwd = cwd
                self.busy = False
                pending = pending[nl + 1:]
            if visible:
                self._emit("".join(visible))
        if pending:
            self._emit(pending)
        self.alive = False
        self.busy = False
        try:
            code = self.proc.wait(timeout=2)
        except Exception:
            code = -1
        self._emit(f"\r\n* shell exited ({code})\r\n")

    def send(self, command: str):
        if not self.alive or not self.proc or not self.proc.stdin:
            raise RuntimeError("shell is not running")
        self.busy = True
        cmd = command.rstrip("\r\n")
        wrapper = (f"{cmd}\r\n"
                   f"Write-Host ('{self.MARK}' + (Get-Location).Path)\r\n")
        try:
            self.proc.stdin.write(wrapper.encode("utf-8", "replace"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.alive = False
            self.busy = False
            raise RuntimeError(f"shell pipe broken: {exc}")

    def interrupt(self):
        """Kill the shell and start a fresh one in the last known directory."""
        self.kill()
        time.sleep(0.15)
        self._start()

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10, **hidden_kwargs())
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.alive = False
        self.busy = False

    def poll(self, cursor: int):
        with self.lock:
            parts = list(self.buf)
            total = sum(len(p) for p in parts)
            if cursor >= total:
                return "", total
            if cursor < 0:
                cursor = 0
            # rebuild from the tail until we cover the requested window
            acc, seen = [], 0
            for chunk in reversed(parts):
                acc.append(chunk)
                seen += len(chunk)
                if seen >= total - cursor:
                    break
            text = "".join(reversed(acc))
            return text[len(text) - (total - cursor):], total


TERMS: dict[str, Terminal] = {}
TERMS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# workspace watcher (OS-level, optional; frontend falls back to polling)
# --------------------------------------------------------------------------- #

class _WatchHandler(FileSystemEventHandler):
    def __init__(self, watcher: "WorkspaceWatcher"):
        self.watcher = watcher

    def _mark(self, path):
        if isinstance(path, bytes):
            path = path.decode("utf-8", "replace")
        self.watcher.mark(path)

    def on_created(self, event):
        if not event.is_directory:
            self._mark(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._mark(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._mark(event.dest_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._mark(event.src_path)


class WorkspaceWatcher:
    """Tracks changed files under a root using watchdog when available."""

    def __init__(self):
        self.root: str | None = None
        self.observer = None
        self.changed: dict[str, float] = {}
        self.lock = threading.Lock()
        self.maxlen = 5000

    def start(self, root: str):
        root = os.path.abspath(root)
        if self.root == root and self.observer is not None:
            return
        self.stop()
        self.root = root
        self.changed = {}
        if HAVE_WATCHDOG:
            try:
                self.observer = Observer()
                self.observer.schedule(_WatchHandler(self), root, recursive=True)
                self.observer.daemon = True
                self.observer.start()
            except Exception:  # noqa: BLE001
                self.observer = None

    def stop(self):
        if self.observer is not None:
            try:
                self.observer.stop()
                self.observer.join(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        self.observer = None
        self.root = None

    def mark(self, path: str):
        if not self.root:
            return
        try:
            rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        except ValueError:
            return
        top = rel.split("/")[0]
        if top.lower() in IGNORED_DIRS or top.startswith("."):
            return
        with self.lock:
            self.changed[rel] = time.time()
            if len(self.changed) > self.maxlen:
                keep = sorted(self.changed.items(), key=lambda kv: kv[1], reverse=True)
                self.changed = dict(keep[: self.maxlen // 2])

    def poll(self, since: float) -> list[tuple[str, float]]:
        with self.lock:
            items = [(rel, ts) for rel, ts in self.changed.items() if ts > since]
        return sorted(items, key=lambda kv: kv[1])


WATCHER = WorkspaceWatcher()


# --------------------------------------------------------------------------- #
# airllm worker
# --------------------------------------------------------------------------- #

class AIWorker:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.events: deque[dict] = deque(maxlen=4000)
        self.seq = 0
        self.lock = threading.Lock()
        self.state = "idle"
        self.model = None

    def _emit(self, obj: dict):
        with self.lock:
            self.seq += 1
            obj["seq"] = self.seq
            self.events.append(obj)

    def python(self) -> str:
        candidates = [
            os.path.join(HERE, ".venv", "Scripts", "python.exe"),
            os.path.join(HERE, ".venv", "bin", "python"),
            os.path.join(os.path.dirname(HERE), "airllm", ".venv", "Scripts", "python.exe"),
            os.path.join(os.path.dirname(HERE), ".venv", "Scripts", "python.exe"),
            os.path.join(os.path.dirname(HERE), ".venv", "bin", "python"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return sys.executable

    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        script = os.path.join(HERE, "tools", "ai_worker.py")
        if not os.path.exists(script):
            raise RuntimeError("tools/ai_worker.py missing")
        self.proc = subprocess.Popen(
            [self.python(), "-u", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, encoding="utf-8", errors="replace", **hidden_kwargs(),
            env={**os.environ, "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false",
                 "PYTHONIOENCODING": "utf-8",
                 "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                 "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
        )
        self.model = None        # a fresh worker has nothing loaded
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = {"type": "log", "msg": line}
                if obj.get("type") == "state":
                    self.state = obj.get("msg", "idle")
                if obj.get("type") == "model":
                    self.model = obj.get("msg")
                self._emit(obj)
        except Exception:
            pass
        self.state = "idle"
        self.model = None        # worker exited: nothing is resident any more
        self._emit({"type": "state", "msg": "idle"})

    def send(self, obj: dict):
        self.ensure()
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.proc = None
            self.ensure()
            assert self.proc and self.proc.stdin
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def since(self, seq: int):
        with self.lock:
            return [e for e in self.events if e["seq"] > seq], self.seq


AI = AIWorker()
CANCEL = threading.Event()          # a running chat/generation should stop

CONFIRMS: dict[str, dict] = {}
CONFIRMS_LOCK = threading.Lock()

DANGEROUS_RE = re.compile(
    r"\b(format|diskpart|shutdown|reg\s+add|regedit|Set-ExecutionPolicy|"
    r"net\s+user|netsh|taskkill|rm\s+-rf|Remove-Item[^|]*-Recurse|"
    r"del\s+/[sq]|rmdir\s+/s|mkfs|dd\s+if=)\b|"
    r"(curl|iwr|Invoke-WebRequest|wget)[^\n|]*\|\s*(iex|Invoke-Expression|iwr)",
    re.IGNORECASE)


# --------------------------------------------------------------------------- #
# MCP (Model Context Protocol) stdio client
# --------------------------------------------------------------------------- #

class MCPSession:
    """One stdio MCP server: JSON-RPC 2.0 over stdin/stdout, line-delimited."""

    def __init__(self, name: str, command: list[str], env: dict | None = None,
                 cwd: str = "", timeout: float = 30):
        self.name = name
        self.command = command
        self.env = env or {}
        self.cwd = cwd
        self.timeout = timeout
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.ready = False
        self.error = ""
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._cond = threading.Condition()
        self._lock = threading.Lock()

    # ------------------------------ transport ------------------------------ #

    def _reader(self):
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg.get("id"), int):
                    with self._cond:
                        self._responses[msg["id"]] = msg
                        self._cond.notify_all()
        except Exception:  # noqa: BLE001
            pass
        self.ready = False

    def _rpc(self, method: str, params: dict | None = None,
             notify: bool = False) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = rid
        body = json.dumps(msg) + "\n"
        self.proc.stdin.write(body.encode("utf-8"))
        self.proc.stdin.flush()
        if notify:
            return {}
        deadline = time.time() + self.timeout
        with self._cond:
            while rid not in self._responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"mcp {self.name}: {method} timed out")
                self._cond.wait(remaining)
            resp = self._responses.pop(rid)
        if "error" in resp:
            raise RuntimeError(f"mcp {self.name}: {resp['error']}")
        return resp.get("result") or {}

    # ------------------------------- lifecycle ----------------------------- #

    def start(self) -> bool:
        if self.proc and self.proc.poll() is None and self.ready:
            return True
        self.stop()
        try:
            env = {**os.environ, **self.env}
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, cwd=self.cwd or None,
                env=env, **hidden_kwargs())
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self._rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "vanta-ide", "version": "1.0"},
            })
            self._rpc("notifications/initialized", notify=True)
            result = self._rpc("tools/list", {})
            self.tools = [
                {"name": t.get("name") or "",
                 "description": t.get("description") or "",
                 "schema": t.get("inputSchema") or {}}
                for t in (result.get("tools") or []) if t.get("name")
            ]
            self.ready = True
            self.error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.stop()
            return False

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        self.ready = False
        self.tools = []

    def call(self, tool: str, args: dict) -> str:
        if not self.ready and not self.start():
            return f"Error: MCP server {self.name} not available ({self.error})"
        try:
            result = self._rpc("tools/call", {"name": tool, "arguments": args})
        except Exception as exc:  # noqa: BLE001
            return f"Error: mcp {self.name}.{tool}: {exc}"
        parts: list[str] = []
        for item in result.get("content") or []:
            if item.get("type") == "text":
                parts.append(item.get("text") or "")
            else:
                parts.append(json.dumps(item))
        text = "\n".join(p for p in parts if p)
        if result.get("isError"):
            return f"Error: {text or 'tool failed'}"
        if len(text) > agent_tools.MAX_OUTPUT_CHARS:
            text = text[:agent_tools.MAX_OUTPUT_CHARS] + "\n... (truncated)"
        return text or "(no output)"


class MCPManager:
    def __init__(self):
        self.sessions: dict[str, MCPSession] = {}
        self.lock = threading.Lock()

    def config_path(self, root: str) -> str:
        for base in (root, HERE):
            if base and os.path.isdir(base):
                p = os.path.join(base, ".mcp.json")
                if os.path.exists(p):
                    return p
        return os.path.join(root or HERE, ".mcp.json")

    def load_config(self, root: str) -> dict:
        try:
            with open(self.config_path(root), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("mcpServers") or data.get("servers") or {}
        except Exception:  # noqa: BLE001
            return {}

    def get(self, name: str, root: str) -> MCPSession | None:
        with self.lock:
            sess = self.sessions.get(name)
            if sess and sess.ready:
                return sess
        cfg = self.load_config(root).get(name)
        if not cfg:
            return None
        command = cfg.get("command")
        if not command:
            return None
        if isinstance(command, str):
            command = [command, *cfg.get("args", [])]
        sess = MCPSession(name, command, cfg.get("env"), cfg.get("cwd") or root)
        with self.lock:
            self.sessions[name] = sess
        sess.start()
        return sess if sess.ready else None

    def status(self, root: str) -> list[dict]:
        cfg = self.load_config(root)
        out = []
        for name, c in cfg.items():
            sess = self.sessions.get(name)
            ready = bool(sess and sess.ready)
            tools = sess.tools if ready else []
            out.append({"name": name, "command": c.get("command"), "ready": ready,
                        "tools": tools, "error": sess.error if sess else ""})
        return out

    def disconnect(self, name: str):
        with self.lock:
            sess = self.sessions.pop(name, None)
        if sess:
            sess.stop()


MCP = MCPManager()


# --------------------------------------------------------------------------- #
# LSP bridge (generic JSON-RPC stdio language servers)
# --------------------------------------------------------------------------- #

class LSPSession:
    """One language server over stdio with Content-Length framing."""

    def __init__(self, lang: str, command: list[str], root: str):
        self.lang = lang
        self.command = command
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self.error = ""
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._diagnostics: dict[str, list] = {}
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._open: set[str] = set()

    def _reader(self):
        assert self.proc and self.proc.stdout
        f = self.proc.stdout
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = f.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    if b":" in line:
                        k, v = line.split(b":", 1)
                        headers[k.decode().strip().lower()] = v.decode().strip()
                length = int(headers.get("content-length") or 0)
                if not length:
                    continue
                body = f.read(length)
                msg = json.loads(body.decode("utf-8", "replace"))
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._cond:
                        self._responses[msg["id"]] = msg
                        self._cond.notify_all()
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    uri = (msg.get("params") or {}).get("uri") or ""
                    self._diagnostics[uri] = (msg.get("params") or {}).get(
                        "diagnostics") or []
        except Exception:  # noqa: BLE001
            pass
        self.ready = False

    def _send(self, method: str, params: dict | None = None,
              notify: bool = False) -> dict:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("lsp not running")
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = rid
        body = json.dumps(msg).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.proc.stdin.flush()
        if notify:
            return {}
        deadline = time.time() + 20
        with self._cond:
            while rid not in self._responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"lsp {self.lang}: {method} timed out")
                self._cond.wait(remaining)
            resp = self._responses.pop(rid)
        if "error" in resp:
            raise RuntimeError(f"lsp {self.lang}: {resp['error']}")
        return resp.get("result") or {}

    def start(self) -> bool:
        if self.proc and self.proc.poll() is None and self.ready:
            return True
        self.stop()
        try:
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0,
                                         cwd=self.root or None, **hidden_kwargs())
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self._send("initialize", {
                "processId": os.getpid(),
                "rootUri": pathlib_path_uri(self.root),
                "capabilities": {
                    "textDocument": {
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "completion": {"completionItem": {
                            "documentationFormat": ["markdown", "plaintext"]}},
                        "publishDiagnostics": {},
                    },
                },
            })
            self._send("initialized", {}, notify=True)
            self.ready = True
            self.error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.stop()
            return False

    def stop(self):
        if self.proc:
            try:
                self._send("shutdown", notify=False) if self.ready else None
                self._send("exit", notify=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        self.ready = False
        self._open.clear()

    def open(self, path: str, text: str):
        uri = pathlib_path_uri(path)
        if uri in self._open:
            self.change(path, text, 999999)
            return
        self._send("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": self.lang,
                             "version": 1, "text": text}}, notify=True)
        self._open.add(uri)

    def change(self, path: str, text: str, version: int):
        uri = pathlib_path_uri(path)
        if uri not in self._open:
            self.open(path, text)
            return
        self._send("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}]}, notify=True)

    def diagnostics(self, path: str) -> list:
        return self._diagnostics.get(pathlib_path_uri(path)) or []

    def _position(self, line: int, col: int) -> dict:
        return {"line": max(0, line - 1), "character": max(0, col - 1)}

    def hover(self, path: str, line: int, col: int):
        return self._send("textDocument/hover", {
            "textDocument": {"uri": pathlib_path_uri(path)},
            "position": self._position(line, col)})

    def definition(self, path: str, line: int, col: int):
        return self._send("textDocument/definition", {
            "textDocument": {"uri": pathlib_path_uri(path)},
            "position": self._position(line, col)})

    def completion(self, path: str, line: int, col: int):
        return self._send("textDocument/completion", {
            "textDocument": {"uri": pathlib_path_uri(path)},
            "position": self._position(line, col)})


def pathlib_path_uri(path: str) -> str:
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p


class LSPManager:
    def __init__(self):
        self.sessions: dict[str, LSPSession] = {}
        self.lock = threading.Lock()

    def config_path(self, root: str) -> str:
        for base in (root, HERE):
            if base and os.path.isdir(base):
                p = os.path.join(base, ".vanta", "lsp.json")
                if os.path.exists(p):
                    return p
        return os.path.join(root or HERE, ".vanta", "lsp.json")

    def load_config(self, root: str) -> dict:
        try:
            with open(self.config_path(root), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def lang_for(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        for lang, exts in LANG_LSP_MAP.items():
            if ext in exts:
                return lang
        return ""

    def session_for(self, root: str, path: str) -> LSPSession | None:
        lang = self.lang_for(path)
        if not lang:
            return None
        cfg = self.load_config(root).get(lang)
        if not cfg or not cfg.get("command"):
            return None
        command = cfg["command"]
        if isinstance(command, str):
            command = [command, *cfg.get("args", [])]
        with self.lock:
            sess = self.sessions.get(lang)
            if not sess or sess.command != command or sess.root != root:
                if sess:
                    sess.stop()
                sess = LSPSession(lang, command, root)
                self.sessions[lang] = sess
            if not sess.ready and not sess.start():
                return None
            return sess

    def status(self, root: str) -> list[dict]:
        out = []
        for lang, cfg in self.load_config(root).items():
            sess = self.sessions.get(lang)
            out.append({"lang": lang, "command": cfg.get("command"),
                        "ready": bool(sess and sess.ready),
                        "error": sess.error if sess else ""})
        return out


LANG_LSP_MAP = {
    "python": {".py", ".pyi", ".pyw"},
    "typescript": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"},
    "go": {".go"},
    "rust": {".rs"},
    "json": {".json", ".jsonc"},
    "css": {".css", ".scss", ".less"},
    "html": {".html", ".htm"},
    "yaml": {".yaml", ".yml"},
    "csharp": {".cs"},
    "cpp": {".c", ".h", ".cc", ".cpp", ".hpp"},
}


LSP = LSPManager()
DEBUGGER = debug_bridge.DEBUGGER
PLUGINS = plugin_api.PluginManager(HERE, log_fn=lambda m: log_line(m))


def log_line(msg: str):
    """Plugins log to stdout/desktop log; no console flash under pythonw."""
    try:
        print(time.strftime("%H:%M:%S ") + msg, flush=True)
    except Exception:  # noqa: BLE001
        pass


def run_agent_tool(call: dict, ws: str) -> str:
    """Execute one tool call, routing MCP tools to their session."""
    name = (call.get("name") or "").strip()
    args = call.get("args") or {}
    if name.startswith("mcp:"):
        try:
            _, server, tool = name.split(":", 2)
        except ValueError:
            return f"Error: bad MCP tool name {name}"
        sess = MCP.get(server, ws)
        if not sess:
            return f"Error: MCP server {server} not connected"
        return sess.call(tool, args)
    if name in ("run_command", "run", "shell", "powershell", "cmd"):
        cmd = str(args.get("command") or "")
        if _sandbox_enabled() and DANGEROUS_RE.search(cmd):
            if not _request_confirm(cmd):
                AI._emit({"type": "note", "msg": f"blocked · {cmd[:90]}"})
                return "Error: command blocked — the user denied it or did not respond."
    PLUGINS.emit("agent_tool", {"name": name, "args": {k: (str(v)[:120])
                for k, v in (args or {}).items()}})
    return agent_tools.execute_tool(call, ws)


def _sandbox_enabled() -> bool:
    try:
        return bool(read_chat_cfg().get("agentSandbox", True))
    except Exception:  # noqa: BLE001
        return True


def _request_confirm(command: str, timeout: float = 180) -> bool:
    """Ask the operator to approve a risky agent command. Blocks until they
    respond via /api/confirm or the timeout elapses."""
    cid = uuid.uuid4().hex[:10]
    event = threading.Event()
    holder = {"event": event, "denied": False}
    with CONFIRMS_LOCK:
        CONFIRMS[cid] = holder
    AI._emit({"type": "confirm", "id": cid, "command": command[:300]})
    event.wait(timeout)
    with CONFIRMS_LOCK:
        CONFIRMS.pop(cid, None)
    return event.is_set() and not holder["denied"]


def tool_schemas(agent_enabled: bool, ws: str) -> list[dict]:
    """JSON schemas for native OpenAI-style tool calling."""
    if not agent_enabled:
        return []
    schemas = [
        {"type": "function", "function": {
            "name": "run_command",
            "description": "Run a PowerShell command inside the workspace root.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string"}}, "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a workspace file (optionally lines like 12-40).",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "lines": {"type": "string"}},
                "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "write_file",
            "description": "Overwrite a workspace file with content.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"]}}},
        {"type": "function", "function": {
            "name": "edit_file",
            "description": "Replace an exact existing block with new text.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "search": {"type": "string"},
                "replace": {"type": "string"}},
                "required": ["path", "search", "replace"]}}},
        {"type": "function", "function": {
            "name": "list_dir",
            "description": "List a directory inside the workspace.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}}, "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "search_files",
            "description": "Regex search across workspace files.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"}, "include": {"type": "string"}},
                "required": ["query"]}}},
    ]
    for srv in MCP.status(ws):
        if not srv["ready"]:
            continue
        for t in srv["tools"]:
            schemas.append({"type": "function", "function": {
                "name": f"mcp:{srv['name']}:{t['name']}",
                "description": t.get("description") or t["name"],
                "parameters": t.get("schema") or {"type": "object", "properties": {}}}})
    return schemas


def agent_mcp_prompt(ws: str) -> str:
    ready = [s for s in MCP.status(ws) if s["ready"] and s["tools"]]
    if not ready:
        return ""
    lines = ["\n\n## MCP tools"]
    for srv in ready:
        for t in srv["tools"]:
            lines.append(f"- `mcp:{srv['name']}:{t['name']}` — "
                         f"{t.get('description') or t['name']}")
    lines.append("Call these like the other tools; the harness executes them and "
                 "returns `[Observation]`.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# chat settings + providers (local AirLLM worker | OpenAI-compatible API)
# --------------------------------------------------------------------------- #

CHAT_DEFAULTS = {
    "provider": "api",       # api | anthropic | opencode | local
    "profile": "",           # selected apiProfile name ("" = flat api fields)
    "apiProfiles": [],       # [{name, baseUrl, apiKey, model}]
    "customInstructions": "",  # appended to every system prompt
    "contextBudget": 32000,  # workspace context character budget
    "failsafe": True,        # auto-fallback to local AirLLM if remote fails
    "mode": "agent",         # agent | chat | code | explain | edit | review | plan
    "contextFile": True,
    "agentTools": True,
    "agentSandbox": True,                    # confirm risky agent shell commands
    "temperature": 0.7,
    "maxTokens": 512,
    "local": {"model": "criscar22/Qwen2.5-Coder-7B-Instruct-abliterated",
              "compression": ""},
    "api": {"baseUrl": "http://100.120.101.8:11434/v1", "apiKey": "", "model": "qwen2.5:0.5b"},
    "anthropic": {"apiKey": "", "model": "claude-sonnet-4-5"},
    "opencode": {"model": "", "agent": False},
}

# Appended to every mode prompt. The model is local and the person at the
# keyboard asked on purpose — encouragement to hedge is the failure mode.
HOUSE_RULES = (
    "\n\nYou are running locally for one person who owns this machine. Talk like a "
    "competent colleague, not a support desk: plain words, no filler, no restating "
    "the question. Never open with 'Sure', 'Certainly', 'Of course', 'Great "
    "question', or an apology. Never close by offering further help or asking if "
    "there is anything else. No disclaimers, no content warnings, no moral asides, "
    "no 'consult a professional', no 'as an AI', no refusal templates. If a request "
    "is blunt, technical, or dark, answer it straight — you are writing text, and "
    "the person reading it is an adult who asked for it."
)

MODE_PROMPTS = {
    "agent": "You are Vanta — an autonomous local agent running directly on this machine "
             "with full filesystem and PowerShell execution access. When asked to build, "
             "fix, inspect, or run something, use your `ACTION:` tools (`run_command`, "
             "`read_file`, `write_file`, `edit_file`, `list_dir`, `search_files`) to "
             "do the work directly in the workspace rather than just talking about it. "
             "Verify your changes and report the result concisely.",
    "chat": "You are Vanta — a local model running on this machine, working with one "
            "person at this keyboard. You are not a product, not a support desk, and "
            "you never describe yourself as an 'AI assistant'.\n"
            "Voice: first person, plain words, dry, no hype. Answer exactly what was "
            "asked at the length it deserves. Talk back when the ask is talk; write "
            "complete runnable code in one fenced block when the ask is code. No lesson "
            "nobody asked for, no restating the question, no bullets unless the answer "
            "is truly a list. Never end with an offer to help or a question about what "
            "to do next.\n"
            "Asked what you are: one line — a local model running on this box — then "
            "carry on.\n"
            "Example of the voice:\n"
            "user: yo\n"
            "assistant: yo. what are we breaking today?\n"
            "user: this leaks the file handle\n"
            "assistant: <one-line diagnosis, then the fixed function in one fenced "
            "block>\n"
            "user: what are you\n"
            "assistant: local model on your GPU, nothing leaves this machine.",
    "code": "Build what was asked. Return complete, runnable code in one fenced block "
            "with the language named — real APIs, error handling, no stubs, no TODOs, "
            "no 'your code here'. Then at most two lines saying what it does and what "
            "the caller must supply. If the ask is ambiguous, pick the strongest "
            "reading, build that, and note the assumption in one line.",
    "explain": "Explain the referenced code. Order: what it does, how it does it, "
               "what breaks it. No preamble, no code unless it clarifies a line.",
    "edit": "Return only the replacement code for the referenced selection — no "
            "prose, no fences unless the language needs them. Match the surrounding "
            "style exactly, including naming and indentation.",
    "review": "Review the referenced code. Bullet list only: bugs first, then risks, "
              "then concrete improvements. Name the symbol or line each point is about.",
    "plan": "Turn the request into an ordered implementation plan. One step per "
            "line, each naming the file it touches. Only include code where a step "
            "would otherwise be ambiguous.",
}

# Popular AI services with OpenAI-compatible endpoints. Key is read from the
# named environment variable at request time when the profile has no key.
AI_SERVICES = [
    {"name": "OpenAI", "baseUrl": "https://api.openai.com/v1", "envKey": "OPENAI_API_KEY",
     "models": ["gpt-4o", "gpt-4o-mini"]},
    {"name": "Groq", "baseUrl": "https://api.groq.com/openai/v1", "envKey": "GROQ_API_KEY",
     "models": ["llama-3.3-70b-versatile", "qwen-2.5-32b"]},
    {"name": "Mistral", "baseUrl": "https://api.mistral.ai/v1", "envKey": "MISTRAL_API_KEY",
     "models": ["mistral-large-latest", "codestral-latest"]},
    {"name": "DeepSeek", "baseUrl": "https://api.deepseek.com/v1", "envKey": "DEEPSEEK_API_KEY",
     "models": ["deepseek-chat", "deepseek-reasoner"]},
    {"name": "Together", "baseUrl": "https://api.together.xyz/v1", "envKey": "TOGETHER_API_KEY",
     "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"]},
    {"name": "OpenRouter", "baseUrl": "https://openrouter.ai/api/v1", "envKey": "OPENROUTER_API_KEY",
     "models": ["anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"]},
    {"name": "xAI Grok", "baseUrl": "https://api.x.ai/v1", "envKey": "XAI_API_KEY",
     "models": ["grok-3", "grok-3-mini"]},
    {"name": "Gemini (compat)", "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
     "envKey": "GEMINI_API_KEY", "models": ["gemini-2.5-pro", "gemini-2.5-flash"]},
    {"name": "LM Studio (local)", "baseUrl": "http://127.0.0.1:1234/v1", "envKey": "", "models": []},
    {"name": "llama.cpp server (local)", "baseUrl": "http://127.0.0.1:8080/v1", "envKey": "", "models": []},
    {"name": "vLLM (local)", "baseUrl": "http://127.0.0.1:8000/v1", "envKey": "", "models": []},
    {"name": "Ollama (local)", "baseUrl": "http://127.0.0.1:11434/v1", "envKey": "", "models": []},
]

# Local checkpoints for a 12 GB GPU (RTX 5070 class): fp16 fits weights <= ~7 GB,
# 4-bit nf4 (needs bitsandbytes) fits <= ~10 GB, else AirLLM streams from disk.
PRESET_META = {
    "Qwen/Qwen3-0.6B": {"paramsB": 0.6, "note": "tiny · fp16"},
    "Qwen/Qwen3-1.7B": {"paramsB": 1.7, "note": "fp16 · very fast"},
    "Qwen/Qwen3-4B": {"paramsB": 4.0, "note": "fp16 fits 12GB · fast"},
    "Qwen/Qwen3-8B": {"paramsB": 8.0, "note": "4-bit fits 12GB"},
    "huihui-ai/Qwen3-4B-abliterated": {"paramsB": 4.0, "note": "fp16 fits 12GB · liberated"},
    "huihui-ai/Qwen3-8B-abliterated": {"paramsB": 8.0, "note": "4-bit fits 12GB · liberated"},
    "criscar22/Qwen2.5-Coder-7B-Instruct-abliterated": {"paramsB": 7.6, "note": "4-bit fits 12GB · coder"},
    "huihui-ai/Llama-3.2-3B-Instruct-abliterated": {"paramsB": 3.6, "note": "fp16 fits 12GB · liberated"},
    "mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated": {"paramsB": 8.0, "note": "4-bit fits 12GB · liberated"},
    "Goekdeniz-Guelmez/Josiefied-Qwen2.5-Coder-14B-Instruct-abliterated-v1": {"paramsB": 14.0, "note": "4-bit ~8GB fits 12GB · coder"},
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B": {"paramsB": 7.6, "note": "4-bit fits 12GB · reasoning"},
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B": {"paramsB": 14.0, "note": "4-bit ~8GB fits 12GB · reasoning"},
    "microsoft/phi-4": {"paramsB": 14.0, "note": "4-bit ~8GB fits 12GB"},
    "meta-llama/Llama-3.1-8B-Instruct": {"paramsB": 8.0, "note": "4-bit fits 12GB"},
    "google/gemma-3-4b-it": {"paramsB": 4.0, "note": "fp16 fits 12GB · multimodal"},
    "microsoft/Phi-3.5-mini-instruct": {"paramsB": 3.8, "note": "fp16 fits 12GB"},
    "mistralai/Mistral-7B-Instruct-v0.3": {"paramsB": 7.2, "note": "4-bit fits 12GB"},
}

LOCAL_PRESETS = [
    "criscar22/Qwen2.5-Coder-7B-Instruct-abliterated",
    "huihui-ai/Qwen3-4B-abliterated",
    "huihui-ai/Qwen3-8B-abliterated",
    "huihui-ai/Llama-3.2-3B-Instruct-abliterated",
    "mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated",
    "Goekdeniz-Guelmez/Josiefied-Qwen2.5-Coder-14B-Instruct-abliterated-v1",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
    "microsoft/phi-4",
    "google/gemma-3-4b-it",
    "microsoft/Phi-3.5-mini-instruct",
    "mistralai/Mistral-7B-Instruct-v0.3",
    "meta-llama/Llama-3.1-8B-Instruct",
    "Qwen/Qwen3-0.6B", "Qwen/Qwen3-1.7B", "Qwen/Qwen3-4B", "Qwen/Qwen3-8B",
]


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def read_chat_cfg() -> dict:
    cfg = json.loads(json.dumps(CHAT_DEFAULTS))
    try:
        with open(AI_CFG_FILE, "r", encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def write_chat_cfg(patch: dict) -> dict:
    cfg = _merge(read_chat_cfg(), patch or {})
    api = cfg.setdefault("api", {})
    if "hasKey" in api:
        api.pop("hasKey", None)
    # never persist hasKey markers in profiles; key presence is computed on read
    for prof in cfg.get("apiProfiles") or []:
        prof.pop("hasKey", None)
    try:
        with open(AI_CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass
    return cfg


def public_cfg(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    key = (out.get("api") or {}).get("apiKey") or ""
    out.setdefault("api", {})["apiKey"] = ""
    out["api"]["hasKey"] = bool(key)
    if isinstance(out.get("anthropic"), dict):
        akey = out["anthropic"].pop("apiKey", None) or ""
        out["anthropic"]["hasKey"] = bool(akey)
    for prof in out.get("apiProfiles") or []:
        prof["hasKey"] = bool(prof.pop("apiKey", None))
    out["presets"] = LOCAL_PRESETS
    out["peers"] = tailscale_peers()
    return out


def resolve_api_profile(cfg: dict, body: dict | None = None) -> dict:
    """Effective api settings: the selected profile wins over the flat fields,
    then an env-var key fills in when the profile has none stored."""
    eff = json.loads(json.dumps(cfg.get("api") or {}))
    profs = cfg.get("apiProfiles") or []
    wanted = ""
    if isinstance(body, dict):
        wanted = str(body.get("profile") or "").strip()
    if not wanted:
        wanted = str(cfg.get("profile") or "").strip()
    if wanted:
        prof = next((p for p in profs if p.get("name") == wanted), None)
        if prof:
            for k in ("baseUrl", "apiKey", "model", "envKey"):
                if prof.get(k):
                    eff[k] = prof[k]
    if not (eff.get("apiKey") or "").strip():
        env_name = (eff.get("envKey") or "").strip()
        if env_name and os.environ.get(env_name):
            eff["apiKey"] = os.environ[env_name]
    return eff


def api_headers(cfg: dict) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = (cfg.get("api") or {}).get("apiKey") or ""
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def normalize_api_base(raw: str) -> str:
    """Normalize a bare IP, host:port, or Ollama root URL into an OpenAI-compatible /v1 base."""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    parsed = urllib.parse.urlsplit(base)
    netloc = parsed.netloc
    path = parsed.path.rstrip("/")
    if ":" not in netloc and parsed.scheme == "http":
        netloc = f"{netloc}:11434"
    if path in ("", "/api"):
        path = "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, path, "", ""))


def api_url(cfg: dict, tail: str) -> str:
    base = normalize_api_base(((cfg.get("api") or {}).get("baseUrl") or ""))
    if not base:
        raise RuntimeError("set a VPS Ollama / API base URL first")
    return base + tail


def api_models(cfg: dict) -> list[str]:
    base = normalize_api_base(((cfg.get("api") or {}).get("baseUrl") or ""))
    if not base:
        raise RuntimeError("set a VPS Ollama / API base URL first")
    names: list[str] = []
    last_err: Exception | None = None
    # Try OpenAI-compatible /v1/models first, then Ollama native /api/tags
    root_base = base[:-3] if base.endswith("/v1") else base
    for url in (f"{base}/models", f"{root_base}/api/tags"):
        try:
            req = urllib.request.Request(url, headers=api_headers(cfg))
            with urllib.request.urlopen(req, timeout=10) as res:
                data = json.loads(res.read() or b"{}")
            items = data.get("data") or data.get("models") or []
            for item in items:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    name = item.get("id") or item.get("name") or item.get("model") or ""
                    if name:
                        names.append(str(name))
            if names:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if not names and last_err is not None:
        raise last_err
    return sorted(set(names))


def _parse_stream_tool_calls(chunk: dict) -> list[dict]:
    """Accumulate OpenAI-style delta.tool_calls across stream chunks."""
    calls: dict[int, dict] = {}
    for choice in chunk.get("choices") or []:
        for tc in (choice.get("delta") or {}).get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"] if not slot["name"] else ""
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    out = []
    for idx in sorted(calls):
        c = calls[idx]
        if not c["name"]:
            continue
        try:
            args = json.loads(c["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append({"id": c["id"] or f"call_{idx}", "name": c["name"], "args": args})
    return out


def _api_stream_once(cfg: dict, messages: list[dict], max_tokens: int, temperature: float,
                     tools: list[dict] | None = None):
    model = ((cfg.get("api") or {}).get("model") or "").strip()
    if not model:
        models = api_models(cfg)
        if not models:
            raise RuntimeError("no models found on remote Ollama server")
        model = models[0]
        cfg.setdefault("api", {})["model"] = model
        write_chat_cfg({"api": {"model": model}})
        AI.model = model
        AI._emit({"type": "model", "msg": model})
    body = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": True,
    }
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(
        api_url(cfg, "/chat/completions"),
        json.dumps(body).encode("utf-8"),
        {**api_headers(cfg), "Accept": "text/event-stream"},
    )
    parts: list[str] = []
    native_calls: dict[int, dict] = {}
    with urllib.request.urlopen(req, timeout=180) as res:
        for raw in res:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("", "[DONE]"):
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for call in _parse_stream_tool_calls(chunk):
                native_calls[len(native_calls)] = call
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    parts.append(piece)
                    AI._emit({"type": "delta", "text": piece})
            if CANCEL.is_set():
                break
    return "".join(parts).strip(), list(native_calls.values())


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools or []:
        f = t.get("function") or {}
        if f.get("name"):
            out.append({"name": f["name"], "description": f.get("description") or "",
                        "input_schema": f.get("parameters") or {"type": "object"}})
    return out


def _anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"type": "text", "text": p.get("text") or ""})
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url") or ""
                    m1 = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", url)
                    if m1:
                        parts.append({"type": "image",
                                      "source": {"type": "base64",
                                                 "media_type": m1.group(1),
                                                 "data": m1.group(2)}})
            content = parts or ""
        if content in ("", None):
            continue
        out.append({"role": role, "content": content})
    return out


def _anthropic_stream_once(eff: dict, system: str, messages: list[dict],
                           max_tokens: int, temperature: float,
                           tools: list[dict] | None = None):
    """Claude via Anthropic /v1/messages SSE. Returns (text, tool_uses)."""
    key = (eff.get("apiKey") or "").strip()
    if not key:
        raise RuntimeError("set an Anthropic API key first")
    model = (eff.get("model") or "claude-sonnet-4-5").strip()
    body = {
        "model": model,
        "system": system,
        "messages": _anthropic_messages(messages),
        "max_tokens": max(16, int(max_tokens)),
        "temperature": max(0.0, min(float(temperature), 1.0)),
        "stream": True,
    }
    if tools:
        body["tools"] = _anthropic_tools(tools)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        json.dumps(body).encode("utf-8"),
        {"x-api-key": key, "anthropic-version": "2023-06-01",
         "Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    text_parts: list[str] = []
    tool_uses: list[dict] = []
    cur_tool: dict | None = None
    with urllib.request.urlopen(req, timeout=300) as res:
        for raw in res:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "content_block_start":
                block = ev.get("content_block") or {}
                if block.get("type") == "tool_use":
                    cur_tool = {"id": block.get("id") or "", "name": block.get("name") or "",
                                "args_json": ""}
            elif etype == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    text_parts.append(delta["text"])
                    AI._emit({"type": "delta", "text": delta["text"]})
                elif delta.get("type") == "input_json_delta" and cur_tool is not None:
                    cur_tool["args_json"] += delta.get("partial_json") or ""
            elif etype == "content_block_stop":
                if cur_tool is not None:
                    try:
                        args = json.loads(cur_tool["args_json"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    tool_uses.append({"id": cur_tool["id"] or f"call_{len(tool_uses)}",
                                      "name": cur_tool["name"], "args": args})
                    cur_tool = None
            if CANCEL.is_set():
                break
    return "".join(text_parts).strip(), tool_uses


def anthropic_chat(eff: dict, cfg: dict, system: str, messages: list[dict],
                   max_tokens: int, temperature: float, root: str = "",
                   agent_enabled: bool = True, failsafe: bool = True):
    convo = list(messages)
    ws = root if (root and os.path.isdir(root)) else os.getcwd()
    max_rounds = agent_tools.MAX_AGENT_STEPS if agent_enabled else 0
    tools = tool_schemas(agent_enabled, ws)
    handed_to_local = False
    try:
        CANCEL.clear()
        for attempt in range(max_rounds + 1):
            if CANCEL.is_set():
                break
            text, tool_uses = _anthropic_stream_once(
                eff, system, convo, max_tokens, temperature,
                tools if attempt < max_rounds else None)
            if CANCEL.is_set():
                AI._emit({"type": "reply", "text": text})
                break
            if tool_uses:
                AI._emit({"type": "step", "text": text})
                convo.append({"role": "assistant", "content": [
                    {"type": "text", "text": text} if text else None,
                    *[{"type": "tool_use", "id": c["id"], "name": c["name"],
                       "input": c["args"]} for c in tool_uses],
                ]})
                convo[-1]["content"] = [p for p in convo[-1]["content"] if p]
                results = []
                for c in tool_uses:
                    if CANCEL.is_set():
                        break
                    obs = run_agent_tool({"name": c["name"], "args": c["args"]}, ws)
                    results.append({"type": "tool_result", "tool_use_id": c["id"],
                                    "content": obs})
                if CANCEL.is_set():
                    AI._emit({"type": "reply", "text": text})
                    break
                convo.append({"role": "user", "content": results})
                continue
            AI._emit({"type": "reply", "text": text})
            break
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        if failsafe and trigger_local_failsafe(
                cfg, convo, max_tokens, temperature, ws, agent_enabled, reason):
            handed_to_local = True
        else:
            AI._emit({"type": "error", "msg": f"anthropic: {reason}"})
    finally:
        if not handed_to_local:
            AI.state = "idle"
            AI._emit({"type": "state", "msg": "idle"})


def opencode_chat(cfg: dict, messages: list[dict], root: str = ""):
    """OpenCode CLI as a provider: `opencode run <prompt>` with streamed stdout."""
    if shutil.which("opencode") is None:
        AI._emit({"type": "error",
                  "msg": "opencode CLI not found on PATH — install it, then retry"})
        AI.state = "idle"
        AI._emit({"type": "state", "msg": "idle"})
        return
    parts = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            continue
        parts.append(f"[{role}]\n{_msg_text(m.get('content'))}")
    # a single user turn doesn't need a transcript wrapper
    if len(parts) == 1:
        prompt = _msg_text(messages[-1].get("content")).strip()
    else:
        prompt = "\n\n".join(parts).strip()
    text_lines: list[str] = []
    cwd = root if (root and os.path.isdir(root)) else os.getcwd()
    AI.state = "generating"
    CANCEL.clear()
    try:
        proc = subprocess.Popen(
            ["opencode", "run", prompt],
            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            **(hidden_kwargs()),
        )
        for line in proc.stdout:
            if CANCEL.is_set():
                proc.terminate()
                break
            clean = ANSI_RE.sub("", line.rstrip())
            if not clean or clean.startswith("> "):
                continue
            AI._emit({"type": "delta", "text": clean + "\n"})
            text_lines.append(clean)
        proc.wait(timeout=60)
        AI._emit({"type": "reply", "text": "\n".join(text_lines).strip()})
    except Exception as exc:  # noqa: BLE001
        AI._emit({"type": "error", "msg": f"opencode: {exc}"})
    finally:
        AI.state = "idle"
        AI._emit({"type": "state", "msg": "idle"})


def trigger_local_failsafe(
    cfg: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    root: str,
    agent_enabled: bool,
    reason: str,
) -> bool:
    """Fallback to local AirLLM worker when the remote VPS Ollama endpoint fails."""
    local_cfg = cfg.get("local") or {}
    local_model = (local_cfg.get("model") or LOCAL_PRESETS[0]).strip()
    compression = (local_cfg.get("compression") or "").strip()
    if not local_model:
        return False
    AI._emit({
        "type": "note",
        "msg": f"failsafe · VPS Ollama unreachable ({reason}) — engaging local AirLLM ({local_model})",
    })
    try:
        if AI.model != local_model:
            AI.send({"cmd": "load", "model": local_model,
                     "compression": compression or None})
        AI.send({
            "cmd": "chat",
            "messages": messages,
            "max_new_tokens": max_tokens,
            "temperature": temperature,
            "workspaceRoot": root,
            "agentTools": agent_enabled,
        })
        return True
    except Exception as exc:  # noqa: BLE001
        AI._emit({"type": "error", "msg": f"failsafe local worker failed: {exc}"})
        return False


def api_chat(cfg: dict, messages: list[dict], max_tokens: int, temperature: float,
             root: str = "", agent_enabled: bool = True, failsafe: bool = True,
             mode: str = "agent"):
    """Stream an OpenAI-compatible / Ollama completion with automatic local AirLLM failsafe."""
    convo = list(messages)
    ws = root if (root and os.path.isdir(root)) else os.getcwd()
    max_rounds = agent_tools.MAX_AGENT_STEPS if agent_enabled else 0
    handed_to_local = False
    tools = tool_schemas(agent_enabled, ws)
    try:
        CANCEL.clear()
        for attempt in range(max_rounds + 1):
            if CANCEL.is_set():
                break
            text, native = _api_stream_once(cfg, convo, max_tokens, temperature,
                                            tools if attempt < max_rounds else None)
            if CANCEL.is_set():
                AI._emit({"type": "reply", "text": text})
                break

            if native:  # native OpenAI-style tool calling round
                AI._emit({"type": "step", "text": text})
                assistant = {"role": "assistant", "content": text or None,
                             "tool_calls": [
                                 {"id": c["id"], "type": "function",
                                  "function": {"name": c["name"],
                                               "arguments": json.dumps(c["args"])}}
                                 for c in native]}
                tool_msgs: list[dict] = []
                for c in native:
                    if CANCEL.is_set():
                        break
                    obs = run_agent_tool(c, ws)
                    tool_msgs.append({"role": "tool", "tool_call_id": c["id"],
                                      "content": obs})
                if CANCEL.is_set():
                    AI._emit({"type": "reply", "text": text})
                    break
                convo = convo + [assistant, *tool_msgs]
                continue

            calls = agent_tools.parse_tool_calls(text) if attempt < max_rounds else []
            if not calls:
                AI._emit({"type": "reply", "text": text})
                break

            AI._emit({"type": "step", "text": text})
            observations: list[str] = []
            for call in calls:
                if CANCEL.is_set():
                    break
                obs = run_agent_tool(call, ws)
                observations.append(obs)
            if CANCEL.is_set():
                AI._emit({"type": "reply", "text": text})
                break

            obs_block = (
                "[Observation]\n"
                + "\n\n".join(observations)
                + "\n\nContinue based on the observation above. Emit another ACTION block if "
                  "more steps are needed, or provide your final answer directly."
            )
            convo = convo + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": obs_block},
            ]
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:240]
        except Exception:  # noqa: BLE001
            pass
        reason = f"HTTP {exc.code}: {detail or exc.reason}"
        if failsafe and trigger_local_failsafe(cfg, convo, max_tokens, temperature, ws, agent_enabled, reason):
            handed_to_local = True
        else:
            AI._emit({"type": "error", "msg": f"vps ollama {reason}"})
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        if failsafe and trigger_local_failsafe(cfg, convo, max_tokens, temperature, ws, agent_enabled, reason):
            handed_to_local = True
        else:
            AI._emit({"type": "error", "msg": f"vps ollama: {reason}"})
    finally:
        if not handed_to_local:
            AI.state = "idle"
            AI._emit({"type": "state", "msg": "idle"})


WANTS_WORKSPACE = re.compile(
    r"\b(project|workspace|repo|repository|codebase|code base|folder|directory|"
    r"files|review|audit|architecture|structure|readme|refactor)\b", re.IGNORECASE)
MENTION = re.compile(r"@([A-Za-z0-9_][A-Za-z0-9_./\\-]{1,120})")
MAX_CTX_CHARS = 32000
PER_FILE_CHARS = 9000
ENTRY_NAMES = {
    "server.py", "main.py", "app.py", "cli.py", "__main__.py", "index.js", "index.ts",
    "index.jsx", "index.tsx", "app.js", "app.ts", "main.rs", "main.go", "main.c",
    "main.cpp", "Program.cs", "Makefile", "Dockerfile",
}
MANIFESTS = ("README.md", "readme.md", "README.txt", "pyproject.toml", "package.json",
             "requirements.txt", "Cargo.toml", "go.mod", "setup.py", "docker-compose.yml",
             "compose.yaml", ".env.example")


def _read_capped(path: str, cap: int) -> str:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(cap + 1)[:cap]
    except OSError:
        return ""


def _walk(root: str, limit: int) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d.lower() not in IGNORED_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
            if len(out) >= limit:
                return out
    return out


def workspace_block(root: str, question: str, active_rel: str = "",
                    budget: int = MAX_CTX_CHARS) -> tuple[str, list[str]]:
    """Tree + the few files a reviewer actually needs, bounded by characters."""
    budget = max(2000, min(int(budget or MAX_CTX_CHARS), 128000))
    files = _walk(root, 6000)
    def _size_kb(rel):
        try:
            return os.path.getsize(os.path.join(root, rel.replace("/", os.sep))) // 1024 + 1
        except OSError:
            return 0
    lines = [f"{rel}  ({_size_kb(rel)} kB)" for rel in files[:300]]
    header = (f"Workspace root: {root}\n"
              f"{len(files)} files (dot-folders, .venv, node_modules and build output excluded)\n"
              f"{chr(10).join(lines)}"
              + ("\n… more files exist but are not listed" if len(files) > 300 else ""))
    parts = [header]
    used = len(header)
    attached: list[str] = []

    def add(rel: str, cap: int, label: str = "") -> None:
        nonlocal used
        if rel == active_rel:      # already attached as the active file
            return
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full) or rel in attached:
            return
        text = _read_capped(full, min(cap, budget - used))
        if not text.strip():
            return
        parts.append(f"{label or 'File'}: {rel}\n```\n{text}\n```")
        attached.append(rel)
        used += len(text) + len(rel) + 12

    # explicit @mentions win, then README/manifests, then the biggest source files
    for name in MENTION.findall(question or ""):
        try:
            rel = os.path.relpath(safe_join(root, name), os.path.abspath(root)).replace(os.sep, "/")
        except ValueError:
            continue
        add(rel, PER_FILE_CHARS)
    for name in MANIFESTS:
        if used > budget * 0.55:
            break
        if name in files:
            add(name, 4000)

    candidates = [rel for rel in files
                  if os.path.splitext(rel)[1].lower() in TEXT_EXT and rel not in attached]
    candidates.sort(key=lambda rel: (os.path.basename(rel) not in ENTRY_NAMES,
                                     rel.count("/"), -os.path.getsize(os.path.join(root, rel))))
    for rel in candidates:
        if used > budget:
            break
        add(rel, PER_FILE_CHARS)
    return "\n\n".join(parts), attached


def git_context_block(root: str) -> str:
    """Branch, status, and a compact diff stat so the model sees current work."""
    if not root or not os.path.isdir(root):
        return ""
    branch = git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch is None:
        return ""
    status = git(root, ["status", "--porcelain=v1", "-uall"]) or ""
    stat = git(root, ["diff", "--stat", "HEAD"]) or ""
    stat = "\n".join(stat.splitlines()[-12:])
    block = f"Git: on branch {branch.strip()}"
    if status.strip():
        lines = status.splitlines()
        block += f"\nChanged files ({len(lines)}):\n" + "\n".join(lines[:20])
        if len(lines) > 20:
            block += f"\n… and {len(lines) - 20} more"
    if stat.strip():
        block += f"\nDiff stat vs HEAD:\n{stat}"
    recent = git(root, ["log", "--oneline", "-5"]) or ""
    if recent.strip():
        block += f"\nRecent commits:\n{recent.strip()}"
    return block


def _msg_text(content) -> str:
    """Message content may be a string or an OpenAI-style content-part array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text") or "" for p in content
                        if isinstance(p, dict) and p.get("type") == "text")
    return ""


def build_messages(body: dict, cfg: dict) -> list[dict]:
    """System prompt from the mode, optional workspace/file context, then history."""
    mode = (body.get("mode") or cfg.get("mode") or "agent").strip()
    provider = (body.get("provider") or cfg.get("provider") or "local").strip()
    model_id = (body.get("apiModel") if provider == "api" else body.get("localModel")) or ""
    if not model_id:
        model_id = (cfg.get("api", {}).get("model") if provider == "api"
                    else cfg.get("local", {}).get("model")) or ""

    agent_enabled = bool(body.get("agentTools") if body.get("agentTools") is not None
                         else cfg.get("agentTools", True)) or (mode == "agent")
    root = (body.get("root") or "").strip()

    system = MODE_PROMPTS.get(mode, MODE_PROMPTS["chat"]) + HOUSE_RULES
    custom = (cfg.get("customInstructions") or "").strip()
    if custom:
        system += "\n\n## Operator preferences\n" + custom[:4000]
    if agent_enabled and mode != "edit":
        system += agent_tools.agent_system_prompt(model_id, root, is_admin())
        if root and os.path.isdir(root):
            system += agent_mcp_prompt(root)

    sys_blocks: list[str] = [system]

    path = (body.get("file") or "").strip()
    code = (body.get("fileText") or "").strip()
    selection = (body.get("selection") or "").strip()
    if body.get("contextFile") and code and mode != "chat":
        if len(code) > 16000:
            code = code[:16000] + "\n… (truncated)"
        sys_blocks.append(f"Active file: {path or '(untitled)'}\n\n{code}")
    if selection:
        if len(selection) > 6000:
            selection = selection[:6000] + "\n… (truncated)"
        sys_blocks.append(f"Selection from {path or 'the active file'}:\n\n{selection}")

    question = " ".join(_msg_text(m.get("content")) for m in (body.get("messages") or [])
                        if m.get("role") == "user")
    wants_workspace = bool(body.get("workspace")) or mode in ("agent", "review", "plan") or \
        WANTS_WORKSPACE.search(question or "")
    if root and os.path.isdir(root) and wants_workspace:
        budget = int(cfg.get("contextBudget") or MAX_CTX_CHARS)
        block, attached = workspace_block(root, question, path, budget)
        if block:
            sys_blocks.append(block)
            AI._emit({"type": "note",
                      "msg": "context · " + (", ".join(attached) if attached
                                             else "tree only")})
        git_block = git_context_block(root)
        if git_block:
            sys_blocks.append(git_block)

    out: list[dict] = [{"role": "system", "content": "\n\n".join(sys_blocks)}]
    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")
        if role in ("user", "assistant"):
            if isinstance(content, list) and role == "user":
                out.append({"role": role, "content": content})
                continue
            text = _msg_text(content).strip()
            if text:
                out.append({"role": role, "content": text})
    return out


def chat_dispatch(body: dict) -> dict:
    cfg = read_chat_cfg()
    provider = (body.get("provider") or cfg.get("provider") or "api").strip()
    failsafe = bool(body.get("failsafe") if body.get("failsafe") is not None
                    else cfg.get("failsafe", True))
    mode = (body.get("mode") or cfg.get("mode") or "agent").strip()
    agent_enabled = bool(body.get("agentTools") if body.get("agentTools") is not None
                         else cfg.get("agentTools", True)) or (mode == "agent")
    root = (body.get("root") or "").strip()
    try:
        max_tokens = int(body.get("maxTokens") or cfg.get("maxTokens") or 512)
    except (TypeError, ValueError):
        max_tokens = 512
    try:
        temperature = float(body.get("temperature") if body.get("temperature") is not None
                            else cfg.get("temperature", 0.6))
    except (TypeError, ValueError):
        temperature = 0.6

    messages = build_messages({**body, "allowRead": True}, cfg)
    if not any(m["role"] == "user" for m in messages):
        return {"ok": False, "error": "empty prompt"}
    PLUGINS.emit("chat_message", {"mode": mode, "provider": provider,
                                  "root": root,
                                  "text": _msg_text(messages[-1].get("content"))[:400]})

    if provider == "api":
        routing = cfg.get("models") or {}
        route = routing.get(mode) if isinstance(routing, dict) else None
        route_api = route.get("api") if isinstance(route, dict) else route
        route_local = route.get("local") if isinstance(route, dict) else None
        merged = _merge(cfg, {
            "failsafe": failsafe,
            "local": {
                "model": body.get("localModel") or route_local
                    or cfg["local"].get("model") or LOCAL_PRESETS[0],
                "compression": body.get("compression") or cfg["local"].get("compression") or "",
            },
            "api": {
                "model": body.get("apiModel") or route_api
                    or cfg["api"].get("model") or "",
                "baseUrl": normalize_api_base(body.get("apiBaseUrl") or cfg["api"].get("baseUrl") or ""),
            },
        })
        eff = resolve_api_profile(merged, body)   # profile overrides flat fields
        merged["api"] = {**merged["api"], **{k: v for k, v in eff.items() if v}}
        if not merged["api"].get("baseUrl") and not failsafe:
            return {"ok": False, "error": "set a VPS Ollama URL first"}
        AI.state = "generating"
        AI._emit({"type": "state", "msg": "generating"})
        threading.Thread(
            target=api_chat,
            args=(merged, messages, max_tokens, temperature, root,
                  agent_enabled and mode != "edit", failsafe, mode),
            name="vanta-api-chat",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "api",
            "profile": merged.get("profile") or "",
            "model": merged["api"].get("model") or "auto (vps)",
            "failsafe": failsafe,
            "sent": len(messages),
        }

    if provider == "anthropic":
        eff = _merge(cfg.get("anthropic") or {}, {})
        if body.get("anthropicModel"):
            eff["model"] = body["anthropicModel"]
        if isinstance(body.get("anthropicApiKey"), str) and body["anthropicApiKey"].strip():
            eff["apiKey"] = body["anthropicApiKey"].strip()
        if not (eff.get("apiKey") or "").strip():
            return {"ok": False, "error": "set an Anthropic API key first (chat settings)"}
        AI.state = "generating"
        AI._emit({"type": "state", "msg": "generating"})
        threading.Thread(
            target=anthropic_chat,
            args=(eff, cfg, messages[0]["content"] if messages and
                  messages[0]["role"] == "system" else "",
                  [m for m in messages if m["role"] != "system"],
                  max_tokens, temperature, root,
                  agent_enabled and mode != "edit", failsafe),
            name="vanta-anthropic-chat",
            daemon=True,
        ).start()
        return {"ok": True, "provider": "anthropic", "model": eff.get("model"),
                "failsafe": failsafe, "sent": len(messages)}

    if provider == "opencode":
        AI.state = "generating"
        AI._emit({"type": "state", "msg": "generating"})
        threading.Thread(
            target=opencode_chat,
            args=(cfg, messages, root),
            name="vanta-opencode-chat",
            daemon=True,
        ).start()
        return {"ok": True, "provider": "opencode", "sent": len(messages)}

    route = (cfg.get("models") or {}).get(mode) if isinstance(cfg.get("models"), dict) else None
    route_local = route.get("local") if isinstance(route, dict) else None
    model = body.get("localModel") or route_local or cfg["local"].get("model") or ""
    if not model:
        return {"ok": False, "error": "set a local model id first"}
    compression = (body.get("compression") or cfg["local"].get("compression") or "")
    try:
        if AI.model != model:
            AI.send({"cmd": "load", "model": model,
                     "compression": compression or None})
        AI.send({"cmd": "chat", "messages": messages, "max_new_tokens": max_tokens,
                 "temperature": temperature,
                 "workspaceRoot": root,
                 "agentTools": agent_enabled and mode != "edit"})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"worker: {exc}"}
    return {"ok": True, "provider": "local", "model": model, "sent": len(messages)}


def _search_files(root: str, needle: str, use_regex: bool, case: bool,
                  include: str) -> tuple[re.Pattern, list[str]]:
    """Compile the pattern and return (pattern, files under root with >=1 hit)."""
    flags = 0 if case else re.IGNORECASE
    pattern = re.compile(needle if use_regex else re.escape(needle), flags)
    inc_re = None
    if include.strip():
        try:
            inc_re = re.compile(include.replace(".", r"\.").replace("*", ".*"),
                                re.IGNORECASE)
        except re.error:
            inc_re = None
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in IGNORED_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if inc_re and not inc_re.search(rel):
                continue
            if not is_texty(full):
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    if any(pattern.search(line) for line in f):
                        files.append(rel)
            except OSError:
                continue
    return pattern, files


def _apply_replace(root: str, needle: str, use_regex: bool, case: bool,
                   replacement: str, files: list[str]) -> dict:
    """Replace needle (or regex) matches in the given files, atomically."""
    flags = 0 if case else re.IGNORECASE
    pattern = re.compile(needle if use_regex else re.escape(needle), flags)
    repl = replacement if use_regex else replacement.replace("\\", "\\\\")
    changed: list[str] = []
    failed: list[dict] = []
    for rel in files:
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            with open(full, "r", encoding="utf-8", errors="replace", newline="") as f:
                raw = f.read()
            updated = pattern.sub(repl, raw)
            if updated == raw:
                continue
            tmp = full + f".vanta-tmp-{uuid.uuid4().hex[:6]}"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(updated)
            os.replace(tmp, full)
            changed.append(rel)
            WATCHER.mark(full)
        except Exception as exc:  # noqa: BLE001
            failed.append({"path": rel, "error": str(exc)})
    return {"changed": changed, "failed": failed}


# --------------------------------------------------------------------------- #
# request handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "VantaIDE/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _authorized(self, path: str, q: dict) -> bool:
        if not AUTH_TOKEN or not path.startswith("/api/") or path == "/api/ping":
            return True
        supplied = self.headers.get("X-Vanta-Token") or q.get("token") or ""
        return secrets.compare_digest(str(supplied), AUTH_TOKEN)

    # ------------------------------ plumbing ------------------------------ #

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                if not self._authorized(path, query):
                    return error(self, "unauthorized", 401)
                return self.api_get(path, query)
            return self.static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            error(self, f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if parsed.path.startswith("/api/") and not self._authorized(parsed.path, query):
                return error(self, "unauthorized", 401)
            return self.api_post(parsed.path, self._body())
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            error(self, f"{type(exc).__name__}: {exc}", 500)

    # ------------------------------ static -------------------------------- #

    def static(self, path: str):
        if path in ("", "/"):
            path = "/index.html"
        target = os.path.abspath(os.path.join(WEB, path.lstrip("/")))
        if not os.path.normcase(target).startswith(os.path.normcase(WEB)):
            return error(self, "nope", 403)
        if not os.path.isfile(target):
            return error(self, "not found", 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        if target.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        if target.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------ GET api ------------------------------- #

    def api_get(self, path: str, q: dict):
        if path.startswith("/api/ext/"):
            result = PLUGINS.dispatch(path[len("/api/ext/"):], "GET", {}, q)
            if result is None:
                return error(self, "no plugin route", 404)
            return json_response(self, result)

        if path == "/api/plugins":
            return json_response(self, {"ok": True, "plugins": PLUGINS.status()})

        if path == "/api/ai/services":
            services = []
            for s in AI_SERVICES:
                services.append({
                    **s,
                    "envSet": bool(s.get("envKey") and os.environ.get(s["envKey"])),
                })
            return json_response(self, {"ok": True, "services": services})

        if path == "/api/ide/editors":
            return json_response(self, {"ok": True, "editors": interop.detect_editors()})

        if path == "/api/ide/import-settings":
            return json_response(self, interop.import_settings())

        if path == "/api/ide/import-keybindings":
            return json_response(self, interop.import_keybindings(HERE))

        if path == "/api/ide/snippets":
            return json_response(self, interop.import_snippets())

        if path == "/api/update/check":
            return json_response(self, updater.check())

        if path == "/api/remotes/list":
            here = HERE
            hosts = remotes.load_remotes(q.get("root", ""), here)
            distros = remotes.wsl_distros()
            return json_response(self, {"ok": True, "ssh": [
                {**h, "id": h.get("name") or h.get("host")} for h in hosts],
                "wsl": [{"name": d, "root": remotes.wsl_root(d),
                         "home": remotes.wsl_home(d)} for d in distros],
                "sshAvailable": shutil.which("ssh") is not None})

        if path == "/api/remotes/open":
            here = HERE
            kind = (body.get("kind") or "ssh").strip()
            root = body.get("root") or ""
            if kind == "wsl":
                distro = (body.get("distro") or "").strip()
                if distro not in remotes.wsl_distros():
                    return error(self, "unknown WSL distro")
                return json_response(self, {"ok": True,
                                            "root": remotes.wsl_home(distro),
                                            "shellCmd": f'wsl.exe -d "{distro}"'})
            host_cfg = next((h for h in remotes.load_remotes(root, here)
                             if (h.get("name") or h.get("host")) == body.get("id")),
                            None)
            if not host_cfg:
                return error(self, "unknown ssh host")
            code, out, err = remotes.ssh_exec(host_cfg, "echo ok", timeout=15)
            if code != 0:
                return error(self, err.strip() or "ssh connect failed", 502)
            return json_response(self, {"ok": True, "id": body.get("id"),
                                        "shellCmd": "ssh " + host_cfg["host"]})

        if path == "/api/remotes/ls":
            here = HERE
            host_cfg = next((h for h in remotes.load_remotes(body.get("root") or "", here)
                             if (h.get("name") or h.get("host")) == body.get("id")), None)
            if not host_cfg:
                return error(self, "unknown ssh host")
            try:
                entries = remotes.ssh_list(host_cfg, body.get("path") or ".")
            except Exception as exc:  # noqa: BLE001
                return error(self, str(exc), 502)
            return json_response(self, {"ok": True, "entries": entries,
                                        "path": body.get("path") or "."})

        if path == "/api/remotes/read":
            here = HERE
            host_cfg = next((h for h in remotes.load_remotes(body.get("root") or "", here)
                             if (h.get("name") or h.get("host")) == body.get("id")), None)
            if not host_cfg:
                return error(self, "unknown ssh host")
            try:
                data = remotes.ssh_read(host_cfg, body.get("path") or "")
            except Exception as exc:  # noqa: BLE001
                return error(self, str(exc), 502)
            if len(data) > 4 * 1024 * 1024:
                return error(self, "file too large for remote editing")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                return error(self, "binary file")
            return json_response(self, {"ok": True, "content": content})

        if path == "/api/remotes/write":
            here = HERE
            host_cfg = next((h for h in remotes.load_remotes(body.get("root") or "", here)
                             if (h.get("name") or h.get("host")) == body.get("id")), None)
            if not host_cfg:
                return error(self, "unknown ssh host")
            try:
                remotes.ssh_write(host_cfg, body.get("path") or "",
                                  (body.get("content") or "").encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                return error(self, str(exc), 502)
            return json_response(self, {"ok": True})
        if path == "/api/ping":
            return json_response(self, {
                "ok": True,
                "vanta": True,
                "admin": is_admin(),
                "pid": os.getpid(),
                "port": self.server.server_address[1],
                "version": self.server_version,
            })

        if path == "/api/env":
            return json_response(self, {
                "ok": True,
                "cwd": os.getcwd(),
                "home": os.path.expanduser("~"),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "admin": is_admin(),
                "tailscale": tailscale_ip(),
                "recent": read_state().get("recent", []),
                "lastRoot": read_state().get("lastRoot"),
                "drives": [f"{d}:\\" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                           if os.path.exists(f"{d}:\\")] if os.name == "nt" else ["/"],
                "git": shutil.which("git") is not None,
                "shell": Terminal.SHELL,
                "ai": {"state": AI.state, "model": AI.model},
            })

        if path == "/api/browse":
            target = q.get("path") or os.path.expanduser("~")
            target = os.path.abspath(os.path.expandvars(target))
            if not os.path.isdir(target):
                return error(self, "not a directory")
            entries = []
            try:
                with os.scandir(target) as it:
                    for e in it:
                        if e.name.startswith("."):
                            continue
                        try:
                            is_dir = e.is_dir()
                        except OSError:
                            continue
                        entries.append({"name": e.name, "path": e.path, "isDir": is_dir})
            except PermissionError:
                return error(self, "permission denied", 403)
            entries.sort(key=lambda x: (not x["isDir"], x["name"].lower()))
            parent = os.path.dirname(target.rstrip("\\/")) or target
            return json_response(self, {"ok": True, "path": target, "parent": parent,
                                        "entries": entries, "sep": os.sep})

        if path == "/api/list":
            root, rel = q.get("root", ""), q.get("path", "")
            target = safe_join(root, rel)
            entries = []
            try:
                with os.scandir(target) as it:
                    for e in it:
                        low = e.name.lower()
                        try:
                            is_dir = e.is_dir()
                        except OSError:
                            continue
                        if is_dir and (low in IGNORED_DIRS or low.startswith(".")):
                            continue
                        stat = None
                        try:
                            stat = e.stat()
                        except OSError:
                            pass
                        entries.append({
                            "name": e.name,
                            "path": os.path.relpath(e.path, root).replace(os.sep, "/"),
                            "isDir": is_dir,
                            "size": stat.st_size if stat and not is_dir else 0,
                            "mtime": int(stat.st_mtime) if stat else 0,
                        })
            except PermissionError:
                return error(self, "permission denied", 403)
            entries.sort(key=lambda x: (not x["isDir"], x["name"].lower()))
            return json_response(self, {"ok": True, "entries": entries})

        if path == "/api/tree":
            root = q.get("root", "")
            if not os.path.isdir(root):
                return error(self, "bad root")
            return json_response(self, {"ok": True, "root": os.path.abspath(root),
                                        "name": os.path.basename(root.rstrip("\\/")) or root})

        if path == "/api/files":
            root = q.get("root", "")
            if not os.path.isdir(root):
                return error(self, "bad root")
            files = walk_files(root)
            return json_response(self, {"ok": True, "files": files, "truncated":
                                        len(files) >= MAX_LISTED_FILES})

        if path == "/api/file":
            root, rel = q.get("root", ""), q.get("path", "")
            target = safe_join(root, rel)
            if not os.path.isfile(target):
                return error(self, "not a file", 404)
            size = os.path.getsize(target)
            with open(target, "rb") as f:
                head = f.read(8192)
            if not is_texty(target, head):
                return json_response(self, {"ok": True, "binary": True, "size": size,
                                            "content": ""})
            if size > MAX_FILE_BYTES:
                return json_response(self, {"ok": True, "tooLarge": True, "size": size,
                                            "content": ""})
            with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
                content = f.read()
            return json_response(self, {"ok": True, "content": content, "size": size,
                                        "mtime": int(os.path.getmtime(target))})

        if path == "/api/stat":
            root, rel = q.get("root", ""), q.get("path", "")
            target = safe_join(root, rel)
            if not os.path.exists(target):
                return error(self, "missing", 404)
            st = os.stat(target)
            return json_response(self, {"ok": True, "mtime": int(st.st_mtime),
                                        "size": st.st_size})

        if path == "/api/search":
            root = q.get("root", "")
            needle = q.get("q", "")
            if not os.path.isdir(root) or not needle:
                return error(self, "needle and root required")
            use_regex = q.get("regex") == "1"
            case = q.get("case") == "1"
            include = q.get("include", "").strip()
            flags = 0 if case else re.IGNORECASE
            try:
                pattern = re.compile(needle if use_regex else re.escape(needle), flags)
            except re.error as exc:
                return error(self, f"bad regex: {exc}")
            hits = []
            scanned = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in IGNORED_DIRS and not d.startswith(".")]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    if include and not re.search(include.replace(".", r"\.").replace("*", ".*"),
                                                 rel, re.IGNORECASE):
                        continue
                    if not is_texty(full):
                        continue
                    try:
                        if os.path.getsize(full) > MAX_FILE_BYTES:
                            continue
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            scanned += 1
                            for n, line in enumerate(f, 1):
                                if pattern.search(line):
                                    hits.append({"path": rel, "line": n,
                                                 "text": line.rstrip("\n")[:400]})
                                    if len(hits) >= MAX_RESULTS:
                                        return json_response(self, {
                                            "ok": True, "hits": hits, "truncated": True,
                                            "scanned": scanned})
                    except OSError:
                        continue
            return json_response(self, {"ok": True, "hits": hits, "truncated": False,
                                        "scanned": scanned})

        if path == "/api/git/status":
            root = q.get("root", "")
            return json_response(self, self.git_status(root))

        if path == "/api/git/diff":
            root, rel = q.get("root", ""), q.get("path", "")
            target = safe_join(root, rel)
            if not os.path.isfile(target):
                return error(self, "missing file", 404)
            head = git(root, ["show", f"HEAD:{rel}"])
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    work = f.read()
            except OSError as exc:
                return error(self, str(exc))
            return json_response(self, {"ok": True, "original": head or "",
                                        "modified": work, "new": head is None})

        if path == "/api/git/branches":
            root = q.get("root", "")
            if not root or not os.path.isdir(root):
                return error(self, "bad root")
            code, out, err = git_out(root, ["branch", "--format=%(refname:short)"])
            if code != 0:
                return json_response(self, {"ok": False, "error": err.strip() or "git failed"})
            branches = [b.strip().lstrip("* ").strip() for b in out.splitlines() if b.strip()]
            cur = self.git(root, ["rev-parse", "--abbrev-ref", "HEAD"]) or ""
            return json_response(self, {"ok": True, "branches": branches,
                                        "current": cur.strip()})

        if path == "/api/watch/poll":
            since = float(q.get("since") or 0)
            changes = WATCHER.poll(since)
            return json_response(self, {
                "ok": True, "supported": WATCHER.observer is not None,
                "changes": [{"path": rel, "ts": ts} for rel, ts in changes],
            })

        if path == "/api/mcp/status":
            return json_response(self, {"ok": True,
                                        "servers": MCP.status(q.get("root", ""))})

        if path == "/api/lsp/status":
            return json_response(self, {"ok": True,
                                        "servers": LSP.status(q.get("root", ""))})

        if path in ("/api/lsp/hover", "/api/lsp/definition", "/api/lsp/completion"):
            root, rel = q.get("root", ""), q.get("path", "")
            target = safe_join(root, rel)
            sess = LSP.session_for(root, target)
            if not sess:
                return json_response(self, {"ok": False, "error": "no language server"})
            fn = {"hover": sess.hover, "definition": sess.definition,
                  "completion": sess.completion}[path.rsplit("/", 1)[1]]
            try:
                result = fn(target, int(q.get("line") or 1), int(q.get("col") or 1))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"ok": False, "error": str(exc)})
            return json_response(self, {"ok": True, "result": result})

        if path == "/api/lsp/diagnostics":
            root, rel = q.get("root", ""), q.get("path", "")
            target = safe_join(root, rel)
            sess = LSP.session_for(root, target)
            return json_response(self, {
                "ok": True,
                "diagnostics": sess.diagnostics(target) if sess else [],
            })

        if path == "/api/gh/prs":
            root = q.get("root", "")
            gh = shutil.which("gh")
            if not gh:
                return json_response(self, {"ok": False, "error": "gh CLI not installed"})
            try:
                out = subprocess.run(
                    [gh, "pr", "list", "--json", "number,title,headRefName,isDraft"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=root or None, timeout=30,
                    **hidden_kwargs())
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"ok": False, "error": str(exc)})
            if out.returncode != 0:
                return json_response(self, {"ok": False,
                                            "error": out.stderr.strip() or "gh failed"})
            try:
                prs = json.loads(out.stdout or "[]")
            except json.JSONDecodeError:
                prs = []
            return json_response(self, {"ok": True, "prs": prs})

        if path == "/api/tasks":
            root = q.get("root") or HERE
            tasks = []
            for base in (root, HERE):
                p = os.path.join(base or "", ".vanta", "tasks.json")
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        tasks = data.get("tasks") or []
                        break
                    except Exception:  # noqa: BLE001
                        pass
            return json_response(self, {"ok": True, "tasks": tasks})

        if path == "/api/config/keybindings":
            root = q.get("root") or HERE
            kb = {}
            for base in (root, HERE):
                p = os.path.join(base or "", ".vanta", "keybindings.json")
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            kb = json.load(f)
                        break
                    except Exception:  # noqa: BLE001
                        pass
            return json_response(self, {"ok": True, "keybindings": kb})

        if path == "/api/session/load":
            root = os.path.abspath(q.get("root") or "")
            state = read_state()
            sessions = state.get("sessions") or {}
            data = sessions.get(os.path.normcase(root)) or {}
            return json_response(self, {"ok": True, "session": data})

        if path == "/api/ollama/tags":
            cfg = read_chat_cfg()
            base = normalize_api_base(q.get("base") or (cfg.get("api") or {}).get("baseUrl") or "")
            if not base:
                return error(self, "no Ollama base URL configured")
            root_base = base[:-3] if base.endswith("/v1") else base
            try:
                req = urllib.request.Request(f"{root_base}/api/tags",
                                             headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as res:
                    data = json.loads(res.read() or b"{}")
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"ok": False, "error": str(exc)})
            models = [{"name": m.get("name") or m.get("model") or "",
                       "size": m.get("size") or 0}
                      for m in data.get("models") or [] if (m.get("name") or m.get("model"))]
            return json_response(self, {"ok": True, "models": models})

        if path == "/api/term/poll":
            with TERMS_LOCK:
                term = TERMS.get(q.get("id", ""))
            if not term:
                return error(self, "no session", 404)
            text, total = term.poll(int(q.get("cursor") or 0))
            return json_response(self, {"ok": True, "output": text, "cursor": total,
                                        "cwd": term.cwd, "alive": term.alive,
                                        "busy": term.busy})

        if path == "/api/debug/poll":
            events, seq = DEBUGGER.since(int(q.get("cursor") or 0))
            return json_response(self, {"ok": True, "events": events,
                                        "cursor": seq, "state": DEBUGGER.state})

        if path == "/api/debug/stack":
            return json_response(self, DEBUGGER.stack())

        if path == "/api/debug/variables":
            frame = q.get("frame")
            ref = q.get("ref")
            try:
                if ref is not None:
                    return json_response(self, DEBUGGER.variables(ref=int(ref)))
                return json_response(self, DEBUGGER.variables(frame=int(frame or 0)))
            except (TypeError, ValueError):
                return error(self, "frame or ref required")

        if path == "/api/ai/poll":
            events, seq = AI.since(int(q.get("cursor") or 0))
            return json_response(self, {"ok": True, "events": events, "cursor": seq,
                                        "state": AI.state, "model": AI.model})

        if path == "/api/ai/config":
            return json_response(self, {"ok": True, **public_cfg(read_chat_cfg())})

        if path == "/api/ai/models":
            cfg = read_chat_cfg()
            provider = (q.get("provider") or cfg.get("provider") or "local").strip()
            if provider == "api":
                try:
                    names = api_models(cfg)
                    return json_response(self, {"ok": True, "models": names})
                except Exception as exc:  # noqa: BLE001
                    return json_response(self, {"ok": False, "models": [],
                                                "error": str(exc)})
            return json_response(self, {"ok": True, "models": LOCAL_PRESETS,
                                    "meta": PRESET_META})

        if path == "/api/hardware":
            info = {"gpu": [], "torch": None}
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,driver_version",
                     "--format=csv,noheader"], capture_output=True, text=True, timeout=10,
                    **hidden_kwargs())
                for line in out.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        info["gpu"].append({"name": parts[0], "vram": parts[1],
                                            "free": parts[2], "driver": parts[3]})
            except Exception:
                pass
            return json_response(self, {"ok": True, **info})

        if path == "/api/admin/elevate":
            if os.name != "nt":
                return error(self, "elevation is only available on Windows")
            pyw = os.path.join(os.path.dirname(HERE), ".venv", "Scripts", "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = "pythonw"
            desk = os.path.join(HERE, "desktop.py")
            root = os.path.dirname(HERE)
            cmd = f"Start-Sleep -Milliseconds 600; Start-Process '{pyw}' -ArgumentList '\"{desk}\"' -WorkingDirectory '{root}'"
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command",
                     f"Start-Process powershell -ArgumentList '-NoProfile -WindowStyle Hidden -Command \"{cmd}\"' -Verb RunAs"],
                    **hidden_kwargs(),
                )
                threading.Timer(0.4, lambda: os._exit(0)).start()
                return json_response(self, {"ok": True, "elevating": True})
            except Exception as exc:  # noqa: BLE001
                return error(self, f"failed to elevate: {exc}", 500)

        return error(self, "unknown endpoint", 404)

    # ------------------------------ POST api ------------------------------ #

    def api_post(self, path: str, body: dict):
        if path.startswith("/api/ext/"):
            result = PLUGINS.dispatch(path[len("/api/ext/"):], "POST", body, {})
            if result is None:
                return error(self, "no plugin route", 404)
            return json_response(self, result)

        if path == "/api/plugins/reload":
            PLUGINS.load_all(body.get("root") or "")
            return json_response(self, {"ok": True, "plugins": PLUGINS.status()})

        if path == "/api/update/stage":
            return json_response(self, updater.apply())

        if path == "/api/update/apply":
            return json_response(self, updater.restart_into_update())

        if path == "/api/project/open":
            root = os.path.abspath(os.path.expandvars(os.path.expanduser(body.get("path", ""))))
            if not os.path.isdir(root):
                return error(self, "not a directory")
            state = read_state()
            recent = [r for r in state.get("recent", []) if os.path.normcase(r) != os.path.normcase(root)]
            recent.insert(0, root)
            state["recent"] = recent[:12]
            state["lastRoot"] = root
            write_state(state)
            PLUGINS.emit("project_open", {"root": root})
            return json_response(self, {"ok": True, "root": root,
                                        "name": os.path.basename(root.rstrip("\\/")) or root})

        if path == "/api/watch/start":
            root = os.path.abspath(body.get("root") or "")
            if not os.path.isdir(root):
                return error(self, "not a directory")
            WATCHER.start(root)
            return json_response(self, {"ok": True, "supported": WATCHER.observer is not None})

        if path == "/api/search/replace":
            root = body.get("root", "")
            needle = str(body.get("q") or "")
            if not os.path.isdir(root) or not needle:
                return error(self, "root and q required")
            use_regex = bool(body.get("regex"))
            case = bool(body.get("case"))
            include = str(body.get("include") or "")
            replacement = str(body.get("replacement") or "")
            try:
                _, files = _search_files(root, needle, use_regex, case, include)
            except re.error as exc:
                return error(self, f"bad regex: {exc}")
            if body.get("paths"):
                wanted = {os.path.normcase(p) for p in body["paths"]}
                files = [f for f in files if os.path.normcase(f) in wanted]
            if body.get("preview"):
                return json_response(self, {"ok": True, "files": files[:200],
                                            "total": len(files)})
            result = _apply_replace(root, needle, use_regex, case, replacement, files)
            return json_response(self, {"ok": True, **result})

        if path == "/api/git/checkout":
            root, branch = body.get("root", ""), (body.get("branch") or "").strip()
            if not branch:
                return error(self, "branch required")
            code, out, err = git_out(root, ["checkout", branch])
            if code != 0:
                return error(self, err.strip() or "checkout failed", 500)
            return json_response(self, {"ok": True, "output": (out + err).strip()})

        if path == "/api/git/push":
            root = body.get("root", "")
            code, out, err = git_out(root, ["push"])
            if code != 0:
                return error(self, (err or out).strip() or "push failed", 500)
            return json_response(self, {"ok": True, "output": (out + err).strip()})

        if path == "/api/git/pull":
            root = body.get("root", "")
            code, out, err = git_out(root, ["pull"])
            if code != 0:
                return error(self, (err or out).strip() or "pull failed", 500)
            return json_response(self, {"ok": True, "output": (out + err).strip()})

        if path == "/api/debug/start":
            root = body.get("root") or ""
            script = body.get("script") or ""
            if not root or not os.path.isdir(root):
                return error(self, "root required")
            target = safe_join(root, script)
            if not os.path.isfile(target):
                return error(self, "script not found", 404)
            result = debug_bridge.DEBUGGER.start(
                root, script, body.get("args") or [],
                bool(body.get("justMyCode") is False))
            return json_response(self, result,
                                 200 if result.get("ok") else 500)

        if path == "/api/debug/breakpoints":
            rel = body.get("path") or ""
            lines = [int(l) for l in (body.get("lines") or [])]
            target = safe_join(body.get("root") or "", rel)
            if not os.path.isfile(target):
                return error(self, "file not found", 404)
            return json_response(self,
                                 debug_bridge.DEBUGGER.set_breakpoints(rel, lines))

        if path == "/api/debug/cmd":
            command = (body.get("cmd") or "").strip()
            return json_response(self, debug_bridge.DEBUGGER.cmd(command))

        if path == "/api/debug/evaluate":
            expr = (body.get("expr") or "").strip()
            if not expr:
                return error(self, "expr required")
            frame = body.get("frame")
            try:
                frame_id = int(frame) if frame is not None else None
            except (TypeError, ValueError):
                frame_id = None
            return json_response(self,
                                 debug_bridge.DEBUGGER.evaluate(expr, frame_id))

        if path == "/api/confirm":
            cid = str(body.get("id") or "")
            approve = bool(body.get("approve"))
            with CONFIRMS_LOCK:
                pending = CONFIRMS.pop(cid, None)
            if not pending:
                return error(self, "no pending confirmation", 404)
            if approve:
                pending["event"].set()
            else:
                pending["denied"] = True
                pending["event"].set()
            return json_response(self, {"ok": True})

        if path == "/api/ai/cancel":
            CANCEL.set()
            if AI.proc and AI.proc.poll() is None:
                try:
                    AI.send({"cmd": "stop"})
                except Exception:  # noqa: BLE001
                    pass
            return json_response(self, {"ok": True})

        if path == "/api/mcp/connect":
            name = (body.get("name") or "").strip()
            root = body.get("root") or ""
            if not name:
                return error(self, "name required")
            sess = MCP.get(name, root)
            if not sess:
                return error(self, f"mcp {name}: not configured or failed to start", 500)
            return json_response(self, {"ok": True, "tools": sess.tools})

        if path == "/api/mcp/disconnect":
            MCP.disconnect((body.get("name") or "").strip())
            return json_response(self, {"ok": True})

        if path == "/api/lsp/open":
            root, rel = body.get("root", ""), body.get("path", "")
            target = safe_join(root, rel)
            sess = LSP.session_for(root, target)
            if not sess:
                return json_response(self, {"ok": False, "error": "no language server"})
            try:
                sess.open(target, str(body.get("text") or ""))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"ok": False, "error": str(exc)})
            return json_response(self, {"ok": True})

        if path == "/api/lsp/change":
            root, rel = body.get("root", ""), body.get("path", "")
            target = safe_join(root, rel)
            sess = LSP.session_for(root, target)
            if not sess:
                return json_response(self, {"ok": False, "error": "no language server"})
            try:
                sess.change(target, str(body.get("text") or ""),
                            int(body.get("version") or 1))
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"ok": False, "error": str(exc)})
            return json_response(self, {"ok": True})

        if path == "/api/db/query":
            root, rel = body.get("root", ""), body.get("path", "")
            sql = str(body.get("sql") or "").strip()
            if not sql:
                return error(self, "sql required")
            target = safe_join(root, rel)
            if not os.path.isfile(target):
                return error(self, "database file not found", 404)
            try:
                con = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5)
                con.row_factory = sqlite3.Row
                cur = con.execute(sql)
                rows = cur.fetchmany(200)
                cols = [d[0] for d in cur.description] if cur.description else []
                con.close()
                return json_response(self, {"ok": True, "columns": cols,
                                            "rows": [tuple(r) for r in rows],
                                            "truncated": len(rows) == 200})
            except sqlite3.Error as exc:
                return error(self, f"sqlite: {exc}")

        if path == "/api/ai/transcribe":
            audio_b64 = str(body.get("audio") or "")
            if not audio_b64:
                return error(self, "audio required (base64)")
            try:
                from faster_whisper import WhisperModel
            except Exception:  # noqa: BLE001
                return error(self, "faster-whisper not installed — "
                                   "pip install faster-whisper", 501)
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(),
                               f"vanta-audio-{uuid.uuid4().hex[:8]}.webm")
            try:
                with open(tmp, "wb") as f:
                    f.write(base64.b64decode(audio_b64))
                size = os.path.getsize(tmp)
                model_name = str(body.get("model") or "small")
                wm = WhisperModel(model_name, device="auto", compute_type="auto")
                segments, _info = wm.transcribe(tmp)
                text = " ".join(s.text.strip() for s in segments).strip()
                return json_response(self, {"ok": True, "text": text, "bytes": size})
            except Exception as exc:  # noqa: BLE001
                return error(self, f"transcribe failed: {exc}", 500)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        if path == "/api/http":
            url = str(body.get("url") or "").strip()
            method = str(body.get("method") or "GET").upper()
            if not url.startswith(("http://", "https://")):
                return error(self, "http(s) url required")
            headers = {str(k): str(v) for k, v in (body.get("headers") or {}).items()}
            data = None
            if body.get("body") not in (None, ""):
                payload = body.get("body")
                data = payload.encode("utf-8") if isinstance(payload, str) \
                    else json.dumps(payload).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    raw = res.read(200_000)
                    return json_response(self, {
                        "ok": True, "status": res.status,
                        "ms": int((time.time() - t0) * 1000),
                        "headers": dict(res.headers.items()),
                        "body": raw.decode("utf-8", "replace"),
                    })
            except urllib.error.HTTPError as exc:
                raw = exc.read(200_000)
                return json_response(self, {
                    "ok": True, "status": exc.code,
                    "ms": int((time.time() - t0) * 1000),
                    "headers": dict(exc.headers.items()) if exc.headers else {},
                    "body": raw.decode("utf-8", "replace"),
                })
            except Exception as exc:  # noqa: BLE001
                return error(self, str(exc), 502)

        if path == "/api/gh/pr/checkout":
            root = body.get("root", "")
            number = body.get("number")
            gh = shutil.which("gh")
            if not gh:
                return error(self, "gh CLI not installed")
            try:
                out = subprocess.run(
                    [gh, "pr", "checkout", str(number)],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=root or None, timeout=60,
                    **hidden_kwargs())
            except Exception as exc:  # noqa: BLE001
                return error(self, str(exc), 500)
            if out.returncode != 0:
                return error(self, out.stderr.strip() or "checkout failed", 500)
            return json_response(self, {"ok": True, "output": (out.stdout + out.stderr).strip()})

        if path == "/api/ollama/pull":
            cfg = read_chat_cfg()
            base = normalize_api_base(body.get("base") or (cfg.get("api") or {}).get("baseUrl") or "")
            model = (body.get("model") or "").strip()
            if not base or not model:
                return error(self, "base and model required")
            root_base = base[:-3] if base.endswith("/v1") else base
            try:
                req = urllib.request.Request(
                    f"{root_base}/api/pull",
                    json.dumps({"model": model, "stream": False}).encode("utf-8"),
                    {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=1800) as res:
                    data = json.loads(res.read() or b"{}")
            except Exception as exc:  # noqa: BLE001
                return error(self, f"pull failed: {exc}", 502)
            if data.get("error"):
                return error(self, data["error"], 502)
            return json_response(self, {"ok": True, "status": data.get("status") or "done"})

        if path == "/api/ollama/delete":
            cfg = read_chat_cfg()
            base = normalize_api_base(body.get("base") or (cfg.get("api") or {}).get("baseUrl") or "")
            model = (body.get("model") or "").strip()
            if not base or not model:
                return error(self, "base and model required")
            root_base = base[:-3] if base.endswith("/v1") else base
            try:
                req = urllib.request.Request(
                    f"{root_base}/api/delete",
                    json.dumps({"model": model}).encode("utf-8"),
                    {"Content-Type": "application/json"}, method="DELETE")
                with urllib.request.urlopen(req, timeout=30) as res:
                    res.read()
            except Exception as exc:  # noqa: BLE001
                return error(self, f"delete failed: {exc}", 502)
            return json_response(self, {"ok": True})

        if path == "/api/session/save":
            root = os.path.abspath(body.get("root") or "")
            state = read_state()
            sessions = state.setdefault("sessions", {})
            sessions[os.path.normcase(root)] = {
                "tabs": body.get("tabs") or [],
                "active": body.get("active") or "",
                "ts": time.time(),
            }
            state["sessions"] = dict(list(sessions.items())[-50:])
            write_state(state)
            return json_response(self, {"ok": True})

        if path == "/api/file":
            root, rel = body.get("root", ""), body.get("path", "")
            target = safe_join(root, rel)
            content = body.get("content", "")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + f".vanta-tmp-{uuid.uuid4().hex[:6]}"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            saved = False
            for attempt in range(3):
                try:
                    os.replace(tmp, target)
                    saved = True
                    break
                except OSError:
                    time.sleep(0.05)
            if not saved:
                try:
                    with open(target, "w", encoding="utf-8", newline="") as f:
                        f.write(content)
                finally:
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
            PLUGINS.emit("file_saved", {"root": root, "path": rel,
                                        "bytes": len(content or "")})
            return json_response(self, {"ok": True, "mtime": int(os.path.getmtime(target))})

        if path == "/api/fs/create":
            root = body.get("root", "")
            rel = body.get("path", "")
            target = safe_join(root, rel)
            if os.path.exists(target):
                return error(self, "already exists", 409)
            if body.get("isDir"):
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target) or root, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(body.get("content", ""))
            return json_response(self, {"ok": True})

        if path == "/api/fs/rename":
            root = body.get("root", "")
            src = safe_join(root, body.get("from", ""))
            dst = safe_join(root, body.get("to", ""))
            if not os.path.exists(src):
                return error(self, "source missing", 404)
            if os.path.exists(dst):
                return error(self, "target exists", 409)
            os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
            os.rename(src, dst)
            return json_response(self, {"ok": True})

        if path == "/api/fs/delete":
            root = body.get("root", "")
            targets = [safe_join(root, p) for p in body.get("paths", [])]
            if not targets:
                return error(self, "nothing to delete")
            trash = os.path.join(root, ".vanta-trash")
            os.makedirs(trash, exist_ok=True)
            keep = os.path.join(trash, ".gitignore")
            if not os.path.exists(keep):
                try:
                    with open(keep, "w", encoding="utf-8") as f:
                        f.write("# Vanta trash — keep deleted files out of git\n*\n")
                except OSError:
                    pass
            moved = []
            for t in targets:
                if not os.path.exists(t):
                    continue
                dest = os.path.join(trash, f"{int(time.time())}-{uuid.uuid4().hex[:6]}-{os.path.basename(t)}")
                shutil.move(t, dest)
                moved.append(os.path.basename(t))
            PLUGINS.emit("file_deleted", {"root": root, "paths": body.get("paths") or [],
                                          "moved": moved})
            return json_response(self, {"ok": True, "moved": moved, "trash": trash})

        if path == "/api/git/commit":
            root = body.get("root", "")
            msg = (body.get("message") or "").strip()
            if not msg:
                return error(self, "message required")
            files = body.get("files") or []
            if files:
                git(root, ["add", "--"] + files)
            else:
                git(root, ["add", "-A"])
            out = git(root, ["commit", "-m", msg])
            if out is None:
                return error(self, "commit failed — check git output")
            return json_response(self, {"ok": True, "output": out})

        if path == "/api/term/start":
            cwd = body.get("cwd") or os.getcwd()
            if not os.path.isdir(cwd):
                cwd = os.getcwd()
            term = Terminal(cwd)
            with TERMS_LOCK:
                TERMS[term.id] = term
            return json_response(self, {"ok": True, "id": term.id, "cwd": term.cwd,
                                        "cursor": 0})

        if path == "/api/term/input":
            with TERMS_LOCK:
                term = TERMS.get(body.get("id", ""))
            if not term:
                return error(self, "no session", 404)
            try:
                term.send(body.get("data", ""))
            except RuntimeError as exc:
                return error(self, str(exc))
            return json_response(self, {"ok": True})

        if path == "/api/term/interrupt":
            with TERMS_LOCK:
                term = TERMS.get(body.get("id", ""))
            if not term:
                return error(self, "no session", 404)
            term.interrupt()
            return json_response(self, {"ok": True, "cwd": term.cwd, "cursor": term.cursor})

        if path == "/api/term/kill":
            with TERMS_LOCK:
                term = TERMS.pop(body.get("id", ""), None)
            if term:
                term.kill()
            return json_response(self, {"ok": True})

        if path == "/api/ide/open":
            exe = body.get("exe") or ""
            root = body.get("root") or ""
            rel = (body.get("path") or "").strip()
            line = int(body.get("line") or 0)
            if not exe:
                return error(self, "exe required")
            target = os.path.abspath(root)
            if rel:
                target = safe_join(root, rel)
            return json_response(self, interop.open_in(exe, target, line))

        if path == "/api/reveal":
            root, rel = body.get("root", ""), body.get("path", "")
            target = safe_join(root, rel)
            if not os.path.exists(target):
                return error(self, "missing", 404)
            if os.name == "nt":
                subprocess.Popen(["explorer", f"/select,{os.path.normpath(target)}"])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(target)])
            return json_response(self, {"ok": True})

        if path == "/api/run":
            root = body.get("root", "")
            rel = body.get("path", "")
            target = safe_join(root, rel)
            if not os.path.isfile(target):
                return error(self, "missing file", 404)
            cmd = self.run_command(root, target)
            if not cmd:
                return error(self, "no runner for this file type")
            return json_response(self, {"ok": True, "command": cmd})

        if path == "/api/ai/config":
            patch = dict(body.get("config") or {})
            key = patch.pop("apiKey", body.get("apiKey"))
            if isinstance(patch.get("api"), dict):
                patch["api"] = dict(patch["api"])
                if not patch["api"].get("apiKey"):
                    patch["api"].pop("apiKey", None)     # empty means keep the saved key
            if isinstance(key, str) and key.strip():
                patch.setdefault("api", {})["apiKey"] = key.strip()
            if body.get("clearKey"):
                patch.setdefault("api", {})["apiKey"] = ""
            cfg = write_chat_cfg(patch)
            return json_response(self, {"ok": True, **public_cfg(cfg)})

        if path == "/api/ai/load":
            model = (body.get("model") or "").strip()
            if not model:
                return error(self, "model required")
            compression = body.get("compression") or ""
            write_chat_cfg({"provider": "local",
                            "local": {"model": model, "compression": compression}})
            try:
                AI.send({"cmd": "load", "model": model,
                         "compression": compression or None})
            except Exception as exc:  # noqa: BLE001
                return error(self, f"worker: {exc}", 500)
            return json_response(self, {"ok": True, "model": model})

        if path == "/api/ai/chat":
            try:
                result = chat_dispatch(body)
            except Exception as exc:  # noqa: BLE001
                return error(self, f"chat: {exc}", 500)
            return json_response(self, result, 200 if result.get("ok") else 400)

        if path == "/api/ai/reset":
            cfg = read_chat_cfg()
            if (cfg.get("provider") or "local") == "local":
                try:
                    AI.send({"cmd": "reset"})
                except Exception:  # noqa: BLE001
                    pass
            AI.state = "idle"
            AI._emit({"type": "reset"})
            return json_response(self, {"ok": True})

        if path == "/api/admin/elevate":
            if os.name != "nt":
                return error(self, "elevation is only available on Windows")
            pyw = os.path.join(os.path.dirname(HERE), ".venv", "Scripts", "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = "pythonw"
            desk = os.path.join(HERE, "desktop.py")
            root = os.path.dirname(HERE)
            cmd = f"Start-Sleep -Milliseconds 600; Start-Process '{pyw}' -ArgumentList '\"{desk}\"' -WorkingDirectory '{root}'"
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command",
                     f"Start-Process powershell -ArgumentList '-NoProfile -WindowStyle Hidden -Command \"{cmd}\"' -Verb RunAs"],
                    **hidden_kwargs(),
                )
                threading.Timer(0.4, lambda: os._exit(0)).start()
                return json_response(self, {"ok": True, "elevating": True})
            except Exception as exc:  # noqa: BLE001
                return error(self, f"failed to elevate: {exc}", 500)

        return error(self, "unknown endpoint", 404)

    # ------------------------------ domain -------------------------------- #

    def run_command(self, root: str, target: str) -> str | None:
        ext = os.path.splitext(target)[1].lower()
        venv_py = os.path.join(root, ".venv", "Scripts", "python.exe")
        if not os.path.exists(venv_py):
            cand = os.path.join(os.path.dirname(HERE), ".venv", "Scripts", "python.exe")
            if os.path.exists(cand):
                venv_py = cand
        py = f'"{venv_py}"' if os.path.exists(venv_py) else "python"
        if ext in {".py", ".pyw"}:
            return f'Set-Location "{root}"; {py} -u "{target}"'
        if ext in {".js", ".mjs", ".cjs"}:
            return f'Set-Location "{root}"; node "{target}"'
        if ext in {".ts", ".mts", ".cts", ".tsx"}:
            return f'Set-Location "{root}"; node --experimental-strip-types "{target}"'
        if ext == ".ps1":
            return f'Set-Location "{root}"; powershell -NoProfile -File "{target}"'
        if ext in {".bat", ".cmd"}:
            return f'Set-Location "{root}"; cmd /c "{target}"'
        if ext == ".html":
            return f'Start-Process "{target}"'
        if ext in {".sh"}:
            return f'Set-Location "{root}"; bash "{target}"'
        return None

    def git(self, root: str, args: list[str]) -> str | None:
        return git(root, args)

    def git_status(self, root: str) -> dict:
        if not root or not os.path.isdir(root):
            return {"ok": False, "error": "bad root"}
        branch = self.git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if branch is None:
            return {"ok": True, "git": False, "branch": None, "changes": []}
        porcelain = self.git(root, ["status", "--porcelain=v1", "-uall"]) or ""
        changes = []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            code, rel = line[:2].strip(), line[3:].strip()
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            changes.append({"code": code or "?", "path": rel})
        ahead = self.git(root, ["rev-list", "--count", "@{u}..HEAD"])
        return {"ok": True, "git": True, "branch": branch.strip(),
                "ahead": int(ahead.strip()) if (ahead or "").strip().isdigit() else 0,
                "changes": changes}


def build_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind a server. port=0 picks a free port; read server_address[1] after."""
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def shutdown_children():
    with TERMS_LOCK:
        for t in list(TERMS.values()):
            t.kill()
        TERMS.clear()
    try:
        DEBUGGER.stop()
    except Exception:  # noqa: BLE001
        pass
    if AI.proc and AI.proc.poll() is None:
        try:
            AI.proc.terminate()
        except Exception:
            pass


def main():
    global AUTH_TOKEN
    ap = argparse.ArgumentParser(description="Vanta IDE server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--token", default="", help="require this shared secret on /api calls")
    ap.add_argument("--root", default=None, help="open this project on launch")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.token:
        AUTH_TOKEN = args.token

    PLUGINS.version = "1.0.0"
    PLUGINS.load_all(args.root or (read_state().get("lastRoot") or ""))

    if args.root:
        state = read_state()
        root = os.path.abspath(args.root)
        recent = [r for r in state.get("recent", []) if r != root]
        state["recent"], state["lastRoot"] = [root] + recent[:11], root
        write_state(state)

    srv = build_server(args.host, args.port)
    port = srv.server_address[1]
    local_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{local_host}:{port}"
    ts_ip = tailscale_ip()
    def safe_print(msg: str):
        try:
            print(msg)
        except UnicodeEncodeError:
            try:
                print(msg.encode("ascii", "replace").decode("ascii"))
            except Exception:  # noqa: BLE001
                pass
    safe_print(f"Vanta IDE  ->  {url}")
    if ts_ip and args.host in ("0.0.0.0", "", ts_ip):
        safe_print(f"  tailscale: http://{ts_ip}:{port}")
    safe_print(f"  web root : {WEB}")
    safe_print(f"  shell    : {Terminal.SHELL}")
    safe_print("  ctrl+c to stop")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        shutdown_children()
        srv.server_close()


if __name__ == "__main__":
    main()
