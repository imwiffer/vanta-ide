"""Plugin API for Vanta IDE.

Drop `.py` files into `plugins/` (repo) or `<project>/.vanta/plugins/`. Each
module can define:

    def register(api): ...

The `api` object exposes:

    api.version                     -> "1.0.0"
    api.logger                      -> log line helper (goes to desktop log)
    api.state                       -> a dict persisted to .vanta/plugins-state.json
    api.on(event, fn)               -> hook: "project_open" | "file_saved" |
                                       "file_deleted" | "agent_tool" | "chat_message"
    api.route(path, fn, methods)    -> add /api/ext/<path> (fn(body, query) -> dict)
    api.config([default])           -> plugin config from .vanta/plugins.json
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import traceback
import uuid

HOOKS = ("project_open", "file_saved", "file_deleted", "agent_tool", "chat_message")


class PluginAPI:
    def __init__(self, manager: "PluginManager", name: str):
        self._manager = manager
        self.name = name

    @property
    def version(self) -> str:
        return self._manager.version

    @property
    def state(self) -> dict:
        return self._manager.state.setdefault(self.name, {})

    def save_state(self):
        self._manager.save_state()

    def log(self, msg: str):
        self._manager.log(f"[{self.name}] {msg}")

    def on(self, event: str, fn):
        if event not in HOOKS:
            raise ValueError(f"unknown hook {event}")
        self._manager.hooks[event].append((self.name, fn))

    def route(self, path: str, fn, methods=("POST",)):
        if not path or path.startswith("/"):
            raise ValueError("path must be a bare name like 'stats'")
        self._manager.routes[(path, tuple(methods))] = (self.name, fn)

    def config(self, default: dict | None = None) -> dict:
        return self._manager.plugin_config(self.name, default or {})


class PluginManager:
    def __init__(self, here: str, log_fn=None):
        self.here = here
        self.log = log_fn or (lambda m: None)
        self.version = "1.0.0"
        self.plugins: dict[str, dict] = {}
        self.hooks: dict[str, list] = {h: [] for h in HOOKS}
        self.routes: dict[tuple, tuple] = {}
        self.state: dict = {}
        self.lock = threading.Lock()

    # ------------------------------- storage -------------------------------- #

    def _state_file(self) -> str:
        return os.path.join(self.here, ".vanta", "plugins-state.json")

    def _config_file(self, root: str) -> str:
        for base in (root, self.here):
            if base and os.path.isdir(base):
                p = os.path.join(base, ".vanta", "plugins.json")
                if os.path.exists(p):
                    return p
        return os.path.join(self.here, ".vanta", "plugins.json")

    def load_state(self):
        try:
            with open(self._state_file(), "r", encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception:  # noqa: BLE001
            self.state = {}

    def save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_file()), exist_ok=True)
            tmp = self._state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self._state_file())
        except OSError:
            pass

    def plugin_config(self, name: str, default: dict) -> dict:
        try:
            with open(self._config_file(""), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return {**default, **(cfg.get(name) or {})}
        except Exception:  # noqa: BLE001
            return default

    # -------------------------------- loading ------------------------------- #

    def plugin_dirs(self, root: str = "") -> list[str]:
        dirs = []
        if root and os.path.isdir(root):
            dirs.append(os.path.join(root, ".vanta", "plugins"))
        dirs.append(os.path.join(self.here, "plugins"))
        return [d for d in dirs if os.path.isdir(d)]

    def load_all(self, root: str = ""):
        with self.lock:
            self.hooks = {h: [] for h in HOOKS}
            self.routes = {}
            self.plugins = {}
            self.load_state()          # plugins see persisted state at register time
            for d in self.plugin_dirs(root):
                for fn in sorted(os.listdir(d)):
                    if not fn.endswith(".py") or fn.startswith("_"):
                        continue
                    name = fn[:-3]
                    try:
                        spec = importlib.util.spec_from_file_location(
                            f"vanta_plugin_{name}", os.path.join(d, fn))
                        mod = importlib.util.module_from_spec(spec)
                        sys.modules[spec.name] = mod
                        spec.loader.exec_module(mod)
                        register = getattr(mod, "register", None)
                        if not callable(register):
                            continue
                        register(PluginAPI(self, name))
                        self.plugins[name] = {
                            "file": os.path.join(d, fn), "ok": True, "error": "",
                        }
                        self.log(f"plugin loaded: {name}")
                    except Exception as exc:  # noqa: BLE001
                        self.plugins[name] = {
                            "file": os.path.join(d, fn), "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        self.log(f"plugin FAILED: {name}: "
                                 f"{traceback.format_exc(limit=2)}")

    def emit(self, event: str, data: dict) -> None:
        for name, fn in list(self.hooks.get(event) or []):
            try:
                fn(data)
            except Exception as exc:  # noqa: BLE001
                self.log(f"hook {event} of {name} failed: {exc}")

    def dispatch(self, path: str, method: str, body: dict, query: dict) -> dict | None:
        key = (path, tuple(method.upper() if m.upper() == method else m
                           for m in [method]))
        for (rpath, methods), (pname, fn) in self.routes.items():
            if rpath != path or method.upper() not in methods:
                continue
            try:
                result = fn(body or {}, query or {})
                return result if isinstance(result, dict) else {"ok": True,
                                                                "result": result}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{pname}: {exc}"}
        return None

    def status(self) -> list[dict]:
        return [{"name": k, **{kk: vv for kk, vv in v.items()}}
                for k, v in sorted(self.plugins.items())]
