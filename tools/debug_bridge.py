"""DAP client bridge for Vanta IDE.

Spawns a debugpy adapter listening on a loopback port, speaks the Debug Adapter
Protocol over that socket, and exposes a small polling API to the IDE:

    DEBUGGER.start(root, script, args)     # launch, wait ready
    DEBUGGER.set_breakpoints(path, lines)
    DEBUGGER.cmd("continue"|"next"|"stepIn"|"stepOut"|"pause"|"stop")
    DEBUGGER.since(cursor)                 # {events, seq}
    DEBUGGER.stack()                       # {frames}
    DEBUGGER.variables(frame=|ref=)        # {scopes}|{variables}
    DEBUGGER.evaluate(expr, frame_id)

One debug session at a time; starting a new one ends the old.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys_dir = os.path.dirname(HERE)
import sys  # noqa: E402

sys.path.insert(0, os.path.join(HERE))

import agent_tools  # noqa: E402,F401  (shared hidden_kwargs)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"creationflags": NO_WINDOW, "startupinfo": si}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Debugger:
    def __init__(self):
        self.sock: socket.socket | None = None
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.state = "idle"          # idle | starting | running | stopped | terminated
        self.root = ""
        self.script = ""
        self.events: deque[dict] = deque(maxlen=4000)
        self.seq = 0
        self.lock = threading.Lock()
        self.responses: dict[int, dict] = {}
        self.resp_cond = threading.Condition()
        self._req_seq = 0
        self.pending_bps: dict[str, list[int]] = {}
        self.threads_cache: list[dict] = []
        self._reader: threading.Thread | None = None
        self.initialized = threading.Event()
        self._initialized_evt = False

    # ------------------------------ transport ------------------------------ #

    def _read_message(self, f):
        headers = {}
        while True:
            line = f.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode().strip().lower()] = v.decode().strip()
        length = int(headers.get("content-length") or 0)
        if not length:
            return None
        return json.loads(f.read(length).decode("utf-8", "replace"))

    def _send(self, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.sock.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        except OSError as exc:
            self.state = "terminated"
            self._emit({"type": "error", "msg": f"debug pipe broken: {exc}"})

    def _request(self, command: str, args: dict | None = None,
                 timeout: float = 10.0) -> dict:
        if not self.sock:
            raise RuntimeError("no debug session")
        with self.lock:
            self._req_seq += 1
            rid = self._req_seq
        self._send({"seq": rid, "type": "request", "command": command,
                    "arguments": args or {}})
        deadline = time.time() + timeout
        with self.resp_cond:
            while rid not in self.responses:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"debug {command} timed out")
                self.resp_cond.wait(remaining)
            resp = self.responses.pop(rid)
        if not resp.get("success", True):
            raise RuntimeError(resp.get("message") or f"{command} failed")
        return resp.get("body") or {}

    def _reader_loop(self):
        f = self.sock.makefile("rb")
        try:
            while True:
                msg = self._read_message(f)
                if msg is None:
                    break
                kind = msg.get("type")
                if kind == "response":
                    with self.resp_cond:
                        self.responses[msg.get("request_seq")] = msg
                        self.resp_cond.notify_all()
                elif kind == "event":
                    self._on_event(msg)
        except Exception as exc:  # noqa: BLE001
            self._emit({"type": "error", "msg": f"debug reader stopped: {exc}"})
        self.state = "terminated"
        self._emit({"type": "session", "state": "terminated"})

    def _emit(self, obj: dict):
        with self.lock:
            self.seq += 1
            obj["seq"] = self.seq
            self.events.append(obj)

    def _on_event(self, msg: dict):
        ev = msg.get("event")
        body = msg.get("body") or {}
        if ev == "output":
            self._emit({"type": "output", "text": body.get("output", ""),
                        "category": body.get("category") or "stdout"})
        elif ev == "stopped":
            self.state = "stopped"
            self._emit({"type": "stopped", "reason": body.get("reason") or "breakpoint",
                        "threadId": body.get("threadId")})
        elif ev == "continued":
            self.state = "running"
            self._emit({"type": "continued"})
        elif ev == "thread":
            self._emit({"type": "thread", "threadId": body.get("threadId")})
        elif ev == "process":
            self._emit({"type": "process", "name": body.get("name")})
        elif ev == "terminated":
            self.state = "terminated"
            self._emit({"type": "terminated"})
            self.initialized.set()   # unblock a launch that ends before config
        elif ev == "exited":
            self._emit({"type": "exited", "code": body.get("exitCode")})
        elif ev == "initialized":
            self.initialized.set()
        elif ev == "breakpoint":
            self._emit({"type": "breakpoint_event",
                        "bp": body.get("breakpoint") or {}})

    # ------------------------------ lifecycle ------------------------------ #

    def start(self, root: str, script: str, args: list[str] | None = None,
              just_my_code: bool = False) -> dict:
        self.stop()
        py = os.path.join(root, ".venv", "Scripts", "python.exe")
        if not os.path.exists(py):
            cand = os.path.join(os.path.dirname(HERE), ".venv", "Scripts", "python.exe")
            py = cand if os.path.exists(cand) else sys.executable
        self.port = _free_port()
        self.root, self.script = root, script
        pending = dict(self.pending_bps)
        self.pending_bps = {}
        self.state = "starting"
        self._emit({"type": "session", "state": "starting"})
        try:
            self.proc = subprocess.Popen(
                [py, "-m", "debugpy.adapter", "--port", str(self.port)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **_hidden_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            self.state = "idle"
            self._emit({"type": "error", "msg": f"debugpy spawn failed: {exc}"})
            return {"ok": False, "error": str(exc)}
        # wait for the adapter to accept connections
        sock = None
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                break
            except OSError:
                if self.proc.poll() is not None:
                    self.state = "idle"
                    self._emit({"type": "error", "msg": "debugpy exited during startup"})
                    return {"ok": False, "error": "debugpy exited during startup"}
                time.sleep(0.15)
        if sock is None:
            self.stop()
            return {"ok": False, "error": "debugpy did not open a port in time"}
        sock.settimeout(None)          # blocking reads from here on
        self.sock = sock
        self.initialized = threading.Event()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        try:
            self._request("initialize", {
                "clientID": "vanta-ide", "clientName": "Vanta IDE",
                "adapterID": "debugpy", "pathFormat": "path",
                "linesStartAt1": True, "columnsStartAt1": True,
                "supportsVariableType": True, "supportsEvaluateForHovers": True,
            }, timeout=15)
            # launch is async: the adapter answers only after configurationDone
            with self.lock:
                self._req_seq += 1
                self._send({"seq": self._req_seq, "type": "request",
                            "command": "launch",
                            "arguments": {
                                "request": "launch", "type": "python",
                                "program": script, "cwd": root,
                                "args": args or [],
                                "console": "internalConsole",
                                "justMyCode": just_my_code,
                            }})
            if not self.initialized.wait(timeout=30):
                raise TimeoutError("debugger never signalled initialized")
        except Exception as exc:  # noqa: BLE001
            self._emit({"type": "error", "msg": f"debug launch failed: {exc}"})
            self.stop()
            return {"ok": False, "error": str(exc)}
        # push any breakpoints the UI already had
        for path, lines in list(pending.items()):
            self._push_breakpoints(path, lines)
        self.pending_bps = {}
        try:
            self._request("configurationDone", timeout=15)
        except Exception as exc:  # noqa: BLE001
            self._emit({"type": "error", "msg": f"configurationDone failed: {exc}"})
        self.state = "running"
        self._emit({"type": "session", "state": "running"})
        return {"ok": True, "port": self.port, "pid": self.proc.pid}

    def _push_breakpoints(self, path: str, lines: list[int]) -> None:
        try:
            res = self._request("setBreakpoints", {
                "source": {"path": os.path.abspath(os.path.join(self.root, path))},
                "breakpoints": [{"line": l} for l in lines],
                "lines": lines,
            }, timeout=10)
            bps = (res.get("body") or {}).get("breakpoints") or []
            self._emit({"type": "note", "msg": "breakpoints · "
                        + ", ".join(f"{b.get('line')}:{'ok' if b.get('verified') else 'unverified'}"
                                    for b in bps)})
        except Exception as exc:  # noqa: BLE001
            self._emit({"type": "error", "msg": f"setBreakpoints failed: {exc}"})

    def set_breakpoints(self, path: str, lines: list[int]) -> dict:
        if not self.sock or self.state in ("idle", "terminated"):
            self.pending_bps[path] = lines
            return {"ok": True, "deferred": True}
        self._push_breakpoints(path, lines)
        return {"ok": True}

    def cmd(self, command: str) -> dict:
        mapping = {
            "continue": "continue", "next": "next", "stepIn": "stepIn",
            "stepOut": "stepOut", "pause": "pause",
        }
        if command == "stop":
            return self.stop()
        dap = mapping.get(command)
        if not dap:
            return {"ok": False, "error": f"unknown command {command}"}
        args = {}
        if dap != "pause":
            tid = self._current_thread()
            if tid:
                args["threadId"] = tid
        try:
            self._request(dap, args, timeout=10)
            self.state = "running"
            self._emit({"type": "continued"})
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def _current_thread(self) -> int | None:
        try:
            threads = self._request("threads", {}, timeout=5).get("threads") or []
        except Exception:  # noqa: BLE001
            return None
        self.threads_cache = threads
        return threads[0]["id"] if threads else None

    def stack(self) -> dict:
        if self.state != "stopped":
            return {"ok": True, "frames": []}
        tid = self._current_thread()
        if tid is None:
            return {"ok": True, "frames": []}
        try:
            res = self._request("stackTrace",
                                {"threadId": tid, "startFrame": 0, "levels": 20},
                                timeout=10)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        frames = [{
            "id": f.get("id"),
            "name": f.get("name") or "?",
            "line": f.get("line"),
            "file": (f.get("source") or {}).get("path") or "",
        } for f in res.get("stackFrames") or []]
        return {"ok": True, "frames": frames}

    def variables(self, frame: int | None = None, ref: int | None = None) -> dict:
        try:
            if ref is not None:
                res = self._request("variables", {"variablesReference": ref},
                                    timeout=10)
                return {"ok": True,
                        "variables": res.get("variables") or []}
            if frame is None:
                return {"ok": False, "error": "frame or ref required"}
            scopes = self._request("scopes", {"frameId": frame}, timeout=10)
            out = []
            for s in scopes.get("scopes") or []:
                out.append({"name": s.get("name"),
                            "variablesReference": s.get("variablesReference"),
                            "expensive": s.get("expensive", False)})
            return {"ok": True, "scopes": out}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def evaluate(self, expr: str, frame_id: int | None = None) -> dict:
        try:
            res = self._request("evaluate",
                                {"expression": expr, "frameId": frame_id,
                                 "context": "repl"}, timeout=15)
            return {"ok": True, "result": res.get("result"),
                    "type": res.get("type")}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def since(self, cursor: int) -> tuple[list[dict], int]:
        with self.lock:
            events = [e for e in self.events if e["seq"] > cursor]
        return events, self.seq

    def stop(self) -> dict:
        if self.sock:
            try:
                self._request("disconnect",
                              {"terminateDebuggee": True}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        was = self.state
        self.proc = None
        self.state = "idle"
        if was != "idle":
            self._emit({"type": "session", "state": "idle"})
        return {"ok": True}


DEBUGGER = Debugger()
