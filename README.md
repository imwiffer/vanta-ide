# Vanta IDE

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](./LICENSE)
[![Platform: Windows 11](https://img.shields.io/badge/Platform-Windows%2011%20WebView2-blue.svg)](https://github.com/imwiffer/vanta-ide)
[![Editor: Monaco](https://img.shields.io/badge/Editor-Monaco%200.57-orange.svg)](https://microsoft.github.io/monaco-editor/)
[![Integration: Agentic OS](https://img.shields.io/badge/Integration-Agentic%20OS-emerald.svg)](https://github.com/imwiffer/agentic-os)

A local-first, single-process IDE that runs in its own window. Python stdlib on
the back end, a bundled Monaco editor on the front, WebView2 hosting the whole
thing as a native app — no browser chrome, no tabs but the editor's own. No
cloud, no accounts, no telemetry; it reads and writes your real files and runs
your real shell.

```
C:\Users\wiff\Documents\Opencode\Projects\vanta-ide
├─ run.cmd                 ← double-click: native window
├─ run.vbs                 ← double-click: no console at all (hidden wscript)
├─ run-admin.cmd           ← run as administrator
├─ run-debug.cmd           ← console + WebView2 devtools
├─ run-web.cmd             ← browser mode at :7777
├─ desktop.py              ← window shell: WebView2 + in-process server
├─ server.py               ← stdlib HTTP + file + git + shell + chat API
├─ assets\vanta.ico        ← app icon (regenerate: tools\make_icon.py)
├─ web\                    ← the UI (index.html, app.js, style.css)
│  └─ vendor\              ← prebuilt Monaco + language workers
├─ tools\ai_worker.py      ← local LLM / AirLLM chat worker (spawned lazily)
├─ tools\agent_tools.py    ← autonomous multi-model agentic tool harness
└─ build.mjs               ← rebuilds web\vendor from node_modules
```

## Launch

```powershell
.\run.cmd                                  # native window, last project
.\run.cmd C:\code\my-project               # native window, that project
.\run-debug.cmd                            # console + WebView2 devtools
.\run-web.cmd                              # browser tab at :7777
```

Make it feel installed — Desktop + Start Menu shortcut with the app icon:

```powershell
powershell -ExecutionPolicy Bypass -File install-shortcut.ps1
powershell -ExecutionPolicy Bypass -File install-shortcut.ps1 -Remove
```

The window owns the server: same process, random-or-7777 port, loopback only.
Close the window and the shell sessions and AirLLM worker die with it. If a
Vanta server is already up on the port, the window attaches to it instead of
starting a second one, and leaves it running on exit.

Shell flags:

| flag | meaning |
|---|---|
| `--root PATH` (or a bare path) | open this folder on launch, and remember it |
| `--port 7778` | preferred port; falls back to a free one if taken |
| `--width/--height` | initial window size |
| `--frameless` | no native frame — no OS resize edges, so rarely worth it |
| `--debug` | WebView2 devtools |
| `--selftest` | hidden window, DOM checks, prints JSON, exits 0/2 |
| `--web` | skip the window, open the default browser |

The last opened folder is restored on the next start (stored in `ide\.state.json`).

If pywebview or the WebView2 runtime is missing, the shell degrades on its own:
Edge `--app` window first (still chromeless), then the default browser. Nothing
to configure.


## What's in it

**Editing** — Monaco, the same engine VS Code uses. Syntax highlighting for
~90 languages, IntelliSense for JS/TS/JSON/CSS/HTML, multi-cursor
(`Alt+Click`), column select (`Shift+Alt+drag`), bracket-pair colorization,
folding, sticky scroll, minimap, find/replace, go-to-definition and references
inside JS/TS projects.

**Files** — lazy tree, create/rename/delete, context menu per node. Deletes go
to `.vanta-trash\` inside the project instead of vanishing. Binary and >4 MB
files are refused with a notice rather than mangled.

**Tabs and breadcrumbs** — multiple open editors, dirty dots, middle-click to
close, breadcrumb path with clickable segments.

**Search** — `Ctrl+Shift+F` scans the tree server-side (case and regex toggles,
include glob) and jumps to the exact line.

**Source control** — branch, per-file status codes, side-by-side diff against
`HEAD` in Monaco's diff editor, commit box (`Ctrl+Enter`). Repos only; no push.

**Terminal** — a persistent PowerShell (`pwsh` when present) behind the panel.
Command history with `↑`/`↓`, `Ctrl+C` interrupts, `■` restarts the shell,
`+` starts a fresh one. Working directory tracks `cd`.

**Run** — `F5` runs the active file: `.py` through the project's `.venv`,
`.js/.mjs` through node, `.ts` through node's type stripping, `.ps1`, `.bat`,
`.sh`, and `.html` through the default browser.

**Quick open / palette** — `Ctrl+P` fuzzy-searches every file in the project;
`Ctrl+Shift+P` lists every command.

**Live reload** — open files are watched. Clean ones reload silently when they
change on disk; dirty ones raise a Reload / Keep-mine banner instead of
clobbering your edits.

**Window behaviour** — native frame with a dark DWM caption, taskbar entry, app
icon. Closing with unsaved tabs pops a confirm instead of dropping your work;
the guard is fed by the page through the bridge, so it never stalls the UI
thread. `F11` fullscreens. "Open Folder" uses the real Windows folder picker
when the app is in its own window.


## Chat dock

A chat panel docks to the right of the terminal (`Ctrl+J`, speech-bubble icon,
or View → Chat Dock). It is part of the workbench, so it stays open while you
edit; drag its left edge to resize, the width is remembered.

**Two providers, same interface:**

| provider | what it talks to | notes |
|---|---|---|
| `local` | `tools/ai_worker.py` in the project's `.venv` — transformers on CUDA, AirLLM as the fallback | no network after the checkpoint is cached |
| `api` | any OpenAI-compatible `/chat/completions` | Ollama, LM Studio, llama.cpp, Groq, OpenAI, OpenRouter, vLLM |

Pick the provider in ⚙, then:

- **local** — type a Hugging Face repo id (preset list provided), leave precision
  on **auto**, press **Load**. First load downloads the checkpoint (1–2× model
  size of disk) — no sharding unless it has to stream.
  Sending a message auto-queues the load if the worker is cold.

  The default is a liberated checkpoint — `criscar22/Qwen2.5-Coder-7B-Instruct-abliterated`,
  a 7.6B coder that holds normal conversation and answers without hedging. Other
  presets in the same list: `mlabonne/Meta-Llama-3.1-8B-Instruct-abliterated`,
  `huihui-ai/Llama-3.2-3B-Instruct-abliterated` (small and fast),
  `Goekdeniz-Guelmez/Josiefied-Qwen2.5-Coder-14B-Instruct-abliterated-v1` (14B in 4-bit).

  The worker picks engine **and precision** per checkpoint, before loading:

  | engine / precision | when | behaviour |
  |---|---|---|
  | `direct` fp16 | weights fit in free VRAM | loaded once, tokens stream through `TextIteratorStreamer` |
  | `direct` 4-bit nf4 | fp16 does not fit, 4-bit does (needs `bitsandbytes`) | same speed class, ~3.5× smaller resident weights |
  | `airllm` | larger than VRAM even at 4-bit | one layer resident at a time, sharded to disk; works past VRAM but pays a per-token streaming tax |

  Precision `auto` is the default; `8bit`/`4bit` in the drawer force it. The choice
  appears as one `engine` line in the chat. Worker diagnostics stay
  out of the conversation; the header shows `writing… Ns` while a reply is pending.

  Measured on the RTX 5070 (11.6 GB free): the 7.6B abliterated coder loads 4-bit
  in ~17 s cold, then answers `yo` in under a second and a linked-list function in
  ~5 s. Nothing sharded, nothing on CPU.
- **api** — set base URL (e.g. `http://127.0.0.1:11434/v1`), key if the endpoint
  wants one, press `⟳` to fetch `/models`, choose one. Keys are stored in
  `ide\.ai.json` on this machine and are never sent back to the page — the UI
  only learns `hasKey`.

**Modes** change the system prompt. Every mode also carries the house rules: no
"Sure", no restating the question, no disclaimers, no closing offer of help, no
refusal templates — a local model talking to the person who owns the machine.

`chat` additionally ships a short worked example of the voice (a greeting answered
with a greeting, a bug report answered with a fix). Rules alone do not beat the
assistant reflex baked into an instruct checkpoint; an example does.

**Workspace context.** Asking about the project (`review`, `audit`, `architecture`,
`files`, `codebase`, `refactor`, `readme`…) or switching to `review`/`plan` makes
the server read the open folder before the model sees a prompt:

1. a file tree — dot-folders, `.venv`, `node_modules`, build output, caches and
   files over 4 MB excluded, 300 entries listed;
2. `README.md` and the manifests (`pyproject.toml`, `package.json`,
   `requirements.txt`, `Cargo.toml`, `go.mod`, …);
3. source files ranked entry points first (`server.py`, `main.py`, `index.js`…),
   then shallow, then large — all inside a 32k character budget.

Anything not included can be pulled by name: `@ide/server.py` in your message.
The chat prints one `context ·` line naming every file that went in, so you can
see what it actually read.

If the model still needs something, it replies `READ: relative/path` (up to three
paths, two rounds). The worker resolves those **inside the workspace root only**,
refuses traversal, binaries and files over 2 MB, and asks the model again with the
contents attached. The refusal is itself visible as a `read refused:` line.

**Out of VRAM** is handled: the worker frees the CUDA cache, retries once at half
the token budget, and if it still fails says which thing to close. Two models
resident at once is the usual cause — one IDE window, one worker.

| mode | behaviour |
|---|---|
| `chat` | conversation first, code when the ask is code; selection only, no full-buffer dump |
| `code` | build the thing: one fenced block, complete and runnable, no stubs |
| `explain` | what it does → how → what breaks it |
| `review` | bullet list: bugs, risks, concrete fixes |
| `plan` | ordered steps, each naming the file it touches |
| `edit` | returns only replacement code; press **Apply** to overwrite the selection |

`temperature`, `max tokens`, and the active-file/selection toggle live in the
same drawer. Temperature drives real sampling (`top_p 0.95`, `top_k 40`,
`repetition_penalty 1.05`) — set it to `0` for greedy decoding. Attached context
is capped (16k chars file, 6k selection) so local models don't turn into
autocomplete.

Status shows in the dock header, the status bar chip, and `window.__vantaState()`.

> Console-flash fix: started from `pythonw.exe` there is no parent console, so
> every child (`git`, `nvidia-smi`, `taskkill`, the AI worker, `explorer`) is
> spawned through `hidden_kwargs()` — `CREATE_NO_WINDOW` plus a hidden
> `STARTUPINFO`. Add new subprocess calls the same way or the flashing returns.
> Prefer `ide\run.vbs` for double-click launches: `wscript` starts `pythonw`
> with a hidden window, so no console is ever allocated.

## Shortcuts

| keys | action |
|---|---|
| `Ctrl+P` | go to file |
| `Ctrl+Shift+P` | command palette |
| `Ctrl+Shift+F` | search in files |
| `Ctrl+Shift+E` | explorer |
| `Ctrl+Shift+G` | source control |
| `Ctrl+B` | toggle sidebar |
| `Ctrl+J` | toggle chat dock |
| `` Ctrl+` `` | toggle terminal panel |
| `Ctrl+S` / `Ctrl+Shift+S` | save / save all |
| `Ctrl+W` | close editor |
| `Ctrl+G` | go to line |
| `F5` | run active file |
| `F12` / `Shift+F12` | definition / references |
| `Shift+Alt+F` | format document |
| `Alt+Z` | word wrap |
| `F11` | fullscreen (window mode) |

## Security

The server listens on loopback only, but it has no authentication: anything that
can reach the port can read/write files and run shell commands as you. Don't
bind `--host 0.0.0.0` on a shared network. There is no sandbox — project root
checks stop path traversal, not intent. The desktop window does not change this;
it just removes the browser from the path.

## Rebuilding the editor bundle

The vendor bundle is committed, so this is only needed after changing Monaco
versions or `web/src/monaco-entry.js`:

```powershell
cd ide
npm install
node build.mjs
```

## Troubleshooting

| symptom | fix |
|---|---|
| nothing appears, no error | read `logs\desktop.log` — the console-less launcher writes everything there |
| window opens but is blank | WebView2 runtime missing (Edge should provide it) — the shell falls back to an Edge `--app` window on its own; check the log line |
| window won't close | you have unsaved tabs; the confirm dialog is in the page, not a native box |
| blank editor area | open the devtools console; usually `vendor/monaco.js` missing — run `node build.mjs` |
| port in use | `--port 7778`, or the window attaches to the running Vanta server automatically |
| terminal prints nothing | shell died — press `■` (restarts in the last cwd) |
| `pwsh` blocked by policy | install PowerShell 7 or let it fall back to `powershell.exe` |
| AI panel stuck on `loading` | first load is downloading/sharding; watch `smoke.log`-style output in the panel's worker messages |
| `torch` reports CPU only | reinstall the CUDA wheel: `pip install --index-url https://download.pytorch.org/whl/cu128 --force-reinstall torch` |

Verify a build without clicking anything:

```powershell
python desktop.py --selftest
```

It creates a hidden window, opens a file, checks Monaco mounted, the tree
rendered, the terminal started, the JS bridge answered, the close guard fired
on a dirty buffer — then prints JSON and exits `0` (or `2` on failure).

---

## Recent Progress & Architecture Milestones

* **Standalone Project Separation:** Decoupled completely from parent workspaces into a first-class repository at [imwiffer/vanta-ide](https://github.com/imwiffer/vanta-ide) with dedicated `.gitignore`, MIT license, standalone launchers, and self-contained logging.
* **Autonomous Multi-Model Agent Harness (`tools/agent_tools.py`):** Real-time tool execution (`read_file`, `write_file`, `edit_file`, `list_dir`, `search_files`, `run_command`) with native adaptive parsers for Qwen (`<tool_call>`), Llama 3 (`<|python_tag|>`), DeepSeek-R1 (with thought stripping), and universal text-based ACTION blocks.
* **Hybrid GPU & AirLLM Engine:** Direct Blackwell RTX 5070 fp16/4-bit acceleration via PyTorch cu128 with automated NVMe disk-layer streaming fallback (AirLLM) for ultra-large models (70B+).
* **Console-Free Native Windowing:** Pure WebView2 runtime integration with zero console flash (`run.vbs`, `run-admin.cmd`), dark DWM captions, and native Windows folder picker bridges.
* **Agentic OS Integration:** Functions as the primary desktop development cockpit and GPU compute bridge for [imwiffer/agentic-os](https://github.com/imwiffer/agentic-os).

---

## License
Released under the [MIT License](./LICENSE).

