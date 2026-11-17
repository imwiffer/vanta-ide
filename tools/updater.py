"""Auto-update for Vanta IDE.

Checks GitHub releases for the repo and, when a newer tag exists, downloads the
release zip into `update-staging/` and writes `apply_update.cmd`. Running the
cmd (spawned detached just before exit) copies the staged files over the app
and restarts the desktop shell.

    updater.check()   -> {current, latest, url, notes, update_available}
    updater.apply()   -> stage + write apply_update.cmd + return {ok, restart}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import zipfile

REPO = "imwiffer/vanta-ide"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def current_version() -> str:
    pkg = os.path.join(HERE, "package.json")
    try:
        with open(pkg, "r", encoding="utf-8") as f:
            return str(json.load(f).get("version") or "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _version_tuple(v: str):
    parts = []
    for p in str(v or "").lstrip("vV ").split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num or 0))
    return tuple(parts[:4] + [0] * (4 - len(parts[:4])))


def check(timeout: float = 8.0) -> dict:
    current = current_version()
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read() or b"{}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "current": current,
                "update_available": False}
    tag = str(data.get("tag_name") or "").lstrip("vV ")
    if not tag:
        return {"ok": False, "error": "no releases published", "current": current,
                "update_available": False}
    assets = data.get("assets") or []
    asset_url = next((a.get("browser_download_url") for a in assets
                      if str(a.get("name", "")).lower().endswith(".zip")), None)
    return {
        "ok": True,
        "current": current,
        "latest": tag,
        "update_available": _version_tuple(tag) > _version_tuple(current),
        "url": asset_url or f"https://github.com/{REPO}/archive/refs/tags/v{tag}.zip",
        "notes": data.get("body") or "",
        "html_url": data.get("html_url") or "",
    }


def _fetch(url: str, dest: str, timeout: float = 120.0) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as res, \
            open(dest, "wb") as f:
        while True:
            chunk = res.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


def apply(info: dict | None = None) -> dict:
    info = info or check(timeout=15)
    if not info.get("update_available"):
        return {"ok": False, "error": "already up to date"}
    staging = os.path.join(HERE, "update-staging")
    os.makedirs(staging, exist_ok=True)
    zpath = os.path.join(staging, "release.zip")
    _fetch(info["url"], zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(staging)
    # the archive wraps everything in a top folder — find it
    inner = staging
    entries = [e for e in os.listdir(staging)
               if e not in ("release.zip",) and os.path.isdir(os.path.join(staging, e))]
    if len(entries) == 1:
        inner = os.path.join(staging, entries[0])
    marker = os.path.join(inner, "server.py")
    if not os.path.exists(marker):
        return {"ok": False, "error": "release archive has no server.py"}
    cmd = os.path.join(HERE, "apply_update.cmd")
    pyw = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else "pythonw"
    script = f'''@echo off
rem Vanta IDE updater — staged at {staging}
timeout /t 2 /nobreak >nul
robocopy "{inner}" "{HERE}" /E /MOVE /NFL /NDL /NJH /NJS >nul
if exist "{os.path.join(staging, 'release.zip')}" del "{os.path.join(staging, 'release.zip')}"
start "Vanta IDE" "{runner}" "{os.path.join(HERE, 'desktop.py')}"
'''
    with open(cmd, "w", encoding="utf-8") as f:
        f.write(script)
    return {"ok": True, "staging": staging, "restart": cmd}


def restart_into_update(delay: float = 0.8) -> dict:
    """Stage now, then exit the server so apply_update.cmd can swap files."""
    cmd = os.path.join(HERE, "apply_update.cmd")
    if not os.path.exists(cmd):
        return {"ok": False, "error": "run apply first"}
    subprocess.Popen(["cmd", "/c", cmd], cwd=HERE,
                     creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0))
    import threading
    threading.Timer(delay, lambda: os._exit(0)).start()
    return {"ok": True}
