"""
Agentic tool execution & model-adaptive tool-call parser for Vanta IDE.

Supports any local or API model across sizes and families:
  * Qwen2.5 / Qwen3 (<tool_call>{"name": ..., "arguments": ...}</tool_call>)
  * Llama 3 / 3.1 / 3.2 (<|python_tag|> or JSON fenced tool calls)
  * DeepSeek-R1 / V3 (strips <think>...</think> before parsing tool calls)
  * Universal ACTION blocks (resilient line-oriented syntax that 3B-70B models
    emit reliably without JSON escaping failures)
  * Legacy READ: path lines

Tools provided:
  * read_file(path, lines)
  * write_file(path, content)
  * edit_file(path, search, replace)
  * list_dir(path)
  * search_files(query, include)
  * run_command(command)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Callable

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".next", ".nuxt", "dist", "build", "out", ".idea", ".vs",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".gradle",
    "target", "bin", "obj", ".tox", "site-packages", ".turbo", ".vanta-trash",
}

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_CHARS = 8000
MAX_AGENT_STEPS = 8

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"creationflags": NO_WINDOW, "startupinfo": si}


def safe_resolve(root: str, rel: str) -> tuple[str, str]:
    """Resolve rel inside root; return (abs_path, normalized_rel). Raises ValueError on escape."""
    root_abs = os.path.abspath(root)
    clean_rel = (rel or "").strip().strip("`'\"").replace("/", os.sep).lstrip("\\/")
    full = os.path.abspath(os.path.join(root_abs, clean_rel))
    a, b = os.path.normcase(root_abs), os.path.normcase(full)
    try:
        inside = os.path.commonpath([a, b]) == a
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"path escapes workspace root: {rel}")
    norm_rel = os.path.relpath(full, root_abs).replace(os.sep, "/")
    return full, norm_rel


# --------------------------------------------------------------------------- #
# model family detection & adaptive system prompt
# --------------------------------------------------------------------------- #

def detect_family(model_id: str) -> str:
    low = (model_id or "").lower()
    if "qwen" in low:
        return "qwen"
    if "deepseek" in low:
        return "deepseek"
    if "llama" in low:
        return "llama"
    if "mistral" in low or "mixtral" in low:
        return "mistral"
    return "generic"


def agent_system_prompt(model_id: str = "", root: str = "", is_admin: bool = False) -> str:
    family = detect_family(model_id)
    priv = "Administrator (elevated)" if is_admin else "Standard user"
    ws = root or os.getcwd()

    base = (
        f"\n\n## Agentic Capabilities & Tools\n"
        f"Workspace root: `{ws}`\n"
        f"Shell privilege: {priv} (PowerShell on Windows)\n\n"
        "You have direct, autonomous access to the filesystem and shell. When a task "
        "requires inspecting files, editing code, creating files, searching the codebase, "
        "or running shell commands/tests, emit one or more tool calls. Stop generating "
        "immediately after emitting your tool call(s) so the harness can execute them "
        "and return the `[Observation]` to you.\n\n"
        "### Available Tools (Universal Action Format)\n\n"
        "1. **Run a PowerShell command**:\n"
        "```\n"
        "ACTION: run_command\n"
        "COMMAND: <powershell command>\n"
        "```\n\n"
        "2. **Read a file** (optional line range `start-end`):\n"
        "```\n"
        "ACTION: read_file\n"
        "PATH: <relative/path>\n"
        "LINES: 1-120\n"
        "```\n\n"
        "3. **Write / overwrite a file**:\n"
        "```\n"
        "ACTION: write_file\n"
        "PATH: <relative/path>\n"
        "CONTENT:\n"
        "<full file content>\n"
        "END_CONTENT\n"
        "```\n\n"
        "4. **Edit an existing file** (search and replace an exact block):\n"
        "```\n"
        "ACTION: edit_file\n"
        "PATH: <relative/path>\n"
        "SEARCH:\n"
        "<exact existing lines>\n"
        "REPLACE:\n"
        "<replacement lines>\n"
        "END_EDIT\n"
        "```\n\n"
        "5. **List directory contents**:\n"
        "```\n"
        "ACTION: list_dir\n"
        "PATH: <relative/path or .>\n"
        "```\n\n"
        "6. **Search across files**:\n"
        "```\n"
        "ACTION: search_files\n"
        "QUERY: <search pattern>\n"
        "```\n"
    )

    if family == "qwen":
        base += (
            "\nYou may use either the `ACTION:` blocks above OR `<tool_call>{\"name\": \"...\", "
            "\"arguments\": {...}}</tool_call>` JSON tags. Prefer `ACTION:` blocks for multiline "
            "file edits to avoid JSON escaping issues."
        )
    elif family == "deepseek":
        base += (
            "\nIf you use `<think>...</think>`, place all `ACTION:` tool calls OUTSIDE and AFTER "
            "your `</think>` closing tag."
        )

    return base


# --------------------------------------------------------------------------- #
# multi-format tool call parser
# --------------------------------------------------------------------------- #

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
XML_TOOL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL | re.IGNORECASE)
JSON_FENCE_RE = re.compile(r"```(?:tool_call|json_tool)\s*\n(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
READ_LINE_RE = re.compile(r"^\s*`?READ:\s*`?(.+?)`?\s*$", re.IGNORECASE | re.MULTILINE)


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks so internal reasoning doesn't trigger tools."""
    cleaned = THINK_RE.sub("", text or "")
    # If an unclosed <think> tag exists with no </think>, strip from <think> onward
    if "<think>" in cleaned.lower() and "</think>" not in cleaned.lower():
        idx = cleaned.lower().find("<think>")
        cleaned = cleaned[:idx]
    return cleaned


def _parse_action_blocks(text: str) -> list[dict]:
    """Parse line-oriented ACTION: blocks."""
    calls: list[dict] = []
    lines = text.splitlines()
    n = len(lines)
    i = 0

    while i < n:
        line = lines[i].strip()
        # Match ACTION: <name> (optionally inside backticks or bold)
        m = re.match(r"^(?:`|\*\*)*ACTION:\s*([a-zA-Z0-9_]+)(?:`|\*\*)*\s*$", line, re.IGNORECASE)
        if not m:
            i += 1
            continue

        action = m.group(1).lower()
        i += 1

        if action in ("run_command", "run", "shell", "powershell", "cmd"):
            cmd_lines: list[str] = []
            while i < n:
                cur = lines[i]
                cur_s = cur.strip()
                if re.match(r"^(?:`|\*\*)*ACTION:\s*[a-zA-Z0-9_]+", cur_s, re.IGNORECASE):
                    break
                cm = re.match(r"^COMMAND:\s*(.*)$", cur_s, re.IGNORECASE)
                if cm:
                    first = cm.group(1).strip()
                    if first:
                        cmd_lines.append(first)
                    i += 1
                    # Collect subsequent lines until ``` or blank line or END_COMMAND or next ACTION
                    while i < n:
                        nxt = lines[i]
                        nxt_s = nxt.strip()
                        if (nxt_s.startswith("```") or nxt_s == "" or
                                nxt_s.upper() == "END_COMMAND" or
                                re.match(r"^(?:`|\*\*)*ACTION:\s*", nxt_s, re.IGNORECASE)):
                            if nxt_s.upper() == "END_COMMAND":
                                i += 1
                            break
                        cmd_lines.append(nxt)
                        i += 1
                    break
                if cur_s.startswith("```") and cmd_lines:
                    break
                i += 1
            cmd_str = "\n".join(cmd_lines).strip()
            if cmd_str:
                calls.append({"name": "run_command", "args": {"command": cmd_str}})

        elif action in ("read_file", "read"):
            path_val = ""
            lines_val = ""
            while i < n:
                cur_s = lines[i].strip()
                if re.match(r"^(?:`|\*\*)*ACTION:\s*", cur_s, re.IGNORECASE):
                    break
                pm = re.match(r"^PATH:\s*(.+)$", cur_s, re.IGNORECASE)
                lm = re.match(r"^LINES:\s*(.+)$", cur_s, re.IGNORECASE)
                if pm:
                    path_val = pm.group(1).strip().strip("`'\"")
                elif lm:
                    lines_val = lm.group(1).strip()
                elif cur_s.startswith("```") or (path_val and cur_s == ""):
                    i += 1
                    break
                i += 1
            if path_val:
                calls.append({"name": "read_file", "args": {"path": path_val, "lines": lines_val}})

        elif action in ("write_file", "write", "create_file"):
            path_val = ""
            content_lines: list[str] = []
            in_content = False
            while i < n:
                cur = lines[i]
                cur_s = cur.strip()
                if not in_content:
                    if re.match(r"^(?:`|\*\*)*ACTION:\s*", cur_s, re.IGNORECASE):
                        break
                    pm = re.match(r"^PATH:\s*(.+)$", cur_s, re.IGNORECASE)
                    if pm:
                        path_val = pm.group(1).strip().strip("`'\"")
                        i += 1
                        continue
                    cm = re.match(r"^CONTENT:\s*(.*)$", cur_s, re.IGNORECASE)
                    if cm:
                        in_content = True
                        rest = cm.group(1)
                        if rest:
                            content_lines.append(rest)
                        i += 1
                        # Skip optional opening code fence right after CONTENT:
                        if i < n and lines[i].strip().startswith("```"):
                            i += 1
                        continue
                    i += 1
                else:
                    if cur_s.upper() == "END_CONTENT":
                        # If line before END_CONTENT was a closing fence, strip it
                        if content_lines and content_lines[-1].strip() == "```":
                            content_lines.pop()
                        i += 1
                        break
                    if re.match(r"^(?:`|\*\*)*ACTION:\s*[a-zA-Z0-9_]+", cur_s, re.IGNORECASE):
                        if content_lines and content_lines[-1].strip() == "```":
                            content_lines.pop()
                        break
                    content_lines.append(cur)
                    i += 1
            if in_content and content_lines and content_lines[-1].strip() == "```":
                content_lines.pop()
            if path_val:
                calls.append({"name": "write_file", "args": {
                    "path": path_val,
                    "content": "\n".join(content_lines),
                }})

        elif action in ("edit_file", "edit", "patch_file"):
            path_val = ""
            search_lines: list[str] = []
            replace_lines: list[str] = []
            section = None  # "search" | "replace"
            while i < n:
                cur = lines[i]
                cur_s = cur.strip()
                if section is None:
                    if re.match(r"^(?:`|\*\*)*ACTION:\s*", cur_s, re.IGNORECASE):
                        break
                    pm = re.match(r"^PATH:\s*(.+)$", cur_s, re.IGNORECASE)
                    if pm:
                        path_val = pm.group(1).strip().strip("`'\"")
                        i += 1
                        continue
                    if re.match(r"^SEARCH:\s*$", cur_s, re.IGNORECASE):
                        section = "search"
                        i += 1
                        if i < n and lines[i].strip().startswith("```"):
                            i += 1
                        continue
                    i += 1
                elif section == "search":
                    if re.match(r"^REPLACE:\s*$", cur_s, re.IGNORECASE):
                        if search_lines and search_lines[-1].strip() == "```":
                            search_lines.pop()
                        section = "replace"
                        i += 1
                        if i < n and lines[i].strip().startswith("```"):
                            i += 1
                        continue
                    search_lines.append(cur)
                    i += 1
                elif section == "replace":
                    if cur_s.upper() == "END_EDIT":
                        if replace_lines and replace_lines[-1].strip() == "```":
                            replace_lines.pop()
                        i += 1
                        break
                    if re.match(r"^(?:`|\*\*)*ACTION:\s*[a-zA-Z0-9_]+", cur_s, re.IGNORECASE):
                        if replace_lines and replace_lines[-1].strip() == "```":
                            replace_lines.pop()
                        break
                    replace_lines.append(cur)
                    i += 1
            if replace_lines and replace_lines[-1].strip() == "```":
                replace_lines.pop()
            if path_val and search_lines:
                calls.append({"name": "edit_file", "args": {
                    "path": path_val,
                    "search": "\n".join(search_lines),
                    "replace": "\n".join(replace_lines),
                }})

        elif action in ("list_dir", "ls", "dir"):
            path_val = "."
            while i < n:
                cur_s = lines[i].strip()
                if re.match(r"^(?:`|\*\*)*ACTION:\s*", cur_s, re.IGNORECASE):
                    break
                pm = re.match(r"^PATH:\s*(.+)$", cur_s, re.IGNORECASE)
                if pm:
                    path_val = pm.group(1).strip().strip("`'\"") or "."
                    i += 1
                    break
                if cur_s.startswith("```") or cur_s == "":
                    i += 1
                    break
                i += 1
            calls.append({"name": "list_dir", "args": {"path": path_val}})

        elif action in ("search_files", "search", "grep"):
            query_val = ""
            inc_val = ""
            while i < n:
                cur_s = lines[i].strip()
                if re.match(r"^(?:`|\*\*)*ACTION:\s*", cur_s, re.IGNORECASE):
                    break
                qm = re.match(r"^(?:QUERY|PATTERN):\s*(.+)$", cur_s, re.IGNORECASE)
                im = re.match(r"^INCLUDE:\s*(.+)$", cur_s, re.IGNORECASE)
                if qm:
                    query_val = qm.group(1).strip().strip("`'\"")
                elif im:
                    inc_val = im.group(1).strip().strip("`'\"")
                elif cur_s.startswith("```") or (query_val and cur_s == ""):
                    i += 1
                    break
                i += 1
            if query_val:
                calls.append({"name": "search_files", "args": {"query": query_val, "include": inc_val}})

    return calls


def _parse_json_tool_calls(text: str) -> list[dict]:
    """Parse Qwen <tool_call>{...}</tool_call> or ```tool_call {...}``` blocks."""
    calls: list[dict] = []
    for regex in (XML_TOOL_RE, JSON_FENCE_RE):
        for raw in regex.findall(text):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            name = (obj.get("name") or obj.get("action") or obj.get("tool") or "").strip().lower()
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or obj
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if name and isinstance(args, dict):
                calls.append({"name": name, "args": args})
    return calls


def parse_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from model output across all supported formats."""
    visible = strip_thinking(text or "")
    calls = _parse_action_blocks(visible)
    if not calls:
        calls = _parse_json_tool_calls(visible)
    if not calls:
        # Check legacy READ: lines
        for m in READ_LINE_RE.finditer(visible):
            rel = m.group(1).strip().strip("`'\"")
            if rel and rel.lower() not in ("none", "nothing"):
                calls.append({"name": "read_file", "args": {"path": rel}})
    return calls[:6]


# --------------------------------------------------------------------------- #
# tool implementations
# --------------------------------------------------------------------------- #

def tool_read_file(root: str, args: dict) -> str:
    rel = str(args.get("path") or args.get("file") or "").strip()
    if not rel:
        return "Error: PATH is required for read_file."
    try:
        full, norm_rel = safe_resolve(root, rel)
    except ValueError as exc:
        return f"Error: {exc}"
    if not os.path.isfile(full):
        return f"Error: file not found: {norm_rel}"
    if os.path.getsize(full) > MAX_FILE_BYTES:
        return f"Error: file too large ({os.path.getsize(full)} bytes): {norm_rel}"
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.splitlines()
    except OSError as exc:
        return f"Error reading {norm_rel}: {exc}"

    line_spec = str(args.get("lines") or "").strip()
    start, end = 1, min(len(all_lines), 250)
    if line_spec:
        m = re.match(r"^(\d+)(?:\s*-\s*(\d+))?$", line_spec)
        if m:
            start = max(1, int(m.group(1)))
            end = int(m.group(2)) if m.group(2) else start + 150
            end = min(len(all_lines), max(start, end))

    selected = all_lines[start - 1:end]
    numbered = "\n".join(f"{idx}: {line}" for idx, line in enumerate(selected, start))
    if len(numbered) > MAX_OUTPUT_CHARS:
        numbered = numbered[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return (f"File: {norm_rel} (showing lines {start}-{min(end, len(all_lines))} "
            f"of {len(all_lines)})\n```\n{numbered}\n```")


def tool_write_file(root: str, args: dict, on_file_change: Callable[[str], None] | None = None) -> str:
    rel = str(args.get("path") or args.get("file") or "").strip()
    content = str(args.get("content") if args.get("content") is not None else "")
    if not rel:
        return "Error: PATH is required for write_file."
    try:
        full, norm_rel = safe_resolve(root, rel)
    except ValueError as exc:
        return f"Error: {exc}"
    try:
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        tmp = full + f".vanta-tmp-{uuid.uuid4().hex[:6]}"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        saved = False
        for _ in range(3):
            try:
                os.replace(tmp, full)
                saved = True
                break
            except OSError:
                time.sleep(0.05)
        if not saved:
            try:
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        if on_file_change:
            on_file_change(norm_rel)
        lines_count = len(content.splitlines())
        return f"Successfully wrote {norm_rel} ({len(content)} bytes, {lines_count} lines)."
    except OSError as exc:
        return f"Error writing {norm_rel}: {exc}"


def tool_edit_file(root: str, args: dict, on_file_change: Callable[[str], None] | None = None) -> str:
    rel = str(args.get("path") or args.get("file") or "").strip()
    search = str(args.get("search") or args.get("target") or "")
    replace = str(args.get("replace") if args.get("replace") is not None else "")
    if not rel or not search:
        return "Error: PATH and SEARCH are required for edit_file."
    try:
        full, norm_rel = safe_resolve(root, rel)
    except ValueError as exc:
        return f"Error: {exc}"
    if not os.path.isfile(full):
        return f"Error: file not found: {norm_rel}"
    try:
        with open(full, "r", encoding="utf-8", errors="replace", newline="") as f:
            raw = f.read()
    except OSError as exc:
        return f"Error reading {norm_rel}: {exc}"

    eol = "\r\n" if "\r\n" in raw else "\n"
    norm_raw = raw.replace("\r\n", "\n")
    norm_search = search.replace("\r\n", "\n")
    norm_replace = replace.replace("\r\n", "\n")

    if norm_search in norm_raw:
        updated = norm_raw.replace(norm_search, norm_replace, 1)
    else:
        # Try line-trimmed matching in case indentation/trailing spaces differed slightly
        raw_lines = norm_raw.splitlines()
        search_lines = [s.strip() for s in norm_search.strip().splitlines()]
        match_idx = -1
        if search_lines:
            m_len = len(search_lines)
            for idx in range(len(raw_lines) - m_len + 1):
                if [r.strip() for r in raw_lines[idx:idx + m_len]] == search_lines:
                    match_idx = idx
                    break
        if match_idx < 0:
            return f"Error: SEARCH block not found in {norm_rel}. Read the file first to get exact lines."
        new_lines = raw_lines[:match_idx] + norm_replace.splitlines() + raw_lines[match_idx + len(search_lines):]
        updated = "\n".join(new_lines) + ("\n" if norm_raw.endswith("\n") else "")

    final_content = updated.replace("\n", eol) if eol == "\r\n" else updated
    res = tool_write_file(root, {"path": norm_rel, "content": final_content}, on_file_change)
    if res.startswith("Error"):
        return res
    return f"Successfully edited {norm_rel}."


def tool_list_dir(root: str, args: dict) -> str:
    rel = str(args.get("path") or ".").strip()
    try:
        full, norm_rel = safe_resolve(root, rel)
    except ValueError as exc:
        return f"Error: {exc}"
    if not os.path.isdir(full):
        return f"Error: not a directory: {norm_rel}"
    entries: list[str] = []
    try:
        with os.scandir(full) as it:
            items = sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))
            for e in items[:200]:
                if e.is_dir():
                    entries.append(f"[DIR]  {e.name}/")
                else:
                    try:
                        sz = e.stat().st_size
                    except OSError:
                        sz = 0
                    entries.append(f"[FILE] {e.name} ({sz} B)")
    except OSError as exc:
        return f"Error listing {norm_rel}: {exc}"
    return f"Directory `{norm_rel}` ({len(entries)} items):\n" + ("\n".join(entries) or "(empty)")


def tool_search_files(root: str, args: dict) -> str:
    query = str(args.get("query") or args.get("pattern") or "").strip()
    include = str(args.get("include") or "").strip()
    if not query:
        return "Error: QUERY is required for search_files."
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    inc_re = None
    if include:
        try:
            inc_re = re.compile(include.replace(".", r"\.").replace("*", ".*"), re.IGNORECASE)
        except re.error:
            inc_re = None

    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in IGNORED_DIRS and not d.startswith(".")]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if inc_re and not inc_re.search(rel):
                continue
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if "\x00" in line:
                            break
                        if pattern.search(line):
                            hits.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                            if len(hits) >= 80:
                                return f"Search results for `{query}` (capped at 80):\n" + "\n".join(hits)
            except OSError:
                continue
    if not hits:
        return f"No matches found for `{query}`."
    return f"Search results for `{query}` ({len(hits)} hits):\n" + "\n".join(hits)


def tool_run_command(root: str, args: dict, timeout: int = 60) -> str:
    cmd = str(args.get("command") or args.get("cmd") or "").strip()
    if not cmd:
        return "Error: COMMAND is required for run_command."
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    cwd = root if (root and os.path.isdir(root)) else os.getcwd()
    t0 = time.time()
    try:
        proc = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-Command", cmd],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_hidden_kwargs(),
        )
        elapsed = time.time() - t0
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        out = out.strip()
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"
        return (f"Command: `{cmd}`\n"
                f"Exit code: {proc.returncode} ({elapsed:.1f}s)\n"
                f"Output:\n```\n{out or '(no output)'}\n```")
    except subprocess.TimeoutExpired:
        return f"Command `{cmd}` timed out after {timeout}s."
    except Exception as exc:  # noqa: BLE001
        return f"Error running `{cmd}`: {exc}"


def execute_tool(
    call: dict,
    root: str,
    emit_fn: Callable[..., None] | None = None,
    on_file_change: Callable[[str], None] | None = None,
) -> str:
    """Execute a single parsed tool call and emit live notifications."""
    name = (call.get("name") or "").lower()
    args = call.get("args") or {}
    ws = root if (root and os.path.isdir(root)) else os.getcwd()

    detail = str(args.get("path") or args.get("command") or args.get("query") or "")[:90]

    if name in ("read_file", "read"):
        res = tool_read_file(ws, args)
    elif name in ("write_file", "write", "create_file"):
        res = tool_write_file(ws, args, on_file_change)
    elif name in ("edit_file", "edit", "patch_file"):
        res = tool_edit_file(ws, args, on_file_change)
    elif name in ("list_dir", "ls", "dir"):
        res = tool_list_dir(ws, args)
    elif name in ("search_files", "search", "grep"):
        res = tool_search_files(ws, args)
    elif name in ("run_command", "run", "shell", "powershell", "cmd"):
        res = tool_run_command(ws, args)
    else:
        res = f"Unknown tool `{name}`."

    if emit_fn:
        ok = not res.startswith("Error") and not res.startswith("Unknown tool")
        first_line = (res.splitlines() or [""])[0][:100]
        emit_fn(type="tool", name=name, detail=detail, ok=ok, summary=first_line)

    return res
