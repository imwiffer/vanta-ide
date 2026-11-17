"""IDE interop for Vanta: detect sibling editors, open projects/files in them,
and import VS Code-style user settings, keybindings, and snippets.

Editors are detected from PATH plus well-known install locations.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"creationflags": NO_WINDOW, "startupinfo": si}


def _localapp(*parts: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or ""
    return os.path.join(base, *parts) if base else ""


def _appdata(*parts: str) -> str:
    base = os.environ.get("APPDATA") or ""
    return os.path.join(base, *parts) if base else ""


# ------------------------------ detection ---------------------------------- #

def detect_editors() -> list[dict]:
    """Installed sibling editors that can open a folder or file."""
    out = []

    def add(name, exe, args_prefix=None, path=None):
        out.append({"name": name, "exe": exe, "args": args_prefix or [],
                    "path": path or shutil.which(exe) or ""})

    # VS Code family
    for name, which in (("VS Code", "code"), ("VS Code Insiders", "code-insiders"),
                        ("VSCodium", "codium"), ("Cursor", "cursor")):
        p = shutil.which(which)
        if p:
            add(name, p)

    # well-known install paths that may not be on PATH
    for name, exe, base in (
        ("VS Code", "Code.exe", os.path.join(_localapp("Programs", "Microsoft VS Code"))),
        ("VS Code Insiders", "Code - Insiders.exe",
         os.path.join(_localapp("Programs", "Microsoft VS Code Insiders"))),
        ("VSCodium", "VSCodium.exe", os.path.join(_localapp("Programs", "VSCodium"))),
        ("Cursor", "Cursor.exe", os.path.join(_localapp("Programs", "cursor"))),
        ("Zed", "zed.exe", os.path.join(_localapp("Programs", "Zed"))),
        ("Sublime Text", "subl.exe", os.path.join(os.environ.get("ProgramFiles", ""), "Sublime Text")),
        ("Notepad++", "notepad++.exe", os.path.join(os.environ.get("ProgramFiles", ""), "Notepad++")),
    ):
        if base:
            cand = os.path.join(base, exe)
            if os.path.exists(cand) and not any(e["name"] == name for e in out):
                add(name, cand)

    # JetBrains IDEs via typical install roots
    roots = [
        r"C:\Program Files\JetBrains",
        _localapp("Programs"),
        _localapp("JetBrains", "Toolbox", "apps"),
    ]
    names = ("IntelliJ IDEA", "PyCharm", "WebStorm", "GoLand", "CLion", "DataGrip",
             "PhpStorm", "RubyMine", "Rider")
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            for n in names:
                if entry.lower().startswith(n.lower().split()[0]):
                    for exe in ("bin", "bin\\idea64.exe", "bin\\pycharm64.exe"):
                        pass
                    # find *64.exe in bin
                    bindir = os.path.join(root, entry, "bin")
                    if os.path.isdir(bindir):
                        for exe in glob.glob(os.path.join(bindir, "*64.exe")):
                            label = entry.replace("IDEA", "IDEA").replace(".exe", "")
                            if not any(e["name"] == label for e in out):
                                out.append({"name": label, "exe": exe, "args": [],
                                            "path": exe})
                    break
    seen, uniq = set(), []
    for e in out:
        if e["exe"].lower() not in seen:
            seen.add(e["exe"].lower())
            uniq.append(e)
    return uniq


def open_in(exe: str, target: str, line: int = 0) -> dict:
    """Open a file or folder in an external editor. VS Code family accepts -g file:line."""
    if not os.path.exists(exe):
        return {"ok": False, "error": "editor not found"}
    args = [exe]
    low = os.path.basename(exe).lower()
    if os.path.isfile(target) and line and (low.startswith("code") or "cursor" in low
                                            or "codium" in low):
        args += ["-g", f"{target}:{line}"]
    args.append(target)
    try:
        subprocess.Popen(args, **_hidden_kwargs())
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# --------------------------- vscode config import --------------------------- #

def vscode_user_dirs() -> dict[str, str]:
    return {name: os.path.join(base, "User")
            for name, base in (
                ("vscode", _appdata("Code")),
                ("cursor", _appdata("Cursor")),
                ("vscodium", _appdata("VSCodium")),
                ("code-insiders", _appdata("Code - Insiders")),
            ) if base and os.path.isdir(os.path.join(base, "User"))}


def _read_json_comments(path: str) -> dict:
    """VS Code settings.json allows comments and trailing commas; strip them."""
    try:
        raw = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    import re as _re
    raw = _re.sub(r"//[^\n]*", "", raw)
    raw = _re.sub(r"/\*.*?\*/", "", raw, flags=_re.DOTALL)
    raw = _re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def import_settings() -> dict:
    """Map editor.* user settings onto Vanta editor settings."""
    found = []
    for name, user in vscode_user_dirs().items():
        s = _read_json_comments(os.path.join(user, "settings.json"))
        if not s:
            continue
        found.append({
            "source": name,
            "fontSize": s.get("editor.fontSize"),
            "minimap": s.get("editor.minimap.enabled"),
            "wordWrap": s.get("editor.wordWrap") in ("on", True),
            "theme": s.get("workbench.colorTheme") or "",
            "tabSize": s.get("editor.tabSize"),
        })
    return {"ok": True, "profiles": found}


KEYMAP = {
    "workbench.action.files.save": "save",
    "workbench.action.files.saveAll": "saveall",
    "workbench.action.quickOpen": "quickopen",
    "workbench.action.showCommands": "palette",
    "workbench.action.findInFiles": "search",
    "workbench.view.explorer": "explorer",
    "workbench.view.scm": "scm",
    "workbench.action.toggleSidebarVisibility": "sidebar",
    "workbench.action.terminal.toggleTerminal": "panel",
    "workbench.action.closeActiveEditor": "close",
    "chat.action.focus": "chat",
    "workbench.action.toggleTheme": "theme",
}

COMMAND_NAME = {
    "save": "save", "saveall": "saveall", "quickopen": "quickopen",
    "palette": "palette", "search": "search", "explorer": "explorer",
    "scm": "scm", "sidebar": "sidebar", "panel": "panel", "close": "close",
    "chat": "chat", "theme": "theme",
}


def import_keybindings(here: str) -> dict:
    """Translate VS Code keybindings into Vanta's keyOverrides format."""
    overrides: dict[str, str] = {}
    count = 0
    for _name, user in vscode_user_dirs().items():
        kb = _read_json_comments(os.path.join(user, "keybindings.json"))
        entries = kb.get("keybindings") if isinstance(kb, dict) else kb
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").lower().replace("win+", "ctrl+").replace("cmd+", "ctrl+")
            cmd = KEYMAP.get(str(entry.get("command") or ""))
            if key and cmd:
                overrides[key] = cmd
                count += 1
    if overrides:
        os.makedirs(os.path.join(here, ".vanta"), exist_ok=True)
        with open(os.path.join(here, ".vanta", "keybindings.json"), "w",
                  encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)
    return {"ok": True, "imported": count, "overrides": overrides}


def import_snippets() -> dict:
    """VS Code snippets: snippets/<lang>.json → {lang: [{prefix, body}]}."""
    out: dict[str, list] = {}
    for _name, user in vscode_user_dirs().items():
        snip_dir = os.path.join(user, "snippets")
        if not os.path.isdir(snip_dir):
            continue
        for path in glob.glob(os.path.join(snip_dir, "*.json")):
            lang = os.path.splitext(os.path.basename(path))[0].lower()
            data = _read_json_comments(path)
            if not isinstance(data, dict):
                continue
            items = out.setdefault(lang, [])
            for v in data.values():
                if not isinstance(v, dict):
                    continue
                prefix = v.get("prefix")
                body = v.get("body")
                if isinstance(prefix, str) and isinstance(body, list):
                    items.append({"prefix": prefix,
                                  "body": "\n".join(str(b) for b in body)})
                elif isinstance(prefix, list):
                    for p in prefix:
                        if isinstance(p, str) and isinstance(body, list):
                            items.append({"prefix": p,
                                          "body": "\n".join(str(b) for b in body)})
    total = sum(len(v) for v in out.values())
    return {"ok": True, "languages": out, "total": total}
