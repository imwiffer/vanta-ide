"""Remote workspaces for Vanta IDE: WSL distros and SSH hosts.

WSL is treated as local-through-a-bridge: distro files are reachable via
`\\\\wsl$\\<distro>\\...`, so the regular file API already works — this module
adds discovery and terminal launch. SSH gets an exec bridge plus base64
read/write so the IDE can browse, open, and edit remote files.

Config for hosts: `.vanta\\remotes.json` (repo or project):

    { "ssh": [ {"name": "vps", "host": "user@1.2.3.4", "port": 22, "key": ""} ],
      "wsl": true }
"""

from __future__ import annotations

import base64
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


def _run(cmd: list[str], timeout: float = 20.0, cwd: str | None = None):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=cwd,
                          **_hidden_kwargs())


# ---------------------------------- WSL ------------------------------------- #

def wsl_distros() -> list[str]:
    if not os.name == "nt" or shutil.which("wsl") is None:
        return []
    try:
        out = _run(["wsl", "-l", "-q"], timeout=10)
        if out.returncode != 0:
            return []
        # wsl -l -q emits UTF-16 on some systems
        text = out.stdout
        if "\x00" in text:
            text = text.replace("\x00", "")
        return [line.strip() for line in text.splitlines() if line.strip()
                and line.strip().lower() not in ("name", "docker-desktop")]
    except Exception:  # noqa: BLE001
        return []


def wsl_root(distro: str) -> str:
    return f"\\\\wsl$\\{distro}"


def wsl_home(distro: str) -> str:
    try:
        out = _run(["wsl", "-d", distro, "--", "sh", "-lc", "echo $HOME"],
                   timeout=10)
        home = out.stdout.strip()
        if out.returncode == 0 and home:
            return f"\\\\wsl$\\{distro}{home.replace('/', os.sep)}"
    except Exception:  # noqa: BLE001
        pass
    return wsl_root(distro)


# ---------------------------------- SSH ------------------------------------- #

def remotes_config_path(root: str = "", here: str = "") -> str:
    for base in (root, here):
        if base and os.path.isdir(base):
            p = os.path.join(base, ".vanta", "remotes.json")
            if os.path.exists(p):
                return p
    return os.path.join(here or root, ".vanta", "remotes.json")


def load_remotes(root: str = "", here: str = "") -> list[dict]:
    try:
        with open(remotes_config_path(root, here), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("ssh") or []
    except Exception:  # noqa: BLE001
        return []


def _ssh_args(host_cfg: dict) -> list[str]:
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(host_cfg.get("port") or 22)]
    if host_cfg.get("key"):
        args += ["-i", host_cfg["key"]]
    args.append(host_cfg["host"])
    return args


def ssh_exec(host_cfg: dict, command: str, timeout: float = 30.0) -> tuple[int, str, str]:
    if shutil.which("ssh") is None:
        return -1, "", "ssh client not installed"
    try:
        out = _run([*_ssh_args(host_cfg), command], timeout=timeout)
        return out.returncode, out.stdout, out.stderr
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def ssh_list(host_cfg: dict, path: str) -> list[dict]:
    code, out, err = ssh_exec(host_cfg, f"ls -1ap -- {shlex_quote(path)}", timeout=20)
    if code != 0:
        raise RuntimeError(err.strip() or "ssh ls failed")
    entries = []
    for line in out.splitlines():
        if not line or line in ("./", "../"):
            continue
        entries.append({"name": line.rstrip("/"), "isDir": line.endswith("/"),
                        "size": 0})
    return entries


def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def ssh_read(host_cfg: dict, path: str) -> bytes:
    code, out, err = ssh_exec(host_cfg, f"base64 < {shlex_quote(path)}", timeout=60)
    if code != 0:
        raise RuntimeError(err.strip() or "ssh read failed")
    return base64.b64decode(out or "")


def ssh_write(host_cfg: dict, path: str, data: bytes) -> None:
    payload = base64.b64encode(data).decode()
    if shutil.which("ssh") is None:
        raise RuntimeError("ssh client not installed")
    proc = subprocess.run(
        [*_ssh_args(host_cfg), f"cat > {shlex_quote(path)}"],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        timeout=60, **_hidden_kwargs())
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ssh write failed").strip())


def ssh_stat(host_cfg: dict, path: str) -> dict:
    code, out, err = ssh_exec(host_cfg, f"wc -c < {shlex_quote(path)}", timeout=15)
    if code != 0:
        raise RuntimeError(err.strip() or "not found")
    return {"size": int((out or "0").strip() or 0)}
