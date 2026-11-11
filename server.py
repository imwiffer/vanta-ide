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
import codecs
import json
import mimetypes
import os
import re
import shutil
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

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
STATE_FILE = os.path.join(HERE, ".state.json")
AI_CFG_FILE = os.path.join(HERE, ".ai.json")
DEFAULT_PORT = 7777

sys.path.insert(0, os.path.join(HERE, "tools"))
import agent_tools  # noqa: E402

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
                             text=True, timeout=60, **hidden_kwargs())
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


# --------------------------------------------------------------------------- #
# chat settings + providers (local AirLLM worker | OpenAI-compatible API)
# --------------------------------------------------------------------------- #

CHAT_DEFAULTS = {
    "provider": "api",                       # api (Remote Ollama VPS primary) | local (AirLLM)
    "failsafe": True,                        # auto-fallback to local AirLLM if remote VPS fails
    "mode": "agent",                         # agent | chat | code | explain | edit | review | plan
    "contextFile": True,
    "agentTools": True,
    "temperature": 0.7,
    "maxTokens": 512,
    "local": {"model": "criscar22/Qwen2.5-Coder-7B-Instruct-abliterated",
              "compression": ""},
    "api": {"baseUrl": "http://100.120.101.8:11434/v1", "apiKey": "", "model": "qwen2.5:0.5b"},
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

# Liberated (abliterated/uncensored) checkpoints first — they answer without
# hedging. Sizes are fp16 on disk; the worker auto-selects 4-bit when the
# checkpoint does not fit in free VRAM.
LOCAL_PRESETS = [
    "criscar22/Qwen2.5-Coder-7B-Instruct-abliterated",       # 7.6B · code + chat
    "mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated",       # 8B   · conversation
    "huihui-ai/Llama-3.2-3B-Instruct-abliterated",           # 3.6B · smallest liberated
    "Goekdeniz-Guelmez/Josiefied-Qwen2.5-Coder-14B-Instruct-abliterated-v1",
    "huihui-ai/Qwen3-4B-abliterated",                        # gated: accept the repo on HF first
    "huihui-ai/Qwen3-8B-abliterated",                        # gated: accept the repo on HF first
    "Qwen/Qwen3-0.6B", "Qwen/Qwen3-1.7B", "Qwen/Qwen3-8B",
    "meta-llama/Llama-3.1-8B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3",
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
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
    out["presets"] = LOCAL_PRESETS
    out["peers"] = tailscale_peers()
    return out


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


def _api_stream_once(cfg: dict, messages: list[dict], max_tokens: int, temperature: float) -> str:
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
    req = urllib.request.Request(
        api_url(cfg, "/chat/completions"),
        json.dumps(body).encode("utf-8"),
        {**api_headers(cfg), "Accept": "text/event-stream"},
    )
    parts: list[str] = []
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
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    parts.append(piece)
                    AI._emit({"type": "delta", "text": piece})
    return "".join(parts).strip()


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
             root: str = "", agent_enabled: bool = True, failsafe: bool = True):
    """Stream an OpenAI-compatible / Ollama completion with automatic local AirLLM failsafe."""
    convo = list(messages)
    ws = root if (root and os.path.isdir(root)) else os.getcwd()
    max_rounds = agent_tools.MAX_AGENT_STEPS if agent_enabled else 0
    handed_to_local = False
    try:
        for attempt in range(max_rounds + 1):
            text = _api_stream_once(cfg, convo, max_tokens, temperature)
            calls = agent_tools.parse_tool_calls(text) if attempt < max_rounds else []
            if not calls:
                AI._emit({"type": "reply", "text": text})
                break

            AI._emit({"type": "step", "text": text})
            observations: list[str] = []
            for call in calls:
                obs = agent_tools.execute_tool(
                    call,
                    ws,
                    emit_fn=lambda **kw: AI._emit(kw),
                    on_file_change=lambda p: AI._emit({"type": "file_changed", "path": p}),
                )
                observations.append(obs)

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


def workspace_block(root: str, question: str, active_rel: str = "") -> tuple[str, list[str]]:
    """Tree + the few files a reviewer actually needs, bounded by characters."""
    files = _walk(root, 6000)
    lines = [f"{rel}  ({os.path.getsize(os.path.join(root, rel.replace('/', os.sep))) // 1024 + 1} kB)"
             for rel in files[:300]]
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
        text = _read_capped(full, min(cap, MAX_CTX_CHARS - used))
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
        if used > MAX_CTX_CHARS * 0.55:
            break
        if name in files:
            add(name, 4000)

    candidates = [rel for rel in files
                  if os.path.splitext(rel)[1].lower() in TEXT_EXT and rel not in attached]
    candidates.sort(key=lambda rel: (os.path.basename(rel) not in ENTRY_NAMES,
                                     rel.count("/"), -os.path.getsize(os.path.join(root, rel))))
    for rel in candidates:
        if used > MAX_CTX_CHARS:
            break
        add(rel, PER_FILE_CHARS)
    return "\n\n".join(parts), attached


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
    if agent_enabled and mode != "edit":
        system += agent_tools.agent_system_prompt(model_id, root, is_admin())

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

    question = " ".join((m.get("content") or "") for m in (body.get("messages") or [])
                        if m.get("role") == "user")
    wants_workspace = bool(body.get("workspace")) or mode in ("agent", "review", "plan") or \
        WANTS_WORKSPACE.search(question or "")
    if root and os.path.isdir(root) and wants_workspace:
        block, attached = workspace_block(root, question, path)
        if block:
            sys_blocks.append(block)
            AI._emit({"type": "note",
                      "msg": "context · " + (", ".join(attached) if attached
                                             else "tree only")})

    out: list[dict] = [{"role": "system", "content": "\n\n".join(sys_blocks)}]
    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
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

    if provider == "api":
        merged = _merge(cfg, {
            "failsafe": failsafe,
            "local": {
                "model": body.get("localModel") or cfg["local"].get("model") or LOCAL_PRESETS[0],
                "compression": body.get("compression") or cfg["local"].get("compression") or "",
            },
            "api": {
                "model": body.get("apiModel") or cfg["api"].get("model") or "",
                "baseUrl": normalize_api_base(body.get("apiBaseUrl") or cfg["api"].get("baseUrl") or ""),
            },
        })
        if not merged["api"].get("baseUrl") and not failsafe:
            return {"ok": False, "error": "set a VPS Ollama URL first"}
        AI.state = "generating"
        AI._emit({"type": "state", "msg": "generating"})
        threading.Thread(
            target=api_chat,
            args=(merged, messages, max_tokens, temperature, root,
                  agent_enabled and mode != "edit", failsafe),
            name="vanta-api-chat",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "api",
            "model": merged["api"].get("model") or "auto (vps)",
            "failsafe": failsafe,
            "sent": len(messages),
        }

    model = body.get("localModel") or cfg["local"].get("model") or ""
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



# --------------------------------------------------------------------------- #
# request handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "VantaIDE/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

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
                return self.api_get(path, query)
            return self.static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            error(self, f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
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

        if path == "/api/term/poll":
            with TERMS_LOCK:
                term = TERMS.get(q.get("id", ""))
            if not term:
                return error(self, "no session", 404)
            text, total = term.poll(int(q.get("cursor") or 0))
            return json_response(self, {"ok": True, "output": text, "cursor": total,
                                        "cwd": term.cwd, "alive": term.alive,
                                        "busy": term.busy})

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
            return json_response(self, {"ok": True, "models": LOCAL_PRESETS})

        if path == "/api/hardware":
            info = {"gpu": [], "torch": None}
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                     "--format=csv,noheader"], capture_output=True, text=True, timeout=10,
                    **hidden_kwargs())
                for line in out.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 3:
                        info["gpu"].append({"name": parts[0], "vram": parts[1],
                                            "driver": parts[2]})
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
            return json_response(self, {"ok": True, "root": root,
                                        "name": os.path.basename(root.rstrip("\\/")) or root})

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


def build_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind a server. port=0 picks a free port; read server_address[1] after."""
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


def shutdown_children():
    with TERMS_LOCK:
        for t in list(TERMS.values()):
            t.kill()
        TERMS.clear()
    if AI.proc and AI.proc.poll() is None:
        try:
            AI.proc.terminate()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Vanta IDE server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--root", default=None, help="open this project on launch")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

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
    print(f"Vanta IDE  →  {url}")
    if ts_ip and args.host in ("0.0.0.0", "", ts_ip):
        print(f"  tailscale: http://{ts_ip}:{port}")
    print(f"  web root : {WEB}")
    print(f"  shell    : {Terminal.SHELL}")
    print("  ctrl+c to stop")
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
