"""Vanta Console — a terminal client for the Vanta IDE chat engine.

Connects to the local Vanta server (starts one if none is running) and gives
you the full chat dock in your terminal: every provider (api / anthropic /
opencode / local), every mode, agent tool events, streaming replies, and
confirm prompts for sandboxed agent commands.

    python console.py                 # attach to 127.0.0.1:7777 or start one
    python console.py --port 7778     # use a specific server
    python console.py --provider anthropic --mode review

Commands while chatting:

    /help                     list commands
    /provider <name>          api | anthropic | opencode | local
    /profile <name>           pick an apiProfile
    /model <id>               set the provider's model
    /mode <name>              agent | chat | code | explain | edit | review | plan
    /file <path>              attach a workspace file as context
    /clear                    new conversation
    /save <file>              write the transcript to a markdown file
    /exit                     quit
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ACCENT = "\033[96m"
DIM = "\033[2m"
ERR = "\033[91m"
OK = "\033[92m"
WARN = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


class Client:
    def __init__(self, base: str, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token
        self.ai_cursor = 0
        self.state = "idle"

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Vanta-Token"] = self.token
        return h

    def get(self, path, **params):
        url = self.base + path + "?" + "&".join(
            f"{k}={urllib.request.quote(str(v), safe='')}" for k, v in params.items())
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read() or b"{}")

    def post(self, path, body=None, timeout=60):
        req = urllib.request.Request(self.base + path,
                                     json.dumps(body or {}).encode("utf-8"),
                                     self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read() or b"{}")


class Session:
    def __init__(self, cli: Client, args):
        self.cli = cli
        self.history: list[dict] = []
        self.file: str = ""
        self.fileText: str = ""
        self.root = ""
        self.provider = args.provider or ""
        self.mode = args.mode or ""
        self.profile = args.profile or ""
        self.model = args.model or ""
        self.state = "idle"
        self.pending_confirms: dict[str, callable] = {}

    def load_env(self):
        env = self.cli.get("/api/env")
        self.root = env.get("lastRoot") or os.getcwd()
        cfg = self.cli.get("/api/ai/config")
        self.provider = self.provider or cfg.get("provider") or "api"
        self.mode = self.mode or cfg.get("mode") or "chat"
        self.profile = self.profile or cfg.get("profile") or ""
        # skip historical events from other clients (UI sessions, old tests)
        res = self.cli.get("/api/ai/poll", cursor=0)
        self.cli.ai_cursor = res.get("cursor", 0)
        return env, cfg

    def send(self, text: str):
        if self.file and os.path.isfile(self.fileText_path()):
            try:
                self.fileText = open(self.fileText_path(), "r",
                                     encoding="utf-8", errors="replace").read()[:16000]
            except OSError:
                self.fileText = ""
        body = {
            "provider": self.provider, "mode": self.mode, "profile": self.profile,
            "model": self.model, "root": self.root,
            "messages": self.history[-16:] + [{"role": "user", "content": text}],
            "file": self.file, "fileText": self.fileText,
        }
        if self.model:
            if self.provider == "local":
                body["localModel"] = self.model
            else:
                body["apiModel"] = self.model
        res = self.cli.post("/api/ai/chat", body)
        self.history.append({"role": "user", "content": text})
        return res

    def fileText_path(self):
        return os.path.join(self.root, self.file) if self.file else ""

    def poll(self):
        res = self.cli.get("/api/ai/poll", cursor=self.cli.ai_cursor)
        self.cli.ai_cursor = res.get("cursor", self.cli.ai_cursor)
        for ev in res.get("events") or []:
            self.handle_event(ev)
        self.state = res.get("state", self.state)

    def handle_event(self, ev):
        t = ev.get("type")
        if t == "delta":
            sys.stdout.write(ev.get("text") or "")
            sys.stdout.flush()
        elif t == "step":
            text = (ev.get("text") or "").strip()
            print(f"\n{DIM}· agent step{RESET}" + (f": {text[:160]}" if text else ""))
        elif t == "tool":
            mark = OK if ev.get("ok", True) else ERR
            print(f"  {mark}⚙ {ev.get('name')}{RESET} {DIM}{ev.get('detail') or ''}{RESET}")
        elif t == "note":
            print(f"{DIM}· {ev.get('msg')}{RESET}")
        elif t == "error":
            print(f"\n{ERR}error: {ev.get('msg')}{RESET}")
        elif t == "confirm":
            cid = ev.get("id")
            cmd = ev.get("command") or ""
            print(f"\n{WARN}⚠ confirm required{RESET}\n  {cmd}")
            ans = input("  [y]es / [n]o: ").strip().lower()
            self.cli.post("/api/confirm", {"id": cid, "approve": ans in ("y", "yes")})
        elif t == "reply":
            reply = (ev.get("text") or "").strip()
            sys.stdout.write("\n")
            if reply:
                self.history.append({"role": "assistant", "content": reply})
            else:
                print(f"{DIM}(empty reply){RESET}")
        elif t == "file_changed":
            print(f"{DIM}· changed: {ev.get('path')}{RESET}")

    def finish_reply(self, text: str):
        sys.stdout.write("\n")
        reply = (text or "").strip()
        if reply:
            self.history.append({"role": "assistant", "content": reply})
        else:
            print(f"{DIM}(empty reply){RESET}")

    def wait_idle(self, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001
                print(f"{ERR}poll failed: {exc}{RESET}")
                return
            if self.state in ("idle",):
                break
            time.sleep(0.35)
        # drain a final reply if the state flipped right after polling
        try:
            self.poll()
        except Exception:  # noqa: BLE001
            pass


def read_reply_from_events(sess: Session) -> str:
    return ""   # replies stream via delta + arrive as events; nothing to return


def apply_command(sess: Session, line: str) -> bool:
    """Handle /commands. Returns False when the loop should exit."""
    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit", "/q"):
        return False
    if cmd == "/help":
        print(__doc__)
        return True
    if cmd == "/provider":
        valid = ("api", "anthropic", "opencode", "local")
        if arg in valid:
            sess.provider = arg
            print(f"{OK}provider: {arg}{RESET}")
        else:
            print(f"{WARN}providers: {' | '.join(valid)}{RESET}")
        return True
    if cmd == "/profile":
        sess.profile = arg
        print(f"{OK}profile: {arg or '(default)'}{RESET}")
        return True
    if cmd == "/model":
        sess.model = arg
        print(f"{OK}model: {arg or '(provider default)'}{RESET}")
        return True
    if cmd == "/mode":
        sess.mode = arg or "chat"
        print(f"{OK}mode: {sess.mode}{RESET}")
        return True
    if cmd == "/file":
        sess.file = arg
        sess.fileText = ""
        if arg:
            try:
                sess.fileText = open(os.path.join(sess.root, arg.replace("/", os.sep)),
                                     "r", encoding="utf-8", errors="replace").read()[:16000]
                print(f"{OK}attached {arg} ({len(sess.fileText)} chars){RESET}")
            except OSError as exc:
                print(f"{ERR}{exc}{RESET}")
                sess.file = ""
        else:
            print("detached file context")
        return True
    if cmd == "/clear":
        sess.history = []
        sess.cli.post("/api/ai/cancel", {})
        print(f"{OK}conversation cleared{RESET}")
        return True
    if cmd == "/save":
        path = arg or "vanta-chat.md"
        rows = [f"### {m['role']}\n\n{m['content']}\n" for m in sess.history]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows))
        print(f"{OK}saved {len(sess.history)} messages to {path}{RESET}")
        return True
    if cmd == "/root":
        if os.path.isdir(arg):
            sess.cli.post("/api/project/open", {"path": arg})
            sess.root = os.path.abspath(arg)
            print(f"{OK}root: {sess.root}{RESET}")
        else:
            print(f"{ERR}not a directory: {arg}{RESET}")
        return True
    print(f"{WARN}unknown command — /help for the list{RESET}")
    return True


def ensure_server(args) -> tuple[Client, bool]:
    """Attach to a running Vanta server, or start one in-process."""
    host = f"127.0.0.1:{args.port}"
    cli = Client(f"http://{host}")
    try:
        ping = cli.get("/api/ping")
        if ping.get("vanta"):
            return cli, False
    except Exception:
        pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server as core  # noqa: E402
    srv = core.build_server("127.0.0.1", args.port)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, name="vanta-http",
                     daemon=True).start()
    core.PLUGINS.load_all(args.root or core.read_state().get("lastRoot") or "")
    print(f"{DIM}started vanta server on 127.0.0.1:{port}{RESET}")
    return Client(f"http://127.0.0.1:{port}"), True


def main() -> int:
    ap = argparse.ArgumentParser(description="Vanta Console")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--token", default="")
    ap.add_argument("--provider", default="")
    ap.add_argument("--profile", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--mode", default="")
    ap.add_argument("--root", default="")
    args = ap.parse_args()

    cli, started = ensure_server(args)
    if args.token:
        cli.token = args.token
    sess = Session(cli, args)
    env, cfg = sess.load_env()
    if args.root:
        if os.path.isdir(args.root):
            cli.post("/api/project/open", {"path": args.root})
            sess.root = os.path.abspath(args.root)
    print(f"{BOLD}Vanta Console{RESET} — {sess.provider}/{sess.mode}"
          f"{' · profile ' + sess.profile if sess.profile else ''}"
          f" · root {sess.root}\n{DIM}/help for commands, /exit to quit{RESET}")

    try:
        while True:
            try:
                line = input(f"{ACCENT}›{RESET} ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                if not apply_command(sess, line):
                    break
                continue
            try:
                res = sess.send(line)
            except urllib.error.HTTPError as exc:
                try:
                    msg = json.loads(exc.read().decode("utf-8", "replace")).get("error")
                except Exception:  # noqa: BLE001
                    msg = str(exc)
                print(f"{ERR}chat: {msg}{RESET}")
                continue
            if not res.get("ok"):
                print(f"{ERR}chat: {res.get('error')}{RESET}")
                continue
            sess.wait_idle()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        try:
            cli.post("/api/ai/cancel", {})
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
