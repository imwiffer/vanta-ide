/* ==========================================================================
   Vanta IDE — frontend
   ========================================================================== */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const S = {
    root: null,
    name: null,
    env: { cwd: '.', home: '', recent: [], drives: [], git: false, shell: '' },
    tabs: [],
    active: null,
    treeCache: new Map(),
    expanded: new Set(['']),
    selected: null,
    git: { git: false, branch: null, changes: [] },
    gitSig: '',
    term: { id: null, cursor: 0, cwd: '', dead: false, history: [], hIndex: -1 },
    ai: { cursor: 0, state: 'idle', model: null, messages: [], busy: false,
          provider: 'api', failsafe: true, mode: 'agent', lastReply: '' },
    settings: {
      minimap: true,
      wordWrap: false,
      fontSize: 13.5,
      autosave: false,
      sidebar: true,
      panel: true,
      chat: true,
    },
    diffTab: null,
    externalPrompt: null,
    watch: { supported: false, cursor: 0 },
    tasks: [],
    preview: false,
    attachments: [],
    breakpoints: {},
    remotes: null,
    dbg: { cursor: 0, active: false, state: 'idle', frames: [], sel: null, vars: [] },
  };
  let keyOverrides = {};

  const api = {
    headers() {
      const t = window.__vantaToken
        || new URLSearchParams(location.search).get('token') || '';
      return t ? { 'X-Vanta-Token': t } : {};
    },
    async get(path, params) {
      const url = new URL(path, location.origin);
      if (params) for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, v);
      }
      const res = await fetch(url, { headers: api.headers() });
      const data = await res.json().catch(() => ({ ok: false, error: 'bad json' }));
      if (!res.ok || data.ok === false) throw new Error(data.error || `${res.status}`);
      return data;
    },
    async post(path, body) {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...api.headers() },
        body: JSON.stringify(body || {}),
      });
      const data = await res.json().catch(() => ({ ok: false, error: 'bad json' }));
      if (!res.ok || data.ok === false) throw new Error(data.error || `${res.status}`);
      return data;
    },
  };

  /* ---------------------------- desktop shell ----------------------------- */
  const shell = () => (window.pywebview && window.pywebview.api ? window.pywebview.api : null);
  const isDesktop = () => shell() !== null;

  function ncall(name, ...args) {
    const s = shell();
    if (!s || typeof s[name] !== 'function') return Promise.resolve(null);
    try { return Promise.resolve(s[name](...args)); } catch { return Promise.resolve(null); }
  }

  function openExternal(url) {
    const s = shell();
    if (s && s.open_external) ncall('open_external', url);
    else window.open(url, '_blank', 'noopener');
  }

  window.__vantaDirty = () => S.tabs.filter((t) => t.dirty &&
    (t.kind === 'file' || t.kind === 'remote')).length;
  window.__vantaState = () => ({
    root: S.root,
    name: S.name,
    tabs: S.tabs.length,
    dirty: window.__vantaDirty(),
    active: S.active,
    tree: document.querySelectorAll('#tree .node').length,
    term: S.term.id,
    termCwd: S.term.cwd,
    ai: S.ai.state,
    chat: { open: chatOpen(), provider: S.ai.provider, mode: S.ai.mode, model: S.ai.model },
    desktop: isDesktop(),
  });
  window.__vantaAskClose = async () => {
    const n = window.__vantaDirty();
    if (!n) { ncall('confirm_close'); return; }
    const ok = await confirmModal(
      'Unsaved changes',
      `${n} open file${n === 1 ? '' : 's'} still ${n === 1 ? 'has' : 'have'} unsaved changes. Close and lose them?`,
      'Close without saving', true);
    if (ok) ncall('confirm_close');
  };
  window.__vantaOpenPath = (p) => { if (p) openProject(p); };
  window.__vantaOpenFile = (p) => { if (p) openFile(p); };

  let shellSyncTimer = null;
  function syncShell() {
    if (!isDesktop()) return;
    clearTimeout(shellSyncTimer);
    shellSyncTimer = setTimeout(() => {
      ncall('sync_state', {
        dirty: window.__vantaDirty(),
        tabs: S.tabs.length,
        root: S.root,
        name: S.name,
      });
    }, 200);
  }

  /* ------------------------------- helpers ------------------------------- */
  const base = (p) => p.split('/').pop();
  const dirOf = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '');
  const inRoot = (rel) => (S.root ? S.root.replace(/\\/g, '/').replace(/\/$/, '') + '/' + rel : rel);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const sizeStr = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
    : n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B';

  let toastTimer = null;
  function toast(msg, kind = '') {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast ' + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 3200);
  }

  /* -------------------------------- icons -------------------------------- */
  const EXT_ICON = {
    js: '🟨', jsx: '🟨', mjs: '🟨', cjs: '🟨', ts: '🟦', tsx: '🟦', json: '🟧', jsonc: '🟧',
    py: '🐍', pyi: '🐍', rb: '💎', go: '🐹', rs: '🦀', java: '☕', kt: '🟪', swift: '🦅',
    c: '🔵', h: '🔵', cpp: '🔵', cc: '🔵', hpp: '🔵', cs: '🟩', php: '🐘', lua: '🌙',
    html: '🌐', htm: '🌐', css: '🎨', scss: '🎨', sass: '🎨', less: '🎨', vue: '💚', svelte: '🧡',
    md: '📝', markdown: '📝', txt: '📄', log: '📄', pdf: '📕', csv: '📊', tsv: '📊',
    sh: '⚡', bash: '⚡', zsh: '⚡', ps1: '⚡', bat: '⚡', cmd: '⚡', psm1: '⚡',
    yml: '⚙️', yaml: '⚙️', toml: '⚙️', ini: '⚙️', cfg: '⚙️', conf: '⚙️', env: '🔐',
    sql: '🗄️', db: '🗄️', sqlite: '🗄️', png: '🖼️', jpg: '🖼️', jpeg: '🖼️', gif: '🖼️',
    svg: '🖼️', webp: '🖼️', ico: '🖼️', mp3: '🎵', wav: '🎵', mp4: '🎬', mov: '🎬',
    zip: '📦', gz: '📦', '7z': '📦', exe: '🛠️', dll: '🛠️', lock: '🔒', gitignore: '🔧',
    dockerfile: '🐳', ipynb: '📓', pt: '🧠', onnx: '🧠', safetensors: '🧠', h5: '🧠',
  };
  const iconFor = (name, isDir) => {
    if (isDir) return '📁';
    const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : name.toLowerCase();
    return EXT_ICON[ext] || '·';
  };

  /* ------------------------------- languages ------------------------------ */
  const LANG_MAP = {
    js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'typescript', mts: 'typescript', cts: 'typescript',
    json: 'json', jsonc: 'json', json5: 'json',
    py: 'python', pyi: 'python', pyw: 'python',
    rb: 'ruby', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', kts: 'kotlin',
    swift: 'swift', c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp',
    cs: 'csharp', php: 'php', lua: 'lua', pl: 'perl', r: 'r', jl: 'julia',
    html: 'html', htm: 'html', vue: 'html', svelte: 'html',
    css: 'css', scss: 'scss', sass: 'scss', less: 'less',
    md: 'markdown', markdown: 'markdown', rst: 'restructuredtext',
    sh: 'shell', bash: 'shell', zsh: 'shell', ps1: 'powershell', psm1: 'powershell',
    bat: 'bat', cmd: 'bat', sql: 'sql', yml: 'yaml', yaml: 'yaml', toml: 'ini',
    ini: 'ini', cfg: 'ini', conf: 'ini', xml: 'xml', svg: 'xml', graphql: 'graphql',
    gql: 'graphql', proto: 'proto', dockerfile: 'dockerfile', tex: 'latex',
    asm: 'asm', s: 'asm', wat: 'wat', zig: 'zig', nim: 'nim', dart: 'dart',
    ex: 'elixir', exs: 'elixir', erl: 'erlang', hs: 'haskell', clj: 'clojure',
    scala: 'scala', m: 'objective-c', mm: 'objective-c', kts: 'kotlin',
  };
  const langOf = (name) => {
    const low = name.toLowerCase();
    if (low === 'dockerfile') return 'dockerfile';
    if (low === 'makefile') return 'makefile';
    if (low.startsWith('.env')) return 'ini';
    const ext = low.includes('.') ? low.split('.').pop() : low;
    return LANG_MAP[ext] || 'plaintext';
  };

  const MONO_FONT = '"Cascadia Code", "JetBrains Mono", Consolas, ui-monospace, monospace';

  /* --------------------------------- monaco -------------------------------- */
  let editor = null;
  let diffEditor = null;
  let diffModels = null;

  function defineTheme() {
    monaco.editor.defineTheme('vanta', {
      base: 'vs-dark',
      inherit: true,
      rules: [
        { token: 'comment', foreground: '55616e', fontStyle: 'italic' },
        { token: 'keyword', foreground: '7fd6cf' },
        { token: 'keyword.control', foreground: '7fd6cf' },
        { token: 'string', foreground: 'b9d98b' },
        { token: 'number', foreground: 'e8b84b' },
        { token: 'type', foreground: '74c7d6' },
        { token: 'type.identifier', foreground: '74c7d6' },
        { token: 'identifier', foreground: 'cfd8e3' },
        { token: 'function', foreground: '8fd3ff' },
        { token: 'variable', foreground: 'cfd8e3' },
        { token: 'constant', foreground: 'e8b84b' },
        { token: 'operator', foreground: '9fb3c8' },
        { token: 'delimiter', foreground: '7c8b9a' },
        { token: 'tag', foreground: '7fd6cf' },
        { token: 'attribute.name', foreground: '8fd3ff' },
        { token: 'attribute.value', foreground: 'b9d98b' },
      ],
      colors: {
        'editor.background': '#0a0c0f',
        'editor.foreground': '#cfd8e3',
        'editorLineNumber.foreground': '#3a444f',
        'editorLineNumber.activeForeground': '#6d7b89',
        'editor.lineHighlightBackground': '#10151b',
        'editor.selectionBackground': '#1f3b3d',
        'editor.inactiveSelectionBackground': '#16262a',
        'editorCursor.foreground': '#3ddbd0',
        'editorIndentGuide.background1': '#171d26',
        'editorIndentGuide.activeBackground1': '#28323d',
        'editorWhitespace.foreground': '#20272f',
        'editorBracketMatch.background': '#173231',
        'editorBracketMatch.border': '#2d6a66',
        'editorGutter.background': '#0a0c0f',
        'editorWidget.background': '#12171e',
        'editorWidget.border': '#262f3a',
        'editorSuggestWidget.background': '#12171e',
        'editorSuggestWidget.selectedBackground': '#1b2a30',
        'editorHoverWidget.background': '#12171e',
        'editorHoverWidget.border': '#262f3a',
        'minimap.background': '#0a0c0f',
        'scrollbarSlider.background': '#1d2530aa',
        'scrollbarSlider.hoverBackground': '#26313daa',
        'scrollbarSlider.activeBackground': '#33404daa',
        'editorOverviewRuler.border': '#00000000',
        'diffEditor.insertedTextBackground': '#2f6b4c55',
        'diffEditor.removedTextBackground': '#7a333055',
        'tab.activeBorder': '#3ddbd0',
      },
    });

    monaco.editor.defineTheme('vanta-light', {
      base: 'vs',
      inherit: true,
      rules: [
        { token: 'comment', foreground: '6b7280', fontStyle: 'italic' },
        { token: 'string', foreground: '0a7d5a' },
        { token: 'keyword', foreground: '7c3aed' },
        { token: 'number', foreground: 'b45309' },
      ],
      colors: {
        'editor.background': '#f7f8fa',
        'editor.foreground': '#1f2937',
        'editorLineNumber.foreground': '#9aa3af',
        'editorGutter.background': '#f0f2f5',
        'editor.selectionBackground': '#c7d9f7',
        'editor.lineHighlightBackground': '#eef1f5',
        'editorIndentGuide.background1': '#d5dae1',
        'editorWidget.background': '#ffffff',
        'editorSuggestWidget.background': '#ffffff',
        'tab.activeBorder': '#0f9d95',
      },
    });
  }

  function setupEditor() {
    editor = monaco.editor.create($('#editor'), {
      theme: 'vanta',
      automaticLayout: true,
      fontFamily: MONO_FONT,
      fontSize: S.settings.fontSize,
      fontLigatures: true,
      lineHeight: 1.65,
      minimap: { enabled: S.settings.minimap, renderCharacters: false, maxColumn: 90 },
      wordWrap: S.settings.wordWrap ? 'on' : 'off',
      smoothScrolling: true,
      cursorBlinking: 'smooth',
      cursorSmoothCaretAnimation: 'on',
      renderWhitespace: 'selection',
      renderLineHighlight: 'all',
      bracketPairColorization: { enabled: true },
      guides: { bracketPairs: true, indentation: true, highlightActiveIndentation: true },
      stickyScroll: { enabled: true },
      padding: { top: 8, bottom: 40 },
      scrollBeyondLastLine: false,
      tabSize: 4,
      detectIndentation: true,
      multiCursorModifier: 'alt',
      linkedEditing: true,
      formatOnPaste: true,
      suggestOnTriggerCharacters: true,
      quickSuggestions: { other: true, comments: false, strings: false },
      occurrencesHighlight: 'singleFile',
      selectionHighlight: true,
      scrollbar: { verticalScrollbarSize: 10, horizontalScrollbarSize: 10, useShadows: false },
      fixedOverflowWidgets: true,
    });

    editor.onDidChangeCursorPosition((e) => {
      $('#sbCursor').textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`;
    });
    editor.onMouseDown((e) => {
      const t = e.target.type;
      if (t === monaco.MouseTargetType.GUTTER_GUTTER
          || t === monaco.MouseTargetType.GUTTER_LINE_NUMBERS) {
        const tab = activeTab();
        if (tab && tab.kind === 'file' && e.target.position) {
          toggleBreakpoint(tab.path, e.target.position.lineNumber);
        }
      }
    });
    editor.onDidChangeModelLanguage?.(() => {});

    editor.addAction({
      id: 'vanta.saveless',
      label: 'Save File (handled globally)',
      run: () => saveActive(),
    });
    $('#editor').addEventListener('mouseleave', () => {});

    defineTheme();
    registerLspProviders();
    applyTheme();
    editor.updateOptions({ theme: 'vanta' });
  }

  function modelFor(path, content) {
    const uri = monaco.Uri.file(inRoot(path).replace(/\\/g, '/'));
    const old = monaco.editor.getModel(uri);
    if (old) old.dispose();
    const model = monaco.editor.createModel(content, langOf(path), uri);
    model.updateOptions({ tabSize: 4, insertSpaces: true });
    return model;
  }

  /* --------------------------------- tabs --------------------------------- */
  function scheduleSessionSave() {
    clearTimeout(scheduleSessionSave.timer);
    scheduleSessionSave.timer = setTimeout(async () => {
      if (!S.root) return;
      try {
        await api.post('/api/session/save', {
          root: S.root,
          tabs: S.tabs.filter((t) => t.kind === 'file').map((t) => ({ path: t.path })),
          active: S.active,
        });
      } catch { /* best effort */ }
    }, 800);
  }

  function renderTabs() {
    const host = $('#tabs');
    host.innerHTML = '';
    for (const tab of S.tabs) {
      const div = document.createElement('div');
      div.className = 'tab' + (S.active === tab.key ? ' active' : '') + (tab.dirty ? ' dirty' : '');
      div.title = tab.path;
      div.innerHTML = `
        <span class="tico">${tab.kind === 'diff' ? '±' : iconFor(tab.name, false)}</span>
        <span class="tname">${esc(tab.name)}</span>
        <span class="dot"></span>
        <button class="x" title="Close">✕</button>`;
      div.addEventListener('mousedown', (e) => {
        if (e.button === 1) { e.preventDefault(); closeTab(tab.key); return; }
        activateTab(tab.key);
      });
      div.querySelector('.x').addEventListener('click', (e) => {
        e.stopPropagation();
        closeTab(tab.key);
      });
      host.appendChild(div);
    }
    syncShell();
  }

  function renderCrumbs() {
    const host = $('#breadcrumbs');
    host.innerHTML = '';
    const tab = activeTab();
    if (!tab) return;
    const parts = tab.path.split('/');
    const rootSeg = document.createElement('span');
    rootSeg.className = 'seg';
    rootSeg.textContent = S.name || 'root';
    host.appendChild(rootSeg);
    parts.forEach((part, i) => {
      const sep = document.createElement('span');
      sep.className = 'sep';
      sep.textContent = '›';
      host.appendChild(sep);
      const seg = document.createElement('span');
      seg.className = 'seg';
      seg.textContent = part;
      seg.addEventListener('click', () => {
        if (i < parts.length - 1) {
          const dir = parts.slice(0, i + 1).join('/');
          S.expanded.add(dir);
          revealPath(dir);
        }
      });
      host.appendChild(seg);
    });
  }

  function setStatusForTab(tab) {
    const model = tab && tab.model;
    if (!model) return;
    $('#sbLang').textContent = model.getLanguageId();
    const opts = model.getOptions();
    $('#sbIndent').textContent = `Spaces: ${opts.tabSize}`;
    const text = model.getValue();
    $('#sbEol').textContent = text.includes('\r\n') ? 'CRLF' : 'LF';
    const pos = editor.getPosition();
    if (pos) $('#sbCursor').textContent = `Ln ${pos.lineNumber}, Col ${pos.column}`;
  }

  const activeTab = () => S.tabs.find((t) => t.key === S.active) || null;

  function activateTab(key) {
    const tab = S.tabs.find((t) => t.key === key);
    if (!tab) return;
    if (S.active && S.active !== key) {
      const prev = S.tabs.find((t) => t.key === S.active);
      if (prev && prev.viewState) prev.viewState = editor.saveViewState();
    }
    S.active = key;
    $('#welcome').classList.add('hidden');

    if (tab.kind === 'diff') {
      showDiff(tab);
    } else {
      $('#diffHost').classList.add('hidden');
      $('#editor').classList.remove('hidden');
      if (diffModels) { diffModels.original.dispose(); diffModels.modified.dispose(); diffModels = null; }
      editor.setModel(tab.model);
      if (tab.viewState) editor.restoreViewState(tab.viewState);
      editor.focus();
      setStatusForTab(tab);
    }
    S.selected = tab.path;
    renderTabs();
    renderCrumbs();
    renderTree();
    scheduleSessionSave();
    if (tab.kind === 'file') {
      lspSend('open', tab).then(() => lspDiagnostics(tab));
    }
    updatePreview();
  }

  async function openFile(rel, line = null) {
    const existing = S.tabs.find((t) => t.kind === 'file' && t.path === rel);
    if (existing) {
      activateTab(existing.key);
      if (line) gotoLine(existing, line);
      return;
    }
    try {
      const data = await api.get('/api/file', { root: S.root, path: rel });
      if (data.binary) return toast(`${base(rel)} is binary — no editor preview`, 'err');
      if (data.tooLarge) return toast(`${base(rel)} is too large for the editor`, 'err');
      const tab = {
        kind: 'file',
        key: rel + '@file',
        path: rel,
        name: base(rel),
        model: modelFor(rel, data.content),
        mtime: data.mtime,
        dirty: false,
        viewState: null,
      };
      tab.savedContent = data.content;
      tab.model.onDidChangeContent(() => {
        if (!tab.dirty) {
          tab.dirty = true;
          renderTabs();
        }
        if (S.settings.autosave) {
          clearTimeout(tab._auto);
          tab._auto = setTimeout(() => saveTab(tab), 900);
        }
        clearTimeout(tab._lsp);
        tab._lsp = setTimeout(() => {
          lspSend('change', tab, { version: Date.now() }).then(() => lspDiagnostics(tab));
          updatePreview();
        }, 500);
      });
      S.tabs.push(tab);
      activateTab(tab.key);
      if (line) gotoLine(tab, line);
      lspSend('open', tab).then(() => lspDiagnostics(tab));
    } catch (err) {
      toast(`open failed: ${err.message}`, 'err');
    }
  }

  function gotoLine(tab, line, endLine = null) {
    const model = tab.model;
    if (!model) return;
    const end = endLine ? Math.min(endLine, model.getLineCount()) : line;
    const range = new monaco.Range(line, 1, end, model.getLineMaxColumn(end));
    editor.setSelection(range);
    editor.revealRangeInCenterIfOutsideViewport(range, monaco.editor.ScrollType.Smooth);
    editor.focus();
  }

  async function saveTab(tab) {
    if (!tab) return;
    if (tab.kind === 'remote') {
      try {
        await api.post('/api/remotes/write',
          { id: tab.remote.id, root: S.root || '', path: tab.remote.path,
            content: tab.model.getValue() });
        tab.savedContent = tab.model.getValue();
        tab.dirty = false;
        tab.mtime = 0;
        renderTabs();
        toast(`${tab.name} saved to remote`, 'ok');
      } catch (err) {
        toast(err.message, 'err');
      }
      return;
    }
    if (tab.kind !== 'file' || !tab.model) return;    try {
      const content = tab.model.getValue();
      const res = await api.post('/api/file', { root: S.root, path: tab.path, content });
      tab.dirty = false;
      tab.savedContent = content;
      tab.mtime = res.mtime;
      renderTabs();
      toast(`saved ${tab.name}`, 'ok');
    } catch (err) {
      toast(`save failed: ${err.message}`, 'err');
    }
  }

  const saveActive = () => saveTab(activeTab());
  async function saveAll() {
    const dirty = S.tabs.filter((t) => t.dirty && t.kind === 'file');
    for (const t of dirty) await saveTab(t);
    if (!dirty.length) toast('nothing to save');
  }

  async function closeTab(key) {
    const idx = S.tabs.findIndex((t) => t.key === key);
    if (idx < 0) return;
    const tab = S.tabs[idx];
    if (tab.dirty) {
      const ok = await confirmModal('Unsaved changes', `"${tab.name}" has unsaved changes. Close anyway?`, 'Close');
      if (!ok) return;
    }
    S.tabs.splice(idx, 1);
    if (tab.model) tab.model.dispose();
    if (tab.kind === 'diff') {
      if (diffModels) { diffModels.original.dispose(); diffModels.modified.dispose(); diffModels = null; }
      if (diffEditor) { diffEditor.dispose(); diffEditor = null; }
    }
    if (S.active === key) {
      const next = S.tabs[Math.max(0, idx - 1)] || S.tabs[idx];
      if (next) { S.active = null; activateTab(next.key); }
      else {
        S.active = null;
        editor.setModel(null);
        $('#welcome').classList.remove('hidden');
        $('#diffHost').classList.add('hidden');
        $('#editor').classList.remove('hidden');
        renderTabs();
        $('#breadcrumbs').innerHTML = '';
      }
    } else {
      renderTabs();
    }
  }

  /* ---------------------------------- tree --------------------------------- */
  function trustedList() {
    try { return JSON.parse(localStorage.getItem('vanta.trusted') || '[]'); }
    catch { return []; }
  }

  function trustRemember(path) {
    const list = trustedList();
    if (!list.some((r) => r.toLowerCase() === String(path).toLowerCase())) {
      list.push(path);
      try { localStorage.setItem('vanta.trusted', JSON.stringify(list.slice(-50))); } catch { /* */ }
    }
  }

  async function openProject(path) {
    const known = (S.env.recent || []).some((r) =>
      r && r.toLowerCase() === String(path).toLowerCase())
      || trustedList().some((r) => r.toLowerCase() === String(path).toLowerCase());
    if (!known) {
      const ok = await confirmModal(
        'Open folder',
        `First time opening this folder. Vanta reads and writes real files here, `
        + `and the agent can run shell commands with your privileges. Trust it?`,
        'Trust & Open', true);
      if (!ok) return null;
      trustRemember(path);
    }
    try {
      const res = await api.post('/api/project/open', { path });
      S.root = res.root;
      S.name = res.name;
      S.expanded = new Set(['']);
      S.treeCache.clear();
      S.tabs.forEach((t) => t.model && t.model.dispose());
      S.tabs = [];
      S.active = null;
      S.selected = null;
      S.git = { git: false, branch: null, changes: [] };
      S.gitSig = '';
      editor.setModel(null);
      if (diffEditor) { diffEditor.dispose(); diffEditor = null; }
      if (diffModels) {
        if (diffModels.original) diffModels.original.dispose();
        if (diffModels.modified) diffModels.modified.dispose();
        diffModels = null;
      }
      $('#diffHost').classList.add('hidden');
      $('#editor').classList.remove('hidden');
      $('#fileBanner').classList.add('hidden');
      $('#welcome').classList.add('hidden');
      $('#rootRow').classList.remove('hidden');
      $('#rootRow').innerHTML = `<span>${esc(S.name)}</span>`;
      $('#treeEmpty').classList.add('hidden');
      $('#projectChip').textContent = S.name;
      $('#sbProject').textContent = S.name;
      document.title = `${S.name} — Vanta`;
      ncall('set_title', `Vanta IDE — ${S.name}`);
      S.watch = { supported: false, cursor: 0 };
      api.post('/api/watch/start', { root: S.root }).then((r) => {
        S.watch.supported = !!r.supported;
        S.watch.cursor = 0;
      }).catch(() => {});
      await refreshTree();
      refreshGit();
      renderRecent();
      S.fileList = null;
      if (S.term.id && S.term.cwd && S.term.cwd.toLowerCase() !== S.root.toLowerCase()) {
        sendCmdToTerm(`Set-Location "${S.root}"`);
      }
      toast(`opened ${res.root}`, 'ok');
      syncShell();
    } catch (err) {
      toast(`open folder failed: ${err.message}`, 'err');
    }
  }

  async function listDir(rel) {
    if (S.treeCache.has(rel)) return S.treeCache.get(rel);
    const data = await api.get('/api/list', { root: S.root, path: rel });
    S.treeCache.set(rel, data.entries);
    return data.entries;
  }

  async function refreshTree(rel = '') {
    if (!S.root) return;
    S.treeCache.delete(rel);
    if (rel === '') S.treeCache.clear();
    await renderTree();
  }

  async function renderTree() {
    const host = $('#tree');
    if (!S.root) return;
    const token = (renderTree.token = (renderTree.token || 0) + 1);
    const scroll = host.scrollTop;
    host.innerHTML = '';
    try {
      await renderDirNode('', host, 0, token);
    } catch (err) {
      if (token === renderTree.token) host.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
    }
    host.scrollTop = scroll;
  }

  async function renderDirNode(rel, host, depth, token = 0) {
    const entries = await listDir(rel);
    if (token && token !== renderTree.token) return;
    const CHUNK = 400;
    const makeRow = (entry) => {
      const row = document.createElement('div');
      const isOpen = S.expanded.has(entry.path);
      row.className = 'node' + (entry.isDir ? ' dir' : '') + (S.selected === entry.path ? ' selected' : '');
      row.style.paddingLeft = `${8 + depth * 12}px`;
      const change = S.git.changes.find((c) => c.path === entry.path);
      row.innerHTML = `
        <span class="chev ${isOpen ? 'open' : ''}">${entry.isDir ? '▶' : ''}</span>
        <span class="ico">${iconFor(entry.name, entry.isDir)}</span>
        <span class="nm">${esc(entry.name)}</span>
        ${change ? `<span class="git-mark gm-${esc(change.code[0])}" title="${esc(change.code)}">${esc(change.code[0])}</span>` : ''}`;
      row.dataset.path = entry.path;
      row.dataset.dir = entry.isDir ? '1' : '0';

      row.addEventListener('click', async (e) => {
        e.stopPropagation();
        S.selected = entry.path;
        if (entry.isDir) {
          if (S.expanded.has(entry.path)) S.expanded.delete(entry.path);
          else S.expanded.add(entry.path);
          await renderTree();
        } else {
          await openFile(entry.path);
        }
      });
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        e.stopPropagation();
        S.selected = entry.path;
        renderTree();
        treeContextMenu(e.clientX, e.clientY, entry);
      });
      host.appendChild(row);

      if (entry.isDir && isOpen) {
        const childHost = document.createElement('div');
        childHost.className = 'children open';
        host.appendChild(childHost);
        renderDirNode(entry.path, childHost, depth + 1, token)
          .catch((err) => {
            childHost.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
          });
      }
    };

    // windowed rendering: huge directories stream in chunks of 400 rows
    for (let i = 0; i < entries.length; i += CHUNK) {
      if (token && token !== renderTree.token) return;
      entries.slice(i, i + CHUNK).forEach(makeRow);
      if (i + CHUNK < entries.length) {
        const more = document.createElement('div');
        more.className = 'node more';
        more.style.paddingLeft = `${8 + depth * 12}px`;
        const shown = Math.min(i + CHUNK, entries.length);
        more.innerHTML = `<span class="nm meta">▾ ${esc(String(entries.length - shown))} more…</span>`;
        more.addEventListener('click', (e) => {
          e.stopPropagation();
          const next = entries.slice(i + CHUNK, i + 2 * CHUNK);
          next.forEach(makeRow);
          if (i + 2 * CHUNK < entries.length) {
            more.remove();
            host.appendChild(more);
            more.querySelector('.nm').textContent = `▾ ${entries.length - i - 2 * CHUNK} more…`;
          } else more.remove();
        });
        host.appendChild(more);
        return;          // lazy: render the next chunk on demand
      }
    }
  }

  function revealPath(rel) {
    const parts = rel.split('/');
    for (let i = 0; i < parts.length; i++) S.expanded.add(parts.slice(0, i + 1).join('/'));
    renderTree().then(() => {
      const el = $(`.node[data-path="${CSS.escape(rel)}"]`);
      if (el) el.scrollIntoView({ block: 'center' });
    });
  }

  function treeContextMenu(x, y, entry) {
    const items = [
      { label: entry.isDir ? 'Open folder' : 'Open file', run: () => entry.isDir ? (S.expanded.add(entry.path), renderTree()) : openFile(entry.path) },
      { label: 'New file…', run: () => createEntry(entry.isDir ? entry.path : dirOf(entry.path), false) },
      { label: 'New folder…', run: () => createEntry(entry.isDir ? entry.path : dirOf(entry.path), true) },
      'hr',
      { label: 'Rename…', run: () => renameEntry(entry) },
      { label: 'Delete', danger: true, run: () => deleteEntry(entry) },
      'hr',
      { label: 'Copy relative path', run: () => copyText(entry.path) },
      { label: 'Copy absolute path', run: () => copyText(inRoot(entry.path)) },
      { label: 'Reveal in Explorer', run: () => revealInOS(entry.path) },
    ];
    if (!entry.isDir) items.push({ label: 'Run file', run: () => runFile(entry.path) });
    showCtx(x, y, items);
  }

  async function createEntry(dir, isDir) {
    const label = isDir ? 'New folder name' : 'New file name';
    const name = await promptModal(dir ? `Create in ${dir}/` : 'Create at project root', 'untitled' + (isDir ? '' : '.txt'), label);
    if (!name) return;
    const rel = (dir ? dir + '/' : '') + name.replace(/^[\\/]+/, '');
    try {
      await api.post('/api/fs/create', { root: S.root, path: rel, isDir });
      S.expanded.add(dir);
      S.treeCache.delete(dir);
      await refreshTree();
      if (!isDir) await openFile(rel);
      toast(`created ${rel}`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function renameEntry(entry) {
    const next = await promptModal('Rename', entry.name, 'new name');
    if (!next || next === entry.name) return;
    const target = (dirOf(entry.path) ? dirOf(entry.path) + '/' : '') + next;
    try {
      await api.post('/api/fs/rename', { root: S.root, from: entry.path, to: target });
      if (S.selected === entry.path) S.selected = target;
      S.treeCache.clear();
      await refreshTree();
      refreshGit();
      toast(`renamed to ${next}`, 'ok');
      const open = S.tabs.find((t) => t.path === entry.path);
      if (open) { await closeTab(open.key); await openFile(target); }
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function deleteEntry(entry) {
    const ok = await confirmModal(
      'Move to trash',
      `Move "${entry.path}" to .vanta-trash inside the project? Recoverable, but it leaves its place.`,
      'Move to trash');
    if (!ok) return;
    try {
      await api.post('/api/fs/delete', { root: S.root, paths: [entry.path] });
      const open = S.tabs.find((t) => t.path === entry.path);
      if (open) await closeTab(open.key);
      S.treeCache.clear();
      await refreshTree();
      refreshGit();
      toast(`moved ${entry.name} to .vanta-trash`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function revealInOS(rel) {
    try {
      await api.post('/api/reveal', { root: S.root, path: rel });
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('copied', 'ok');
    } catch {
      toast('clipboard blocked', 'err');
    }
  }

  /* --------------------------------- search -------------------------------- */
  function searchOptions() {
    return {
      case: $('#searchCase').classList.contains('on') ? '1' : '0',
      regex: $('#searchRegex').classList.contains('on') ? '1' : '0',
    };
  }

  async function runSearch() {
    const q = $('#searchInput').value.trim();
    if (!S.root) return toast('open a folder first', 'err');
    if (!q) return;
    const opts = searchOptions();
    const include = $('#searchInclude').value.trim();
    $('#searchMeta').textContent = 'searching…';
    const results = $('#searchResults');
    results.innerHTML = '';
    try {
      const data = await api.get('/api/search', {
        root: S.root, q, regex: opts.regex, case: opts.case, include,
      });
      $('#searchMeta').textContent =
        `${data.hits.length}${data.truncated ? '+' : ''} results in ${data.scanned} files`;
      if (!data.hits.length) {
        results.innerHTML = '<div class="side-empty">no matches</div>';
        return;
      }
      let current = null;
      let group = null;
      for (const hit of data.hits) {
        if (hit.path !== current) {
          current = hit.path;
          const head = document.createElement('div');
          head.className = 'res-head';
          head.innerHTML = `<span>${iconFor(base(hit.path), false)} ${esc(base(hit.path))}</span>
            <span class="cnt">${esc(dirOf(hit.path) || '.')}</span>`;
          results.appendChild(head);
          group = document.createElement('div');
          results.appendChild(group);
        }
        const row = document.createElement('div');
        row.className = 'res-hit';
        let raw = hit.text.trim();
        if (raw.length > 320) raw = raw.slice(0, 320) + '…';
        let html;
        try {
          const flags = opts.case === '1' ? 'g' : 'gi';
          const body = opts.regex === '1' ? `(${q})` : `(${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`;
          html = raw.split(new RegExp(body, flags))
            .map((part, i) => (i % 2 ? `<mark>${esc(part)}</mark>` : esc(part)))
            .join('');
        } catch {
          html = esc(raw);
        }
        row.innerHTML = `<span class="ln">${hit.line}</span>${html}`;
        row.title = `${hit.path}:${hit.line}`;
        row.addEventListener('click', () => openFile(hit.path, hit.line));
        group.appendChild(row);
      }
    } catch (err) {
      $('#searchMeta').textContent = err.message;
    }
  }

  async function replaceAll() {
    const q = $('#searchInput').value.trim();
    const replacement = $('#searchReplace').value;
    if (!S.root) return toast('open a folder first', 'err');
    if (!q) return toast('search first', 'err');
    if (q === replacement) return toast('search and replace are identical', 'err');
    const opts = searchOptions();
    const include = $('#searchInclude').value.trim();
    $('#searchMeta').textContent = 'scanning…';
    try {
      const preview = await api.post('/api/search/replace', {
        root: S.root, q, regex: opts.regex === '1', case: opts.case === '1',
        include, replacement, preview: true,
      });
      $('#searchMeta').textContent = `${preview.total} file${preview.total === 1 ? '' : 's'} to change`;
      if (!preview.total) { toast('nothing to replace', 'ok'); return; }
      const shown = preview.files.slice(0, 6).map((f) => `• ${f}`).join('<br/>');
      const more = preview.files.length > 6
        ? `<br/>… and ${preview.total - 6} more` : '';
      const ok = await openModal({
        title: 'Replace All',
        body: `<div>Replace <b>${esc(q)}</b> with <b>${esc(replacement)}</b> in `
          + `<b>${preview.total}</b> file${preview.total === 1 ? '' : 's'}?<br/><br/>`
          + `<div class="meta small">${shown}${more}</div></div>`,
        ok: 'Replace', danger: true,
      });
      if (!ok) return;
      const res = await api.post('/api/search/replace', {
        root: S.root, q, regex: opts.regex === '1', case: opts.case === '1',
        include, replacement,
      });
      const n = res.changed.length;
      $('#searchMeta').textContent = `replaced in ${n} file${n === 1 ? '' : 's'}`;
      toast(`replaced in ${n} file${n === 1 ? '' : 's'}`, 'ok');
      if (res.failed.length) {
        toast(`${res.failed.length} file(s) failed`, 'err');
      }
      S.treeCache.clear();
      renderTree();
      refreshGit();
      runSearch();
    } catch (err) {
      $('#searchMeta').textContent = err.message;
      toast(err.message, 'err');
    }
  }

  /* ---------------------------------- git ---------------------------------- */
  async function refreshGit() {
    if (!S.root) return;
    try {
      const data = await api.get('/api/git/status', { root: S.root });
      const sig = JSON.stringify(data.changes) + '|' + data.branch;
      const changed = sig !== S.gitSig;
      S.gitSig = sig;
      S.git = data;
      const badge = $('#scmBadge');
      if (data.git && data.changes.length) {
        badge.textContent = data.changes.length;
        badge.classList.remove('hidden');
      } else badge.classList.add('hidden');
      $('#gitBranchRow').textContent = data.git
        ? `on ${data.branch} — ${data.changes.length} change${data.changes.length === 1 ? '' : 's'}`
        : 'not a git repository';
      $('#sbBranch').textContent = data.git ? `⑂ ${data.branch}` : '⑂ —';
      const list = $('#gitChanges');
      list.innerHTML = '';
      for (const c of data.changes) {
        const row = document.createElement('div');
        row.className = 'change-row';
        row.innerHTML = `<span class="code">${esc(c.code[0])}</span><span class="p">${esc(c.path)}</span>`;
        row.title = `${c.path} — click for diff`;
        row.addEventListener('click', () => openDiff(c.path));
        list.appendChild(row);
      }
      if (changed) renderTree();
      loadBranches();
    } catch (err) {
      $('#gitBranchRow').textContent = err.message;
      $('#gitActions')?.classList.add('hidden');
    }
  }

  async function openDiff(rel) {
    try {
      const data = await api.get('/api/git/diff', { root: S.root, path: rel });
      const key = rel + '@diff';
      const old = S.tabs.find((t) => t.key === key);
      if (old) S.tabs.splice(S.tabs.indexOf(old), 1);
      const tab = {
        kind: 'diff',
        key,
        path: rel,
        name: base(rel) + ' (HEAD)',
        data,
      };
      S.tabs.push(tab);
      activateTab(key);
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  function showDiff(tab) {
    $('#editor').classList.add('hidden');
    $('#diffHost').classList.remove('hidden');
    if (diffModels) {
      if (diffModels.original) diffModels.original.dispose();
      if (diffModels.modified) diffModels.modified.dispose();
      diffModels = null;
    }
    if (diffEditor) {
      diffEditor.dispose();
      diffEditor = null;
    }
    const uri = (suffix) => monaco.Uri.file(inRoot(tab.path).replace(/\\/g, '/') + suffix);
    const original = monaco.editor.createModel(tab.data.original || '', langOf(tab.path), uri(':head'));
    const modified = monaco.editor.createModel(tab.data.modified || '', langOf(tab.path), uri(':work'));
    diffModels = { original, modified };
    diffEditor = monaco.editor.createDiffEditor($('#diffHost'), {
      theme: 'vanta',
      automaticLayout: true,
      fontFamily: MONO_FONT,
      fontSize: S.settings.fontSize,
      renderSideBySide: true,
      ignoreTrimWhitespace: false,
      renderOverviewRuler: true,
      readOnly: true,
    });
    diffEditor.setModel({ original, modified });
  }

  async function commitChanges() {
    const msg = $('#gitMessage').value.trim();
    if (!msg) return toast('write a commit message first', 'err');
    $('#btnCommit').disabled = true;
    try {
      const res = await api.post('/api/git/commit', { root: S.root, message: msg });
      $('#gitCommitOut').textContent = (res.output || 'committed').trim().split('\n')[0];
      $('#gitMessage').value = '';
      toast('committed', 'ok');
      refreshGit();
    } catch (err) {
      $('#gitCommitOut').textContent = err.message;
      toast(err.message, 'err');
    } finally {
      $('#btnCommit').disabled = false;
    }
  }

  async function loadBranches() {
    if (!S.root) return;
    try {
      const res = await api.get('/api/git/branches', { root: S.root });
      const sel = $('#gitBranch');
      if (!res.branches || !res.branches.length) {
        sel.innerHTML = '';
        $('#gitActions').classList.add('hidden');
        return;
      }
      $('#gitActions').classList.remove('hidden');
      if (sel.dataset.sig !== res.branches.join(',')) {
        sel.dataset.sig = res.branches.join(',');
        sel.innerHTML = res.branches.map((b) =>
          `<option value="${esc(b)}">${esc(b)}</option>`).join('');
      }
      if (res.current) sel.value = res.current;
    } catch {
      $('#gitActions').classList.add('hidden');
    }
  }

  async function checkoutBranch(branch) {
    if (!branch || branch === S.git.branch) return;
    try {
      const res = await api.post('/api/git/checkout', { root: S.root, branch });
      toast(`switched to ${branch}`, 'ok');
      S.treeCache.clear();
      await refreshTree();
      refreshGit();
      if (res.output) $('#gitCommitOut').textContent = res.output.split('\n')[0];
    } catch (err) {
      toast(err.message, 'err');
      loadBranches();
    }
  }

  async function gitPushPull(kind) {    if (!S.root) return;
    const btn = kind === 'push' ? $('#btnGitPush') : $('#btnGitPull');
    btn.disabled = true;
    try {
      const res = await api.post(`/api/git/${kind}`, { root: S.root });
      toast(`${kind} ok`, 'ok');
      if (res.output) $('#gitCommitOut').textContent = res.output.trim().split('\n')[0];
      refreshGit();
      S.treeCache.clear();
      renderTree();
    } catch (err) {
      toast(`${kind}: ${err.message}`, 'err');
      $('#gitCommitOut').textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  /* -------------------------------- terminal ------------------------------- */
  function termWrite(text) {
    const out = $('#termOut');
    out.textContent += text;
    if (out.textContent.length > 400000) out.textContent = out.textContent.slice(-300000);
    out.scrollTop = out.scrollHeight;
  }

  async function startTerminal(cwd) {
    if (S.term.id) await api.post('/api/term/kill', { id: S.term.id }).catch(() => {});
    $('#termOut').textContent = '';
    try {
      const res = await api.post('/api/term/start', { cwd: cwd || S.root || S.env.cwd });
      S.term = { id: res.id, cursor: res.cursor, cwd: res.cwd, dead: false, history: S.term.history, hIndex: -1 };
      $('#termCwd').textContent = res.cwd;
      if (cwd && S.root && cwd !== S.root) sendCmdToTerm(`Set-Location "${cwd}"`);
    } catch (err) {
      termWrite(`* failed to start shell: ${err.message}\n`);
    }
  }

  async function pollTerminal() {
    if (!S.term.id) return;
    try {
      const res = await api.get('/api/term/poll', { id: S.term.id, cursor: S.term.cursor });
      if (res.output) {
        termWrite(res.output);
        S.term.cursor = res.cursor;
      }
      if (res.cwd) {
        S.term.cwd = res.cwd;
        $('#termCwd').textContent = res.cwd;
      }
      if (!res.alive && !S.term.dead) {
        S.term.dead = true;
        termWrite('\n* shell exited — press ⟳ to restart\n');
      }
    } catch { /* transient */ }
  }

  async function sendCmdToTerm(cmd) {
    if (!S.term.id) {
      await startTerminal(S.root || undefined);
      await sleep(120);
    }
    try {
      if (!S.term.history.includes(cmd)) S.term.history.push(cmd);
      S.term.hIndex = -1;
      await api.post('/api/term/input', { id: S.term.id, data: cmd });
    } catch (err) {
      termWrite(`* ${err.message}\n`);
    }
  }

  function bindTerminal() {
    const input = $('#termInput');
    input.addEventListener('keydown', async (e) => {
      if (e.key === 'Enter') {
        const cmd = input.value.trim();
        if (!cmd) return;
        input.value = '';
        termWrite(`\n❯ ${cmd}\n`);
        if (cmd === 'clear' || cmd === 'cls') { $('#termOut').textContent = ''; return; }
        await sendCmdToTerm(cmd);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        const h = S.term.history;
        if (!h.length) return;
        S.term.hIndex = S.term.hIndex < 0 ? h.length - 1 : Math.max(0, S.term.hIndex - 1);
        input.value = h[S.term.hIndex] || '';
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        const h = S.term.history;
        if (S.term.hIndex < 0) return;
        S.term.hIndex += 1;
        if (S.term.hIndex >= h.length) { S.term.hIndex = -1; input.value = ''; }
        else input.value = h[S.term.hIndex];
      } else if (e.key === 'c' && e.ctrlKey) {
        e.preventDefault();
        input.value = '';
        await api.post('/api/term/interrupt', { id: S.term.id }).catch(() => {});
        termWrite('\n* interrupted\n');
      }
    });
  }

  async function runFile(rel) {
    if (!rel) return toast('no file to run', 'err');
    try {
      const res = await api.post('/api/run', { root: S.root, path: rel });
      showPanel(true);
      switchPanelTab('terminal');
      await sendCmdToTerm(res.command);
      termWrite(`\n❯ ${res.command}\n`);
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  /* -------------------------------- chat dock ------------------------------- */
  const CHAT_KEY = 'vanta.chat.open';
  const CHAT_W_KEY = 'vanta.chat.w';
  let chatStream = null;       // bubble currently receiving delta events
  let chatCfg = null;          // last server-side settings snapshot
  let chatClock = null;        // elapsed-seconds ticker while a reply is pending

  function startChatClock() {
    const t0 = Date.now();
    clearInterval(chatClock);
    chatClock = setInterval(() => {
      if (!S.ai.busy) { clearInterval(chatClock); chatClock = null; return; }
      const row = $('#chatState');
      if (row) row.textContent = `writing… ${Math.round((Date.now() - t0) / 1000)}s`;
    }, 1000);
  }

  function stopChatClock() {
    if (chatClock) clearInterval(chatClock);
    chatClock = null;
  }

  function chatOpen() {
    return !$('#chatDock').classList.contains('closed');
  }

  function paintChatAct() {
    const btn = $('.act[data-view="ai"]');
    if (btn) btn.classList.toggle('active', chatOpen());
  }

  function showChat(on) {
    S.settings.chat = !!on;
    $('#chatDock').classList.toggle('closed', !on);
    const rz = $('#dockRz');
    if (rz) rz.classList.toggle('hidden', !on);
    paintChatAct();
    try { localStorage.setItem(CHAT_KEY, on ? '1' : '0'); } catch { /* private mode */ }
    if (editor) editor.layout();
    if (on) setTimeout(() => $('#chatInput').focus(), 40);
  }

  function paintChatTag() {
    const tag = $('#chatTag');
    const provider = S.ai.provider;
    const labels = { api: 'vps', anthropic: 'claude', opencode: 'opencode', local: 'local' };
    const label = labels[provider] || provider;
    const model = provider === 'local'
      ? (S.ai.model || ($('#chatLocalModel') ? $('#chatLocalModel').value.trim() : ''))
      : provider === 'anthropic'
        ? (($('#chatAnthropicModel') && $('#chatAnthropicModel').value.trim()) || S.ai.model || 'claude')
        : provider === 'opencode' ? 'opencode'
          : (($('#chatApiModel') && $('#chatApiModel').value) || S.ai.model || 'auto');
    const fsOn = $('#chatFailsafe') ? $('#chatFailsafe').checked : S.ai.failsafe !== false;
    if (tag) {
      tag.textContent = `${label} · ${model || 'auto'}`;
      tag.classList.toggle('api', provider === 'api');
      tag.title = provider === 'api'
        ? `Primary: Remote Ollama VPS${fsOn ? ' (Local AirLLM failsafe armed)' : ''}`
        : provider === 'anthropic' ? 'Claude (Anthropic API)'
          : provider === 'opencode' ? 'OpenCode CLI agent'
            : 'Local AirLLM engine';
    }
    const chip = $('#sbAi');
    if (chip) {
      chip.textContent = `ai: ${label}${model ? ' · ' + base(model) : ''}${provider === 'api' && fsOn ? ' (+fs)' : ''}`;
      chip.classList.toggle('busy', S.ai.state === 'generating' || S.ai.state === 'loading');
      const titles = {
        api: `Remote Ollama VPS (${$('#chatApiBase')?.value || ''}) — Failsafe: ${fsOn ? 'Local AirLLM' : 'off'}`,
        anthropic: 'Claude via Anthropic API',
        opencode: 'OpenCode CLI agent in this workspace',
        local: `Local AirLLM (${model || ''})`,
      };
      chip.title = titles[provider] || provider;
    }
  }

  function setAiState(state, model) {
    S.ai.state = state || 'idle';
    if (model) S.ai.model = model;
    const row = $('#chatState');
    if (row && !chatClock) {
      row.textContent = state === 'generating' ? 'writing…'
        : state === 'loading' ? 'loading model…' : (state || 'idle');
    }
    paintChatTag();
  }

  function applyChatConfig(cfg) {
    if (!cfg) return;
    chatCfg = cfg;
    S.ai.provider = ['api', 'anthropic', 'opencode', 'local'].includes(cfg.provider)
      ? cfg.provider : 'api';
    S.ai.failsafe = cfg.failsafe !== false;
    S.ai.mode = cfg.mode || 'agent';
    $$('#chatProvider .seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.provider === S.ai.provider));
    $('#chatLocalGroup').classList.toggle('hidden', S.ai.provider !== 'local');
    $('#chatApiGroup').classList.toggle('hidden', S.ai.provider !== 'api');
    $('#chatAnthropicGroup').classList.toggle('hidden', S.ai.provider !== 'anthropic');
    $('#chatOpencodeGroup').classList.toggle('hidden', S.ai.provider !== 'opencode');

    $('#chatMode').value = S.ai.mode;
    if (cfg.temperature !== undefined) $('#chatTemp').value = cfg.temperature;
    if (cfg.maxTokens !== undefined) $('#chatMax').value = cfg.maxTokens;
    $('#chatContext').checked = cfg.contextFile !== false;
    if ($('#chatAgentTools')) $('#chatAgentTools').checked = cfg.agentTools !== false;
    if ($('#chatSandbox')) $('#chatSandbox').checked = cfg.agentSandbox !== false;
    if ($('#chatFailsafe')) $('#chatFailsafe').checked = S.ai.failsafe;
    if ($('#chatInstructions')) $('#chatInstructions').value = cfg.customInstructions || '';
    if ($('#chatBudget')) $('#chatBudget').value = cfg.contextBudget || 32000;

    // API profiles
    const profList = $('#chatProfileList');
    if (profList) {
      const profs = cfg.apiProfiles || [];
      profList.innerHTML = '<option value="">default (fields above)</option>' +
        profs.map((p) => `<option value="${esc(p.name)}">${esc(p.name)}${p.hasKey ? ' ·key' : ''}</option>`).join('');
      profList.value = cfg.profile || '';
    }

    const local = cfg.local || {};
    $('#chatLocalModel').value = local.model || '';
    $('#chatCompression').value = local.compression || '';
    const datalist = $('#chatLocalList');
    if (datalist) {
      datalist.innerHTML = (cfg.presets || []).map((m) =>
        `<option value="${esc(m)}"></option>`).join('');
    }

    const peerList = $('#chatPeerList');
    if (peerList) {
      const peers = (cfg.peers || []).map((p) =>
        `<option value="${esc(p.ollamaUrl)}">${esc(p.host)} (${esc(p.ip)}${p.online ? ' · online' : ''})</option>`);
      peers.push('<option value="http://127.0.0.1:11434/v1">localhost Ollama (127.0.0.1:11434)</option>');
      peerList.innerHTML = peers.join('');
    }

    const apiCfg = cfg.api || {};
    $('#chatApiBase').value = apiCfg.baseUrl || 'http://100.120.101.8:11434/v1';
    $('#chatApiKey').value = '';
    $('#chatApiKey').placeholder = apiCfg.hasKey
      ? 'key saved — type to replace'
      : 'optional — saved to ide/.ai.json';
    if ($('#chatAnthropicKey')) {
      $('#chatAnthropicKey').value = '';
      $('#chatAnthropicKey').placeholder = (cfg.anthropic && cfg.anthropic.hasKey)
        ? 'key saved — type to replace'
        : 'sk-ant-… (saved locally)';
    }
    if (apiCfg.model) {
      const sel = $('#chatApiModel');
      if (!Array.from(sel.options).some((o) => o.value === apiCfg.model)) {
        sel.insertAdjacentHTML('beforeend',
          `<option value="${esc(apiCfg.model)}">${esc(apiCfg.model)}</option>`);
      }
      sel.value = apiCfg.model;
    }
    paintChatTag();
    setAiState(S.ai.state, S.ai.model);
    if (S.ai.provider === 'api' && apiCfg.baseUrl) {
      api.get('/api/ai/models', { provider: 'api' }).then((res) => {
        if (res && res.models && res.models.length) {
          const sel = $('#chatApiModel');
          const cur = sel.value || apiCfg.model || res.models[0];
          sel.innerHTML = '<option value="">auto (first available on VPS)</option>' +
            res.models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
          if (cur && res.models.includes(cur)) sel.value = cur;
          paintChatTag();
        }
      }).catch(() => {});
    }
  }

  async function loadChatConfig() {
    try {
      applyChatConfig(await api.get('/api/ai/config'));
    } catch (err) {
      toast('chat config: ' + err.message, 'err');
    }
  }

  async function saveProfile() {
    const name = await promptModal('New profile name', '', 'e.g. groq · openai · vps-2');
    if (!name) return;
    const profs = ((chatCfg || {}).apiProfiles || []).filter((p) => p.name !== name);
    profs.push({
      name,
      baseUrl: $('#chatApiBase').value.trim(),
      model: $('#chatApiModel').value || '',
    });
    const key = $('#chatApiKey').value.trim();
    if (key) profs[profs.length - 1].apiKey = key;
    const res = await saveChatConfig({ apiProfiles: profs, profile: name });
    if (res) {
      await loadChatConfig();
      toast(`profile ${name} saved`, 'ok');
    }
  }

  async function deleteProfile() {
    const sel = $('#chatProfileList');
    const name = sel.value;
    if (!name) return toast('select a profile first', 'err');
    const ok = await confirmModal('Delete profile', `Remove profile "${name}"?`, 'Delete', true);
    if (!ok) return;
    const profs = ((chatCfg || {}).apiProfiles || []).filter((p) => p.name !== name);
    await saveChatConfig({ apiProfiles: profs, profile: '' });
    await loadChatConfig();
    toast(`profile ${name} deleted`, 'ok');
  }

  function gatherChatPatch(extra) {
    const patch = {
      provider: S.ai.provider,
      profile: $('#chatProfileList') ? ($('#chatProfileList').value || '') : '',
      customInstructions: $('#chatInstructions') ? $('#chatInstructions').value : '',
      contextBudget: Number($('#chatBudget') ? $('#chatBudget').value : 0) || 32000,
      anthropic: {
        model: $('#chatAnthropicModel') ? $('#chatAnthropicModel').value.trim() : '',
      },
      failsafe: $('#chatFailsafe') ? $('#chatFailsafe').checked : true,
      mode: $('#chatMode').value,
      contextFile: $('#chatContext').checked,
      agentTools: $('#chatAgentTools') ? $('#chatAgentTools').checked : true,
      agentSandbox: $('#chatSandbox') ? $('#chatSandbox').checked : true,
      temperature: Number($('#chatTemp').value) || 0.6,
      maxTokens: Number($('#chatMax').value) || 512,
      local: {
        model: $('#chatLocalModel').value.trim(),
        compression: $('#chatCompression').value || '',
      },
      api: {
        baseUrl: $('#chatApiBase').value.trim(),
        model: $('#chatApiModel').value || '',
      },
    };
    const key = $('#chatApiKey').value.trim();
    if (key) patch.api.apiKey = key;
    const akey = $('#chatAnthropicKey') ? $('#chatAnthropicKey').value.trim() : '';
    if (akey) patch.anthropic.apiKey = akey;
    return Object.assign(patch, extra || {});
  }

  async function saveChatConfig(patch, clearKey = false) {
    try {
      const res = await api.post('/api/ai/config', { config: patch || {}, clearKey });
      chatCfg = res;
      if (res.api) {
        $('#chatApiKey').value = '';
        $('#chatApiKey').placeholder = res.api.hasKey
          ? 'key saved — type to replace'
          : 'saved to ide/.ai.json';
      }
      return res;
    } catch (err) {
      toast('settings: ' + err.message, 'err');
      return null;
    }
  }

  function setProvider(provider, quiet = false) {
    const known = ['api', 'anthropic', 'opencode', 'local'];
    S.ai.provider = known.includes(provider) ? provider : 'api';
    $$('#chatProvider .seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.provider === S.ai.provider));
    $('#chatLocalGroup').classList.toggle('hidden', S.ai.provider !== 'local');
    $('#chatApiGroup').classList.toggle('hidden', S.ai.provider !== 'api');
    $('#chatAnthropicGroup').classList.toggle('hidden', S.ai.provider !== 'anthropic');
    $('#chatOpencodeGroup').classList.toggle('hidden', S.ai.provider !== 'opencode');
    const hints = {
      api: 'Primary: Remote Ollama on VPS. If unreachable and failsafe is checked, requests automatically route to local AirLLM.',
      anthropic: 'Claude via api.anthropic.com — native tool calling in agent mode. Set your key below.',
      opencode: 'OpenCode CLI runs as the agent in this workspace (`opencode run`). Install it on PATH.',
      local: "Failsafe / Local: AirLLM streams layers off disk onto this machine's GPU.",
    };
    $('#chatHint').textContent = hints[S.ai.provider] || '';
    paintChatTag();
    if (!quiet) saveChatConfig({ provider: S.ai.provider });
  }

  async function fetchChatModels() {
    const btn = $('#btnChatFetch');
    btn.disabled = true;
    btn.textContent = '…';
    try {
      await saveChatConfig(gatherChatPatch());
      const res = await api.get('/api/ai/models', { provider: 'api' });
      const sel = $('#chatApiModel');
      const current = sel.value;
      const models = res.models || [];
      sel.innerHTML = '<option value="">auto (first available on VPS)</option>' +
        models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
      if (current && models.includes(current)) sel.value = current;
      else if (models.length) sel.value = models[0];
      paintChatTag();
      toast(`${models.length} model${models.length === 1 ? '' : 's'} from ${$('#chatApiBase').value}`, 'ok');
    } catch (err) {
      toast('vps models: ' + err.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '⟳';
    }
  }

  async function loadLocalModel() {
    const model = $('#chatLocalModel').value.trim();
    if (!model) { toast('set a model id first', 'err'); return; }
    const compression = $('#chatCompression').value || null;
    $('#chatHint').textContent = `loading ${model} — first run downloads the checkpoint, precision is picked to fit free VRAM…`;
    try {
      await saveChatConfig({ provider: 'local', local: { model, compression: compression || '' } });
      await api.post('/api/ai/load', { model, compression });
      setProvider('local', true);
      paintChatTag();
      aiWrite('system', `loading ${model}`, 'system');
    } catch (err) {
      $('#chatHint').textContent = err.message;
      toast(err.message, 'err');
    }
  }

  function selectionText() {
    if (!editor) return '';
    const model = editor.getModel();
    const sel = editor.getSelection();
    if (!model || !sel || sel.isEmpty()) return '';
    return model.getValueInRange(sel);
  }

  function aiWrite(who, text, cls = '', streaming = false) {
    const log = $('#chatLog');
    const empty = log.querySelector('.empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    div.className = `msg ${cls}${streaming ? ' streaming' : ''}`;
    div.innerHTML = `<span class="who">${esc(who)}</span><div class="msg-body"></div>`;
    div.querySelector('.msg-body').textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    if (streaming) chatStream = div;
    return div;
  }

  function aiWriteTool(ev) {
    const log = $('#chatLog');
    const empty = log.querySelector('.empty');
    if (empty) empty.remove();
    const div = document.createElement('div');
    const okCls = ev.ok === false ? 'err' : 'ok';
    div.className = `msg tool ${okCls}`;
    const detail = ev.detail ? ` · ${ev.detail}` : '';
    const sum = ev.summary ? `<div class="tool-sum">${esc(ev.summary)}</div>` : '';
    div.innerHTML = `<div class="tool-head"><span class="tool-badge">⚙ ${esc(ev.name || 'tool')}</span><span class="tool-target">${esc(detail)}</span></div>${sum}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function chatEmpty() {
    $('#chatLog').innerHTML =
      '<div class="empty"><b>Chat dock.</b><br/>Set a provider and model in ⚙, pick a mode, then ask. ' +
      'With text selected in the editor, <b>edit</b> mode rewrites it — press <b>Apply</b>.</div>';
  }

  function clearChatView(note) {
    S.ai.messages = [];
    S.ai.lastReply = '';
    S.ai.busy = false;
    stopChatClock();
    chatStream = null;
    $('#btnChatSend').disabled = false;
    $('#btnChatStop').classList.add('hidden');
    $('#btnChatApply').classList.add('hidden');
    chatEmpty();
    if (note) aiWrite('system', note, 'system');
  }

  async function resetChat() {
    try { await api.post('/api/ai/reset', {}); } catch { /* ignore */ }
    clearChatView('worker reset');
  }

  function finishChat() {
    S.ai.busy = false;
    stopChatClock();
    $('#btnChatSend').disabled = false;
    $('#btnChatStop').classList.add('hidden');
    if (chatStream) chatStream.classList.remove('streaming');
    chatStream = null;
  }

  async function stopChat() {
    if (!S.ai.busy) return;
    try { await api.post('/api/ai/cancel'); } catch { /* best effort */ }
  }

  function finishChatReply(text) {
    const div = (chatStream && chatStream.isConnected)
      ? chatStream : aiWrite('assistant', '', 'assistant');
    div.classList.remove('streaming');
    div.querySelector('.who').textContent = S.ai.provider;
    const body = div.querySelector('.msg-body');
    const reply = text !== undefined && text !== null && text !== '' ? text : body.textContent;
    if (reply && body.textContent !== reply) body.textContent = reply;
    if (!reply) body.textContent = '…';
    S.ai.lastReply = reply;
    S.ai.busy = false;
    stopChatClock();
    $('#btnChatSend').disabled = false;
    $('#btnChatStop').classList.add('hidden');
    if (reply) S.ai.messages.push({ role: 'assistant', content: reply });
    chatStream = null;
    if ($('#chatMode').value === 'edit') {
      $('#btnChatApply').classList.toggle('hidden', !selectionText() || !reply);
    }
    const log = $('#chatLog');
    log.scrollTop = log.scrollHeight;
  }

  async function chatSend() {
    const input = $('#chatInput');
    const text = input.value.trim();
    if (!text || S.ai.busy) return;
    if (!chatOpen()) showChat(true);
    const tab = activeTab();
    const fileText = tab && tab.kind === 'file' && editor ? editor.getValue() : '';
    const images = S.ai.provider === 'api' ? S.attachments.slice(0, 4) : [];
    if (images.length && S.ai.provider !== 'api') S.attachments = [];
    const body = {
      provider: S.ai.provider,
      profile: $('#chatProfileList') ? ($('#chatProfileList').value || '') : '',
      failsafe: $('#chatFailsafe') ? $('#chatFailsafe').checked : true,
      mode: $('#chatMode').value,
      messages: [],
      temperature: Number($('#chatTemp').value),
      maxTokens: Number($('#chatMax').value) || 512,
      contextFile: $('#chatContext').checked,
      agentTools: $('#chatAgentTools') ? $('#chatAgentTools').checked : true,
      agentSandbox: $('#chatSandbox') ? $('#chatSandbox').checked : true,
      localModel: $('#chatLocalModel').value.trim(),
      apiBaseUrl: $('#chatApiBase').value.trim(),
      apiModel: $('#chatApiModel').value,
      anthropicModel: $('#chatAnthropicModel') ? $('#chatAnthropicModel').value.trim() : '',
      anthropicApiKey: $('#chatAnthropicKey') ? $('#chatAnthropicKey').value.trim() : '',
      root: S.root || '',
      file: tab ? tab.path : '',
      fileText,
      selection: selectionText(),
    };
    if (body.provider !== 'api' && body.provider !== 'anthropic') delete body.anthropicApiKey;
    input.value = '';
    if (images.length) {
      const content = [{ type: 'text', text }];
      for (const url of images) content.push({ type: 'image_url', image_url: { url } });
      S.ai.messages.push({ role: 'user', content });
      S.attachments = [];
      renderAttachments();
    } else {
      S.ai.messages.push({ role: 'user', content: text });
    }
    body.messages = S.ai.messages.slice(-16);
    aiWrite('you', text, 'user');
    S.ai.busy = true;
    S.ai.lastReply = '';
    $('#btnChatSend').disabled = true;
    $('#btnChatStop').classList.remove('hidden');
    $('#btnChatApply').classList.add('hidden');
    startChatClock();
    const pending = aiWrite(S.ai.provider === 'api' ? 'vps' : S.ai.provider, '', 'assistant streaming', true);
    try {
      const res = await api.post('/api/ai/chat', body);
      if (res.model) S.ai.model = res.model;
      paintChatTag();
      saveChatConfig({
        provider: body.provider,
        failsafe: body.failsafe,
        mode: body.mode,
        temperature: body.temperature,
        maxTokens: body.maxTokens,
        contextFile: body.contextFile,
        agentTools: body.agentTools,
        agentSandbox: body.agentSandbox,
      });
    } catch (err) {
      if (pending.isConnected) pending.remove();
      chatStream = null;
      aiWrite('error', err.message, 'error');
      finishChat();
    }
  }

  function stripFences(text) {
    const m = String(text).match(/^\s*```[\w+#.-]*\r?\n([\s\S]*?)\r?\n```\s*$/);
    return m ? m[1] : String(text);
  }

  function applyChatReply() {
    if (!S.ai.lastReply) return;
    const sel = editor && editor.getSelection();
    if (!sel || sel.isEmpty()) { toast('select the code to replace first', 'err'); return; }
    editor.executeEdits('chat', [{
      range: sel,
      text: stripFences(S.ai.lastReply),
      forceMoveMarkers: true,
    }]);
    editor.focus();
    toast('reply applied to selection', 'ok');
  }

  async function pollAi() {
    try {
      const res = await api.get('/api/ai/poll', { cursor: S.ai.cursor });
      for (const ev of res.events) {
        S.ai.cursor = ev.seq;
        if (ev.type === 'state') {
          setAiState(ev.msg, res.model);
        } else if (ev.type === 'model') {
          S.ai.model = ev.msg;
          setAiState(res.state, ev.msg);
        } else if (ev.type === 'delta') {
          if (!chatStream || !chatStream.isConnected) {
            chatStream = aiWrite(S.ai.provider, '', 'assistant streaming', true);
          }
          const body = chatStream.querySelector('.msg-body');
          body.textContent += ev.text;
          const log = $('#chatLog');
          log.scrollTop = log.scrollHeight;
        } else if (ev.type === 'step') {
          if (chatStream && chatStream.isConnected) {
            chatStream.classList.remove('streaming');
            const body = chatStream.querySelector('.msg-body');
            if (!body.textContent && ev.text) body.textContent = ev.text;
          } else if (ev.text) {
            aiWrite(S.ai.provider, ev.text, 'assistant');
          }
          chatStream = null;
        } else if (ev.type === 'tool') {
          aiWriteTool(ev);
        } else if (ev.type === 'reply') {
          finishChatReply(ev.text);
        } else if (ev.type === 'file_changed') {
          S.treeCache.clear();
          refreshTree();
          refreshGit();
          const hit = S.tabs.find((t) => t.kind === 'file' && t.path === ev.path);
          if (hit && S.root) {
            api.get('/api/file', { root: S.root, path: hit.path }).then((d) => {
              if (d && !d.binary && !d.tooLarge && hit.model) {
                hit.model.setValue(d.content);
                hit.savedContent = d.content;
                hit.mtime = d.mtime;
                hit.dirty = false;
                renderTabs();
              }
            }).catch(() => {});
          }
        } else if (ev.type === 'error') {
          if (chatStream && chatStream.isConnected) chatStream.remove();
          chatStream = null;
          aiWrite('error', ev.msg, 'error');
          finishChat();
        } else if (ev.type === 'note') {
          aiWrite('engine', ev.msg, 'system');
        } else if (ev.type === 'log') {
          /* engine diagnostics stay out of the conversation */
        } else if (ev.type === 'confirm') {
          aiWriteConfirm(ev);
        } else if (ev.type === 'reset') {
          clearChatView();
        }
      }
      if (res.state) setAiState(res.state, res.model);
    } catch { /* server may be restarting */ }
  }

  function aiWriteConfirm(ev) {
    const log = $('#chatLog');
    const div = document.createElement('div');
    div.className = 'msg confirm';
    div.innerHTML = `
      <div class="tool-head"><span class="tool-badge">⚠ confirm</span></div>
      <div class="msg-body"><pre class="modal-pre">${esc(ev.command || '')}</pre>
        <div class="row gap6">
          <button class="btn danger" data-cid="${esc(ev.id)}" data-approve="0">Deny</button>
          <button class="btn primary" data-cid="${esc(ev.id)}" data-approve="1">Allow</button>
        </div></div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async function answerConfirm(id, approve) {
    try { await api.post('/api/confirm', { id, approve }); } catch { /* gone */ }
  }

  /* --------------------------------- panel --------------------------------- */
  function showPanel(on) {
    S.settings.panel = on;
    $('#panel').classList.toggle('closed', !on);
    if (on && editor) editor.layout();
  }

  function switchPanelTab(name) {
    $$('.ptab').forEach((b) => b.classList.toggle('active', b.dataset.ptab === name));
    $('#termPane').classList.toggle('hidden', name !== 'terminal');
    $('#outputPane').classList.toggle('hidden', name !== 'output');
    $('#debugPane').classList.toggle('hidden', name !== 'debug');
    if (name === 'terminal') $('#termInput').focus();
  }

  function output(text) {
    $('#outputText').textContent = text;
    showPanel(true);
    switchPanelTab('output');
  }

  /* --------------------------------- views --------------------------------- */
  function setView(name) {
    if (name === 'ai') {
      if (!chatOpen()) showChat(true);
      else $('#chatInput').focus();
      paintChatAct();
      return;
    }
    $$('.act').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
    $$('#sidebar .side').forEach((s) => s.classList.toggle('visible', s.dataset.panel === name));
    S.settings.sidebar = true;
    $('#sidebar').classList.remove('collapsed');
    if (name === 'search') $('#searchInput').focus();
    if (name === 'scm') refreshGit();
    if (name === 'project') { loadRemotes(); loadPlugins(); loadEditors(); }
    paintChatAct();
  }

  /* --------------------------------- menus --------------------------------- */
  const MENUS = {
    file: [
      ['Open Folder…', '', () => browseModal()],
      ['Open Recent', '', () => setView('project')],
      ['divider'],
      ['New File…', '', () => createEntry('', false)],
      ['New Folder…', '', () => createEntry('', true)],
      ['divider'],
      ['Save', 'Ctrl+S', saveActive],
      ['Save All', 'Ctrl+Shift+S', saveAll],
      ['Close Editor', 'Ctrl+W', () => S.active && closeTab(S.active)],
      ['divider'],
      ['Autosave', '', () => toggleSetting('autosave'), 'autosave'],
      ['Reload Window', '', () => location.reload()],
    ],
    edit: [
      ['Undo', '', () => editor.trigger('menu', 'undo')],
      ['Redo', '', () => editor.trigger('menu', 'redo')],
      ['divider'],
      ['Find', 'Ctrl+F', () => editor.trigger('menu', 'actions.find')],
      ['Replace', 'Ctrl+H', () => editor.trigger('menu', 'editor.action.startFindReplaceAction')],
      ['divider'],
      ['Format Document', 'Shift+Alt+F', () => editor.trigger('menu', 'editor.action.formatDocument')],
      ['Toggle Comment', 'Ctrl+/', () => editor.trigger('menu', 'editor.action.commentLine')],
      ['Trim Trailing Whitespace', '', () => {
        const m = editor.getModel();
        if (!m) return;
        const ops = [{ range: m.getFullModelRange(), text: m.getValue().replace(/[ \t]+$/gm, '') }];
        m.pushEditOperations([], ops, () => null);
      }],
    ],
    view: [
      ['Explorer', 'Ctrl+Shift+E', () => setView('explorer')],
      ['Search', 'Ctrl+Shift+F', () => setView('search')],
      ['Source Control', 'Ctrl+Shift+G', () => setView('scm')],
      ['AirLLM', '', () => setView('ai')],
      ['Chat Dock', 'Ctrl+J', () => setView('ai')],
      ['divider'],
      ['Toggle Sidebar', 'Ctrl+B', () => toggleSidebar()],
      ['Toggle Panel', 'Ctrl+`', () => showPanel(!S.settings.panel)],
      ['divider'],
      ['Toggle Minimap', '', () => toggleSetting('minimap')],
      ['Toggle Word Wrap', 'Alt+Z', () => toggleSetting('wordWrap')],
      ['Zoom In', 'Ctrl+=', () => zoom(1)],
      ['Zoom Out', 'Ctrl+-', () => zoom(-1)],
    ],
    go: [
      ['Go to File…', 'Ctrl+P', () => openQuickOpen()],
      ['Command Palette…', 'Ctrl+Shift+P', () => openPalette()],
      ['Go to Line…', 'Ctrl+G', () => editor.trigger('menu', 'editor.action.gotoLine')],
      ['Go to Symbol…', 'Ctrl+Shift+O', () => editor.trigger('menu', 'editor.action.gotoSymbol')],
      ['divider'],
      ['Go to Definition', 'F12', () => editor.trigger('menu', 'editor.action.revealDefinition')],
      ['Find References', 'Shift+F12', () => editor.trigger('menu', 'editor.action.goToReferences')],
    ],
    run: [
      ['Run Active File', 'F5', () => S.active && runFile(activeTab()?.path)],
      ['Debug Python File', '', debugActiveFile],
      ['Restart Terminal', '', () => startTerminal(S.root || undefined)],
      ['Stop Shell', '', () => S.term.id && api.post('/api/term/interrupt', { id: S.term.id }).catch(() => {})],
      ['divider'],
      ['Refresh Git Status', '', refreshGit],
    ],
    terminal: [
      ['New Terminal', '', () => startTerminal(S.root || undefined)],
      ['Clear', '', () => { $('#termOut').textContent = ''; }],
      ['Interrupt / Restart', '', () => S.term.id && api.post('/api/term/interrupt', { id: S.term.id }).catch(() => {})],
      ['divider'],
      ['Toggle Panel', 'Ctrl+`', () => showPanel(!S.settings.panel)],
    ],
    help: [
      ['Keyboard Shortcuts', '', showShortcuts],
      ['AirLLM on GitHub', '', () => openExternal('https://github.com/lyogavin/airllm')],
      ['divider'],
      ['About Vanta', '', showAbout],
    ],
  };

  function toggleSetting(key) {
    S.settings[key] = !S.settings[key];
    if (key === 'minimap') editor.updateOptions({ minimap: { enabled: S.settings.minimap } });
    if (key === 'wordWrap') editor.updateOptions({ wordWrap: S.settings.wordWrap ? 'on' : 'off' });
    if (key === 'autosave') toast(`autosave ${S.settings.autosave ? 'on' : 'off'}`, 'ok');
    if (key === 'sidebar') $('#sidebar').classList.toggle('collapsed', !S.settings.sidebar);
  }

  function toggleSidebar() {
    S.settings.sidebar = !S.settings.sidebar;
    $('#sidebar').classList.toggle('collapsed', !S.settings.sidebar);
    if (editor) editor.layout();
  }

  function zoom(delta) {
    S.settings.fontSize = Math.min(28, Math.max(9, S.settings.fontSize + delta));
    editor.updateOptions({ fontSize: S.settings.fontSize });
    if (diffEditor) diffEditor.updateOptions({ fontSize: S.settings.fontSize });
  }

  function openMenu(name, anchor) {
    const drop = $('#menuDrop');
    const items = MENUS[name] || [];
    drop.innerHTML = '';
    for (const item of items) {
      if (item === 'divider' || item[0] === 'divider') { drop.appendChild(document.createElement('hr')); continue; }
      const [label, hint, run, settingKey] = item;
      const b = document.createElement('button');
      const checked = settingKey && S.settings[settingKey] ? ' ✓' : '';
      b.innerHTML = `<span>${esc(label)}${checked}</span><span class="key">${esc(hint || '')}</span>`;
      b.addEventListener('click', () => { hideMenu(); run(); });
      drop.appendChild(b);
    }
    const rect = anchor.getBoundingClientRect();
    drop.classList.remove('hidden');
    drop.style.left = `${rect.left}px`;
    drop.style.top = `${rect.bottom + 2}px`;
  }

  function hideMenu() {
    $('#menuDrop').classList.add('hidden');
    $$('#menubar button').forEach((b) => b.classList.remove('open'));
  }

  /* -------------------------------- overlay -------------------------------- */
  const OVERLAY = { mode: 'commands', items: [], index: 0, filter: '' };

  function openOverlay(mode, placeholder) {
    OVERLAY.mode = mode;
    OVERLAY.index = 0;
    OVERLAY.filter = '';
    const input = $('#overlayInput');
    input.value = '';
    input.placeholder = placeholder;
    $('#overlay').classList.remove('hidden');
    input.focus();
    renderOverlay();
  }

  function closeOverlay() {
    $('#overlay').classList.add('hidden');
  }

  function fuzzyScore(needle, hay) {
    if (!needle) return 1;
    const n = needle.toLowerCase(), h = hay.toLowerCase();
    let score = 0, hi = 0, streak = 0;
    for (let i = 0; i < n.length; i++) {
      const idx = h.indexOf(n[i], hi);
      if (idx < 0) return -1;
      streak = idx === hi ? streak + 1 : 0;
      score += 10 + streak * 6 - Math.min(8, idx - hi);
      if (idx === 0 || '/\\_.-'.includes(h[idx - 1])) score += 12;
      hi = idx + 1;
    }
    return score - h.length * 0.06;
  }

  function overlaySource() {
    if (OVERLAY.mode === 'files') {
      return (S.fileList || []).slice(0, 12000).map((p) => ({
        label: base(p), path: p, kind: 'file', run: () => openFile(p),
      }));
    }
    return COMMANDS.concat(S.tasks.map((t) => ({
      label: `Task: ${t.label || t.name || 'task'}`,
      hint: (t.command || t.cmd || t.run || '').slice(0, 60),
      kind: 'task',
      run: () => runTask(t),
    }))).map((c) => ({
      label: c.label, path: c.hint || '', kind: c.kind || 'cmd', run: c.run,
    }));
  }

  function renderOverlay() {
    const list = $('#overlayList');
    const q = OVERLAY.filter.trim();
    let items = overlaySource();
    if (q) {
      items = items
        .map((it) => ({ it, score: fuzzyScore(q, it.path ? it.path + ' ' + it.label : it.label) }))
        .filter((x) => x.score > -1)
        .sort((a, b) => b.score - a.score)
        .map((x) => x.it);
    }
    OVERLAY.items = items.slice(0, 80);
    if (OVERLAY.index >= OVERLAY.items.length) OVERLAY.index = 0;
    list.innerHTML = '';
    OVERLAY.items.forEach((it, i) => {
      const row = document.createElement('div');
      row.className = 'oitem' + (i === OVERLAY.index ? ' sel' : '');
      row.innerHTML = `<span class="okind">${it.kind}</span><span>${esc(it.label)}</span>${it.path ? `<span class="opath">${esc(it.path)}</span>` : ''}`;
      row.addEventListener('mouseenter', () => { OVERLAY.index = i; renderOverlaySelection(); });
      row.addEventListener('click', () => { closeOverlay(); it.run(); });
      list.appendChild(row);
    });
    renderOverlaySelection();
  }

  function renderOverlaySelection() {
    $$('#overlayList .oitem').forEach((el, i) => el.classList.toggle('sel', i === OVERLAY.index));
    const sel = $$('#overlayList .oitem')[OVERLAY.index];
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  }

  function overlayKey(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); OVERLAY.index = Math.min(OVERLAY.items.length - 1, OVERLAY.index + 1); renderOverlaySelection(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); OVERLAY.index = Math.max(0, OVERLAY.index - 1); renderOverlaySelection(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const it = OVERLAY.items[OVERLAY.index];
      if (it) { closeOverlay(); it.run(); }
    } else if (e.key === 'Escape') { e.preventDefault(); closeOverlay(); }
  }

  async function openQuickOpen() {
    openOverlay('files', 'Go to file by name…');
    if (!S.root) return;
    try {
      const data = await api.get('/api/files', { root: S.root });
      S.fileList = data.files;
      if (data.truncated) toast('file list truncated at 40k', 'err');
      renderOverlay();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  function openPalette() {
    loadTasks();
    openOverlay('commands', 'Type a command…');
  }

  const COMMANDS = [
    { label: 'File: Open Folder…', hint: 'Ctrl+K Ctrl+O', run: () => browseModal() },
    { label: 'File: Save', hint: 'Ctrl+S', run: saveActive },
    { label: 'File: Save All', hint: 'Ctrl+Shift+S', run: saveAll },
    { label: 'File: New File…', run: () => createEntry('', false) },
    { label: 'File: New Folder…', run: () => createEntry('', true) },
    { label: 'View: Explorer', hint: 'Ctrl+Shift+E', run: () => setView('explorer') },
    { label: 'View: Search', hint: 'Ctrl+Shift+F', run: () => setView('search') },
    { label: 'View: Source Control', hint: 'Ctrl+Shift+G', run: refreshGit },
    { label: 'View: Chat Dock', hint: 'Ctrl+J', run: () => setView('ai') },
    { label: 'View: Toggle Sidebar', hint: 'Ctrl+B', run: toggleSidebar },
    { label: 'View: Toggle Panel', hint: 'Ctrl+`', run: () => showPanel(!S.settings.panel) },
    { label: 'View: Toggle Minimap', run: () => toggleSetting('minimap') },
    { label: 'View: Toggle Word Wrap', hint: 'Alt+Z', run: () => toggleSetting('wordWrap') },
    { label: 'View: Toggle Autosave', run: () => toggleSetting('autosave') },
    { label: 'Terminal: New', run: () => startTerminal(S.root || undefined) },
    { label: 'Terminal: Clear', run: () => { $('#termOut').textContent = ''; } },
    { label: 'Terminal: Interrupt', run: () => S.term.id && api.post('/api/term/interrupt', { id: S.term.id }).catch(() => {}) },
    { label: 'Run: Active File', hint: 'F5', run: () => S.active && runFile(activeTab()?.path) },
    { label: 'Debug: Python File', run: debugActiveFile },
    { label: 'Git: Refresh Status', run: refreshGit },
    { label: 'Git: Commit Staged…', run: () => { setView('scm'); $('#gitMessage').focus(); } },
    { label: 'Chat: Toggle Settings', run: () => $('#chatSettings').classList.toggle('hidden') },
    { label: 'Chat: Load Local Model', run: loadLocalModel },
    { label: 'Chat: Fetch API Models', run: fetchChatModels },
    { label: 'Chat: Switch Provider', run: () => setProvider(S.ai.provider === 'api' ? 'local' : 'api') },
    { label: 'Chat: Reset Worker', run: resetChat },
    { label: 'Editor: Find', hint: 'Ctrl+F', run: () => editor.trigger('p', 'actions.find') },
    { label: 'Editor: Go to Line', hint: 'Ctrl+G', run: () => editor.trigger('p', 'editor.action.gotoLine') },
    { label: 'Editor: Format Document', hint: 'Shift+Alt+F', run: () => editor.trigger('p', 'editor.action.formatDocument') },
    { label: 'Editor: Markdown Preview', run: toggleMdPreview },
    { label: 'Tools: Database Query…', run: dbModal },
    { label: 'Tools: HTTP Request…', run: httpModal },
    { label: 'View: Toggle Light Theme', run: toggleTheme },
    { label: 'Help: Keyboard Shortcuts', run: showShortcuts },
    { label: 'Reload Window', run: () => location.reload() },
  ];

  /* --------------------------------- modal --------------------------------- */
  let modalResolve = null;

  function openModal({ title, body, ok = 'OK', cancel = 'Cancel', danger = false }) {
    return new Promise((resolve) => {
      modalResolve = resolve;
      $('#modalTitle').textContent = title;
      $('#modalBody').innerHTML = body;
      const okBtn = $('#modalOk');
      okBtn.textContent = ok;
      okBtn.className = 'btn ' + (danger ? 'danger' : 'primary');
      $('#modalCancel').textContent = cancel;
      $('#modalWrap').classList.remove('hidden');
      const first = $('#modalBody input, #modalBody textarea, #modalBody .brow');
      if (first) setTimeout(() => first.focus(), 20);
      okBtn.onclick = () => {
        const inputs = $$('#modalBody input');
        const val = inputs.length ? inputs[0].value : true;
        finishModal(val);
      };
      $('#modalCancel').onclick = () => finishModal(null);
      $('#modalWrap').onclick = (e) => { if (e.target === $('#modalWrap')) finishModal(null); };
    });
  }

  function finishModal(value) {
    $('#modalWrap').classList.add('hidden');
    if (modalResolve) { const r = modalResolve; modalResolve = null; r(value); }
  }

  async function promptModal(title, value, placeholder) {
    const res = await openModal({
      title,
      body: `<input class="input" id="modalInput" value="${esc(value)}" placeholder="${esc(placeholder || '')}" spellcheck="false" />`,
    });
    return res == null ? null : String(res).trim();
  }

  async function confirmModal(title, message, okLabel, danger = false) {
    const res = await openModal({
      title,
      body: `<div>${esc(message)}</div>`,
      ok: okLabel || 'Confirm',
      danger,
    });
    return res !== null && res !== false;
  }

  async function browseModal(startPath) {
    if (isDesktop()) {
      try {
        const picked = await ncall('pick_folder', startPath || S.root || '');
        if (picked) return openProject(picked);
        return null;
      } catch (err) {
        toast(`folder picker failed: ${err.message}`, 'err');
      }
    }
    let current = null;
    const body = document.createElement('div');
    body.innerHTML = `
      <div class="row gap6">
        <button class="btn" id="browseUp" title="Parent">↑</button>
        <input class="input grow" id="browsePath" spellcheck="false" />
      </div>
      <div class="browse-list" id="browseList"></div>
      <div class="meta small" id="browseHint">Pick a folder, then Open.</div>`;
    const promise = openModal({ title: 'Open Folder', body: body.innerHTML, ok: 'Open', cancel: 'Cancel' });

    const load = async (p) => {
      try {
        const data = await fetch('/api/browse?path=' + encodeURIComponent(p),
          { headers: api.headers() }).then((r) => r.json());
        if (data.ok === false) { $('#browseHint').textContent = data.error; return; }
        current = data.path;
        $('#browsePath').value = data.path;
        const list = $('#browseList');
        list.innerHTML = '';
        const up = document.createElement('div');
        up.className = 'brow';
        up.innerHTML = '<span class="up">↑ ..</span>';
        up.addEventListener('click', () => load(data.parent));
        list.appendChild(up);
        for (const d of data.entries.filter((x) => x.isDir)) {
          const row = document.createElement('div');
          row.className = 'brow';
          row.textContent = '📁 ' + d.name;
          row.addEventListener('click', () => load(d.path));
          list.appendChild(row);
        }
      } catch (err) {
        $('#browseHint').textContent = err.message;
      }
    };

    setTimeout(() => {
      load(startPath || S.root || S.env.home || S.env.cwd);
      $('#browseUp').onclick = () => {
        const p = $('#browsePath').value;
        load(p.replace(/[\\/][^\\/]+[\\/]?$/, '') || p);
      };
      $('#browsePath').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); load($('#browsePath').value); }
      });
    }, 30);

    const result = await promise;
    if (result === null) return;
    if (typeof result === 'string' && result.trim()) return openProject(result.trim());
    return null;
  }

  /* ------------------------------- shortcuts ------------------------------- */
  function showShortcuts() {
    const rows = [
      ['Ctrl P', 'go to file'], ['Ctrl Shift P', 'command palette'],
      ['Ctrl Shift F', 'search in files'], ['Ctrl Shift E', 'explorer'],
      ['Ctrl Shift G', 'source control'], ['Ctrl B', 'toggle sidebar'],
      ['Ctrl J', 'chat dock'], ['Ctrl `', 'terminal panel'], ['Ctrl S', 'save'],
      ['Ctrl W', 'close editor'], ['F5', 'run active file'],
      ['F12', 'go to definition'], ['Shift Alt F', 'format document'],
    ];
    openModal({
      title: 'Keyboard shortcuts',
      ok: 'Close',
      cancel: 'Close',
      body: `<ul class="keys">${rows.map(([k, v]) =>
        `<li><kbd>${k}</kbd><span>${v}</span></li>`).join('')}</ul>`,
    }).then(() => {});
  }

  /* ------------------------------ LSP bridge ------------------------------- */
  const LSP_LANGS = ['python', 'typescript', 'javascript', 'go', 'rust',
                     'json', 'css', 'html', 'yaml'];
  const lspRegistered = new Set();

  function relFromModel(model) {
    if (!model || !S.root) return '';
    const p = decodeURIComponent(model.uri.path).replace(/\\/g, '/');
    const root = S.root.replace(/\\/g, '/').replace(/\/$/, '');
    if (p.toLowerCase().startsWith(root.toLowerCase() + '/')) {
      return p.slice(root.length + 1);
    }
    return p.replace(/^\//, '');
  }

  async function lspSend(kind, tab, extra = {}) {
    if (!tab || tab.kind !== 'file' || !tab.model || !S.root) return null;
    try {
      return await api.post(`/api/lsp/${kind}`, {
        root: S.root, path: tab.path, text: tab.model.getValue(), ...extra,
      });
    } catch { return null; }
  }

  async function lspDiagnostics(tab) {
    if (!tab || tab.kind !== 'file' || !tab.model || !S.root) return;
    try {
      const res = await api.get('/api/lsp/diagnostics', { root: S.root, path: tab.path });
      if (!res.ok) return;
      const markers = (res.diagnostics || []).map((d) => {
        const sev = d.severity || 1;
        return {
          severity: sev === 1 ? monaco.MarkerSeverity.Error
            : sev === 2 ? monaco.MarkerSeverity.Warning
            : sev === 3 ? monaco.MarkerSeverity.Info : monaco.MarkerSeverity.Hint,
          message: d.message || '',
          startLineNumber: ((d.range?.start?.line ?? 0) || 0) + 1,
          startColumn: ((d.range?.start?.character ?? 0) || 0) + 1,
          endLineNumber: ((d.range?.end?.line ?? 0) || 0) + 1,
          endColumn: ((d.range?.end?.character ?? 0) || 0) + 1,
          source: d.source || 'lsp',
        };
      });
      monaco.editor.setModelMarkers(tab.model, 'lsp', markers);
    } catch { /* no server */ }
  }

  function registerLspProviders() {
    for (const lang of LSP_LANGS) {
      if (lspRegistered.has(lang)) continue;
      lspRegistered.add(lang);
      monaco.languages.registerHoverProvider(lang, {
        async provideHover(model, pos) {
          const rel = relFromModel(model);
          if (!rel || !S.root) return null;
          try {
            const res = await api.get('/api/lsp/hover', {
              root: S.root, path: rel, line: pos.lineNumber, col: pos.column,
            });
            const c = res.result && res.result.contents;
            if (!res.ok || !c) return null;
            const val = typeof c === 'string' ? c
              : (c.value || (Array.isArray(c) ? c.join('\n') : ''));
            return val ? { contents: [{ value: val }] } : null;
          } catch { return null; }
        },
      });
      monaco.languages.registerCompletionItemProvider(lang, {
        async provideCompletionItems(model, pos) {
          const rel = relFromModel(model);
          if (!rel || !S.root) return { suggestions: [] };
          try {
            const res = await api.get('/api/lsp/completion', {
              root: S.root, path: rel, line: pos.lineNumber, col: pos.column,
            });
            const items = Array.isArray(res.result) ? res.result
              : (res.result && res.result.items) || [];
            const word = model.getWordUntilPosition(pos);
            const range = new monaco.Range(pos.lineNumber, word.startColumn,
              pos.lineNumber, word.endColumn);
            return {
              suggestions: items.slice(0, 400).map((i) => ({
                label: i.label,
                kind: monaco.languages.CompletionItemKind.Function,
                insertText: i.insertText || i.label,
                detail: i.detail || '',
                range,
              })),
            };
          } catch { return { suggestions: [] }; }
        },
      });
      monaco.languages.registerDefinitionProvider(lang, {
        async provideDefinition(model, pos) {
          const rel = relFromModel(model);
          if (!rel || !S.root) return null;
          try {
            const res = await api.get('/api/lsp/definition', {
              root: S.root, path: rel, line: pos.lineNumber, col: pos.column,
            });
            const r = res.result;
            if (!res.ok || !r) return null;
            const locs = Array.isArray(r) ? r : [r];
            return locs.filter(Boolean).map((loc) => {
              const st = (loc.range && loc.range.start) || {};
              return {
                uri: monaco.Uri.parse(loc.uri || loc.targetUri),
                range: new monaco.Range((st.line || 0) + 1, (st.character || 0) + 1,
                  (st.line || 0) + 1, (st.character || 0) + 12),
              };
            });
          } catch { return null; }
        },
      });
    }
  }

  /* ---------------------------- markdown preview ---------------------------- */
  function toggleMdPreview() {
    const tab = activeTab();
    if (!tab || tab.kind !== 'file' ||
        !/\.(md|markdown|mdx)$/i.test(tab.path || '')) {
      return toast('open a markdown file first', 'err');
    }
    S.preview = !S.preview;
    updatePreview();
  }

  function updatePreview() {
    const host = $('#mdPreview');
    if (!host) return;
    const tab = activeTab();
    const show = S.preview && tab && tab.kind === 'file' &&
      tab.model && /\.(md|markdown|mdx)$/i.test(tab.path || '');
    host.classList.toggle('hidden', !show);
    $('#editor').classList.toggle('hidden', !!show);
    if (show && self.vantaMd) {
      host.innerHTML = self.vantaMd.render(tab.model.getValue());
    }
  }

  /* ------------------------------- db client -------------------------------- */
  function dbModal() {
    if (!S.root) return toast('open a folder first', 'err');
    const body = `
      <div class="row gap6">
        <input class="input grow" id="dbPath" placeholder="database file (e.g. data/app.db)" spellcheck="false" />
        <button class="btn" id="dbRun">Run</button>
      </div>
      <textarea class="input" id="dbSql" rows="4" spellcheck="false" placeholder="SELECT name FROM sqlite_master WHERE type='table'"></textarea>
      <div id="dbOut" class="modal-out"></div>`;
    openModal({ title: 'Database (SQLite, read-only)', body, ok: 'Close', cancel: 'Close' });
    setTimeout(() => {
      $('#dbRun').addEventListener('click', async () => {
        const out = $('#dbOut');
        out.textContent = 'running…';
        try {
          const res = await api.post('/api/db/query', {
            root: S.root, path: $('#dbPath').value.trim(),
            sql: $('#dbSql').value.trim(),
          });
          if (!res.rows.length) { out.textContent = '(no rows)'; return; }
          const head = `<tr>${res.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr>`;
          const rows = res.rows.map((r) =>
            `<tr>${r.map((v) => `<td>${esc(String(v ?? ''))}</td>`).join('')}</tr>`).join('');
          out.innerHTML = `<div class="meta small">${res.rows.length} rows`
            + `${res.truncated ? ' (truncated)' : ''}</div>`
            + `<div class="table-wrap"><table>${head}${rows}</table></div>`;
        } catch (err) {
          out.textContent = err.message;
        }
      });
    }, 30);
  }

  /* ------------------------------ http client ------------------------------- */
  function httpModal() {
    const body = `
      <div class="row gap6">
        <select id="httpMethod" class="input" style="width:90px">
          <option>GET</option><option>POST</option><option>PUT</option>
          <option>PATCH</option><option>DELETE</option>
        </select>
        <input class="input grow" id="httpUrl" placeholder="https://…" spellcheck="false" />
        <button class="btn" id="httpRun">Send</button>
      </div>
      <textarea class="input" id="httpBody" rows="3" spellcheck="false" placeholder="request body (json) — optional"></textarea>
      <div id="httpOut" class="modal-out"></div>`;
    openModal({ title: 'HTTP request', body, ok: 'Close', cancel: 'Close' });
    setTimeout(() => {
      $('#httpRun').addEventListener('click', async () => {
        const out = $('#httpOut');
        out.textContent = 'sending…';
        const raw = $('#httpBody').value.trim();
        try {
          const res = await api.post('/api/http', {
            method: $('#httpMethod').value,
            url: $('#httpUrl').value.trim(),
            body: raw || undefined,
          });
          let pretty = res.body || '';
          try { pretty = JSON.stringify(JSON.parse(pretty), null, 2); } catch { /* raw */ }
          out.innerHTML = `<div class="meta small">${res.status} · ${res.ms} ms</div>`
            + `<pre class="modal-pre">${esc(pretty.slice(0, 4000))}</pre>`;
        } catch (err) {
          out.textContent = err.message;
        }
      });
    }, 30);
  }

  /* --------------------------------- tasks ---------------------------------- */
  async function loadTasks() {
    try {
      const res = await api.get('/api/tasks', { root: S.root || '' });
      S.tasks = res.tasks || [];
    } catch { S.tasks = []; }
  }

  function runTask(task) {
    const cmd = task.command || task.cmd || task.run;
    if (!cmd) return toast('task has no command', 'err');
    sendCmdToTerm(cmd);
    showPanel(true);
    switchPanelTab('terminal');
  }

  /* ------------------------------- session ---------------------------------- */
  async function restoreSession() {
    if (!S.root) return;
    try {
      const res = await api.get('/api/session/load', { root: S.root });
      const sess = res.session || {};
      const paths = (sess.tabs || []).slice(0, 12).map((t) => t.path).filter(Boolean);
      let last = null;
      for (const p of paths) {
        try { await openFile(p); last = p + '@file'; } catch { /* gone */ }
      }
      if (sess.active && S.tabs.some((t) => t.key === sess.active)) {
        activateTab(sess.active);
      } else if (last && sess.tabs.length) {
        activateTab(last);
      }
      if (paths.length) toast(`restored ${paths.length} tab(s)`, 'ok');
    } catch { /* none */ }
  }

  /* ----------------------------- ollama manager ----------------------------- */
  async function loadOllamaModels() {
    const list = $('#ollamaList');
    if (!list) return;
    try {
      const res = await api.get('/api/ollama/tags', { base: $('#chatApiBase').value.trim() });
      if (!res.ok) { list.innerHTML = `<div class="side-empty">${esc(res.error)}</div>`; return; }
      list.innerHTML = (res.models || []).map((m) => `
        <div class="change-row" data-model="${esc(m.name)}">
          <span class="p">${esc(m.name)}</span>
          <button class="icon-btn ollama-del" title="Delete model">✕</button>
        </div>`).join('') || '<div class="side-empty">no models</div>';
    } catch (err) {
      list.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
    }
  }

  async function pullOllamaModel() {
    const model = $('#ollamaPull').value.trim();
    if (!model) return toast('model name required', 'err');
    const btn = $('#btnOllamaPull');
    btn.disabled = true;
    btn.textContent = 'pulling…';
    try {
      const res = await api.post('/api/ollama/pull',
        { base: $('#chatApiBase').value.trim(), model });
      toast(`pulled ${model}`, 'ok');
      if (res.status) $('#chatHint').textContent = `ollama: ${res.status}`;
      loadOllamaModels();
      fetchChatModels();
    } catch (err) {
      toast(`pull failed: ${err.message}`, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = '⇣ Pull';
    }
  }

  async function deleteOllamaModel(model) {
    const ok = await confirmModal('Delete model', `Remove ${model} from the VPS?`, 'Delete', true);
    if (!ok) return;
    try {
      await api.post('/api/ollama/delete', { base: $('#chatApiBase').value.trim(), model });
      toast(`deleted ${model}`, 'ok');
      loadOllamaModels();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  /* --------------------------------- gh prs --------------------------------- */
  async function loadPrs() {
    if (!S.root) return toast('open a folder first', 'err');
    const host = $('#gitPRs');
    host.innerHTML = '<div class="side-empty">loading…</div>';
    try {
      const res = await api.get('/api/gh/prs', { root: S.root });
      if (!res.ok) { host.innerHTML = `<div class="side-empty">${esc(res.error)}</div>`; return; }
      if (!(res.prs || []).length) { host.innerHTML = '<div class="side-empty">no open PRs</div>'; return; }
      host.innerHTML = res.prs.map((pr) => `
        <div class="change-row" data-pr="${pr.number}">
          <span class="code">#${pr.number}</span>
          <span class="p">${esc(pr.title)}${pr.isDraft ? ' (draft)' : ''}</span>
        </div>`).join('');
    } catch (err) {
      host.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
    }
  }

  async function checkoutPr(number) {
    try {
      await api.post('/api/gh/pr/checkout', { root: S.root, number });
      toast(`checked out PR #${number}`, 'ok');
      S.treeCache.clear();
      await refreshTree();
      refreshGit();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  /* ------------------------------- dictation -------------------------------- */
  let mediaRec = null;

  async function toggleDictation() {
    if (mediaRec && mediaRec.state === 'recording') {
      mediaRec.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks = [];
      mediaRec = new MediaRecorder(stream);
      mediaRec.ondataavailable = (e) => chunks.push(e.data);
      mediaRec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        $('#btnChatMic').style.color = '';
        const blob = new Blob(chunks, { type: 'audio/webm' });
        const b64 = await new Promise((res) => {
          const r = new FileReader();
          r.onload = () => res(String(r.result).split(',')[1] || '');
          r.readAsDataURL(blob);
        });
        $('#chatState').textContent = 'transcribing…';
        try {
          const out = await api.post('/api/ai/transcribe', { audio: b64 });
          if (out.text) {
            const inp = $('#chatInput');
            inp.value = (inp.value ? inp.value + ' ' : '') + out.text;
            inp.focus();
          }
        } catch (err) {
          toast(err.message, 'err');
        } finally {
          $('#chatState').textContent = 'idle';
        }
      };
      mediaRec.start();
      $('#btnChatMic').style.color = 'var(--danger)';
      toast('recording — click again to stop', 'ok');
    } catch (err) {
      toast(`mic: ${err.message}`, 'err');
    }
  }

  /* --------------------------- image attachments ---------------------------- */
  function renderAttachments() {
    const host = $('#chatAttachments');
    host.classList.toggle('hidden', !S.attachments.length);
    host.innerHTML = S.attachments.map((a, i) =>
      `<span class="chip"><img src="${a}" alt="img"/>`
      + `<button data-i="${i}" title="Remove">✕</button></span>`).join('');
  }

  function addAttachmentDataUrl(dataUrl) {
    if (S.ai.provider !== 'api') {
      toast('images ride along on the api provider only', 'err');
      return;
    }
    S.attachments.push(dataUrl);
    renderAttachments();
  }

  function bindAttachments() {
    $('#btnChatMic').addEventListener('click', toggleDictation);
    $('#btnChatAttach').addEventListener('click', () => $('#chatImageInput').click());    $('#chatImageInput').addEventListener('change', () => {
      const f = $('#chatImageInput').files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => addAttachmentDataUrl(r.result);
      r.readAsDataURL(f);
      $('#chatImageInput').value = '';
    });
    $('#chatInput').addEventListener('paste', (e) => {
      for (const it of (e.clipboardData && e.clipboardData.items) || []) {
        if (it.type && it.type.startsWith('image/')) {
          e.preventDefault();
          const f = it.getAsFile();
          if (!f) continue;
          const r = new FileReader();
          r.onload = () => addAttachmentDataUrl(r.result);
          r.readAsDataURL(f);
        }
      }
    });
    $('#chatAttachments').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-i]');
      if (!btn) return;
      S.attachments.splice(Number(btn.dataset.i), 1);
      renderAttachments();
    });
  }

  /* -------------------------------- debugger -------------------------------- */
  function bpLines(path) { return S.breakpoints[path] || []; }

  function toggleBreakpoint(path, line) {
    if (!path) return;
    const list = new Set(bpLines(path));
    if (list.has(line)) list.delete(line); else list.add(line);
    S.breakpoints[path] = [...list].sort((a, b) => a - b);
    if (!S.breakpoints[path].length) delete S.breakpoints[path];
    paintBps();
    api.post('/api/debug/breakpoints', { root: S.root, path, lines: bpLines(path) })
      .catch(() => {});
  }

  function paintBps() {
    const tab = activeTab();
    if (!tab || tab.kind !== 'file' || !tab.model) return;
    const lines = bpLines(tab.path);
    const old = tab._bpDecos || [];
    tab._bpDecos = lines.length ? tab.model.deltaDecorations(old, lines.map((l) => ({
      range: new monaco.Range(l, 1, l, 1),
      isWholeLine: false,
      linesDecorationsClassName: 'bp-dot',
      className: 'bp-line',
    }))) : tab.model.deltaDecorations(old, []);
    const stopFrame = S.dbg.frames.find((f) => f.file && tab.path &&
      f.file.toLowerCase().endsWith(tab.path.toLowerCase()));
    const cur = tab._dbgDeco ? tab.model.deltaDecorations(tab._dbgDeco, []) : [];
    tab._dbgDeco = stopFrame && S.dbg.state === 'stopped'
      ? tab.model.deltaDecorations(cur, [{
          range: new monaco.Range(stopFrame.line, 1, stopFrame.line, 1),
          isWholeLine: true, className: 'dbg-line',
        }]) : cur;
  }

  async function debugActiveFile() {
    const tab = activeTab();
    if (!tab || tab.kind !== 'file') return toast('open a file first', 'err');
    if (!/\.(py|pyw)$/i.test(tab.path)) return toast('debugger runs .py files', 'err');
    if (tab.dirty) await saveTab(tab);
    try {
      const res = await api.post('/api/debug/start', { root: S.root, script: tab.path });
      S.dbg.active = true;
      S.dbg.cursor = 0;
      S.dbg.frames = [];
      S.dbg.state = 'starting';
      $('#dbgOutput').textContent = '';
      showPanel(true);
      switchPanelTab('debug');
      for (const [p, lines] of Object.entries(S.breakpoints)) {
        api.post('/api/debug/breakpoints', { root: S.root, path: p, lines }).catch(() => {});
      }
      paintBps();
      toast(`debugging ${tab.name}`, 'ok');
    } catch (err) {
      toast(`debug: ${err.message}`, 'err');
    }
  }

  async function pollDebug() {
    if (!S.dbg.active) return;
    let res;
    try {
      res = await api.get('/api/debug/poll', { cursor: S.dbg.cursor });
    } catch { return; }
    for (const ev of res.events || []) {
      S.dbg.cursor = ev.seq;
      if (ev.type === 'stopped') {
        S.dbg.state = 'stopped';
        await refreshDebug();
      } else if (ev.type === 'continued') {
        S.dbg.state = 'running';
        S.dbg.frames = [];
        S.dbg.vars = [];
        paintBps();
        paintDebug();
      } else if (ev.type === 'output' && ev.category !== 'telemetry') {
        const out = $('#dbgOutput');
        out.textContent += ev.text;
        out.scrollTop = out.scrollHeight;
      } else if (ev.type === 'session' || ev.type === 'terminated' || ev.type === 'exited') {
        S.dbg.state = ev.state || 'terminated';
        if (S.dbg.state === 'idle' || S.dbg.state === 'terminated') {
          S.dbg.active = S.dbg.state === 'terminated';
          S.dbg.frames = [];
        }
        paintDebug();
      } else if (ev.type === 'error') {
        toast(ev.msg, 'err');
      }
    }
    S.dbg.state = res.state || S.dbg.state;
    paintDebug();
  }

  async function refreshDebug() {
    try {
      const res = await api.get('/api/debug/stack');
      S.dbg.frames = res.frames || [];
      const top = S.dbg.frames.find((f) => !f.file.includes('runpy')) || S.dbg.frames[0];
      S.dbg.sel = top ? top.id : null;
      paintBps();
      if (top) {
        const vars = await api.get('/api/debug/variables', { frame: top.id });
        const locals = (vars.scopes || []).find((s) => s.name === 'Locals');
        if (locals) {
          const kids = await api.get('/api/debug/variables',
            { ref: locals.variablesReference });
          S.dbg.vars = kids.variables || [];
        } else S.dbg.vars = [];
      } else S.dbg.vars = [];
    } catch { /* gone */ }
    paintDebug();
  }

  function paintDebug() {
    const bar = $('#dbgBar');
    if (!bar) return;
    bar.classList.toggle('hidden', !S.dbg.active);
    $('#dbgState').textContent = S.dbg.active
      ? `debug · ${S.dbg.state}${S.dbg.sel ? ' · frame ' + S.dbg.sel : ''}`
      : '';
    const stack = $('#dbgStack');
    stack.innerHTML = S.dbg.frames.map((f, i) => `
      <div class="dbg-row${f.id === S.dbg.sel ? ' sel' : ''}" data-frame="${f.id}">
        <span class="dbg-name">${esc(f.name)}</span>
        <span class="dbg-loc">${esc(f.file ? f.file.split(/[\\/]/).pop() : '')}:${f.line}</span>
      </div>`).join('') || '<div class="side-empty">no frames</div>';
    const vars = $('#dbgVars');
    vars.innerHTML = S.dbg.vars.map((v) => `
      <div class="dbg-row" data-ref="${v.variablesReference}">
        <span class="dbg-name">${esc(v.name)}</span>
        <span class="dbg-val" title="${esc(v.type || '')}">${esc(v.value)}</span>
      </div>`).join('')
      || '<div class="side-empty">' + (S.dbg.state === 'stopped' ? 'no locals' : 'paused? no — running') + '</div>';
  }

  async function dbgExpand(ref, rowEl) {
    const res = await api.get('/api/debug/variables', { ref }).catch(() => null);
    if (!res || !res.variables) return;
    const indent = rowEl.style.paddingLeft || '10px';
    const html = res.variables.map((v) => `
      <div class="dbg-row" data-ref="${v.variablesReference}"
           style="padding-left: calc(${indent} + 12px)">
        <span class="dbg-name">${esc(v.name)}</span>
        <span class="dbg-val">${esc(v.value)}</span>
      </div>`).join('');
    rowEl.insertAdjacentHTML('afterend', html);
    rowEl.dataset.ref = '0';
  }

  async function dbgEvaluate() {
    const expr = $('#dbgExpr').value.trim();
    if (!expr || !S.dbg.sel) return;
    $('#dbgExpr').value = '';
    try {
      const res = await api.post('/api/debug/evaluate', { expr, frame: S.dbg.sel });
      const out = $('#dbgOutput');
      out.textContent += `› ${expr}\n${res.result}\n`;
      out.scrollTop = out.scrollHeight;
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function dbgCmd(cmd) {
    try {
      await api.post('/api/debug/cmd', { cmd });
      if (cmd === 'stop') { S.dbg.active = false; S.dbg.state = 'idle'; }
      else S.dbg.state = 'running';
      paintDebug();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  function bindDebugger() {
    $('#dbgContinue').addEventListener('click', () => dbgCmd('continue'));
    $('#dbgNext').addEventListener('click', () => dbgCmd('next'));
    $('#dbgStepIn').addEventListener('click', () => dbgCmd('stepIn'));
    $('#dbgStepOut').addEventListener('click', () => dbgCmd('stepOut'));
    $('#dbgStop').addEventListener('click', () => dbgCmd('stop'));
    $('#dbgExpr').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); dbgEvaluate(); }
    });
    $('#dbgStack').addEventListener('click', async (e) => {
      const row = e.target.closest('[data-frame]');
      if (!row) return;
      S.dbg.sel = Number(row.dataset.frame);
      const vars = await api.get('/api/debug/variables', { frame: S.dbg.sel }).catch(() => null);
      const locals = vars && (vars.scopes || []).find((s) => s.name === 'Locals');
      S.dbg.vars = [];
      if (locals) {
        const kids = await api.get('/api/debug/variables', { ref: locals.variablesReference });
        S.dbg.vars = kids.variables || [];
      }
      paintDebug();
    });
    $('#dbgVars').addEventListener('click', (e) => {
      const row = e.target.closest('[data-ref]');
      if (!row) return;
      const ref = Number(row.dataset.ref);
      if (ref > 0) dbgExpand(ref, row);
    });
  }

  /* ------------------------------- remotes ---------------------------------- */
  async function loadRemotes() {
    const host = $('#remoteList');
    if (!host) return;
    try {
      const res = await api.get('/api/remotes/list', { root: S.root || '' });
      if (!res.ok) return;
      S.remotes = res;
      const rows = [];
      for (const w of res.wsl || []) {
        rows.push(`<div class="change-row" data-kind="wsl" data-distro="${esc(w.name)}">
          <span class="p">WSL · ${esc(w.name)}</span></div>`);
      }
      for (const h of res.ssh || []) {
        rows.push(`<div class="change-row" data-kind="ssh" data-id="${esc(h.id)}">
          <span class="p">SSH · ${esc(h.name || h.host)}</span></div>`);
      }
      host.innerHTML = rows.join('') || '<div class="side-empty">none — add .vanta\\remotes.json</div>';
    } catch { /* none */ }
  }

  function openRemoteShell(kind, distro, id) {
    if (kind === 'wsl') sendCmdToTerm(`wsl.exe -d "${distro}"`);
    else sendCmdToTerm(`ssh ${id}`);
    showPanel(true);
    switchPanelTab('terminal');
  }

  async function sshBrowseModal(id, startPath = '.') {
    let cwd = startPath;
    const body = `
      <div class="row gap6">
        <button class="btn" id="rUp" title="Parent">↑</button>
        <input class="input grow" id="rPath" spellcheck="false" />
        <button class="btn" id="rGo" title="Go">→</button>
      </div>
      <div class="browse-list" id="rList"></div>
      <div class="meta small" id="rHint">Click a file to edit remotely.</div>`;
    openModal({ title: `SSH · ${id}`, body, ok: 'Close', cancel: 'Close' });
    const load = async (p) => {
      try {
        const res = await api.post('/api/remotes/ls', { id, root: S.root || '', path: p });
        if (!res.ok) { $('#rHint').textContent = res.error; return; }
        cwd = res.path;
        $('#rPath').value = cwd;
        const list = $('#rList');
        list.innerHTML = '';
        for (const e of res.entries || []) {
          const row = document.createElement('div');
          row.className = 'brow';
          row.textContent = (e.isDir ? '📁 ' : '📄 ') + e.name;
          row.addEventListener('click', async () => {
            const np = (cwd === '/' ? '' : cwd) + '/' + e.name;
            if (e.isDir) load(np);
            else openRemoteFile(id, np);
          });
          list.appendChild(row);
        }
      } catch (err) {
        $('#rHint').textContent = err.message;
      }
    };
    setTimeout(() => {
      load(cwd);
      $('#rUp').addEventListener('click', () => {
        load(cwd.replace(/\/[^/]+\/?$/, '') || '/');
      });
      $('#rGo').addEventListener('click', () => load($('#rPath').value.trim() || '.'));
      $('#rPath').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); load($('#rPath').value.trim()); }
      });
    }, 30);
  }

  async function openRemoteFile(id, path) {
    try {
      const res = await api.post('/api/remotes/read', { id, root: S.root || '', path });
      if (!res.ok) return toast(res.error, 'err');
      const name = path.split('/').pop();
      const key = `${id}:${path}@remote`;
      const old = S.tabs.find((t) => t.key === key);
      if (old) { activateTab(key); return; }
      const uri = monaco.Uri.parse(`vanta-remote:///${encodeURIComponent(id)}/${path}`);
      const prev = monaco.editor.getModel(uri);
      if (prev) prev.dispose();
      const model = monaco.editor.createModel(res.content, langOf(name), uri);
      model.updateOptions({ tabSize: 4, insertSpaces: true });
      const tab = {
        kind: 'remote', key, path, name: `${name} (ssh:${id})`, model,
        remote: { id, path }, dirty: false, viewState: null,
      };
      tab.savedContent = res.content;
      tab.model.onDidChangeContent(() => {
        if (!tab.dirty) { tab.dirty = true; renderTabs(); }
      });
      S.tabs.push(tab);
      activateTab(key);
      toast(`opened remote ${name}`, 'ok');
    } catch (err) {
      toast(`remote open: ${err.message}`, 'err');
    }
  }

  /* -------------------------------- plugins --------------------------------- */
  async function loadPlugins() {
    const host = $('#pluginList');
    if (!host) return;
    try {
      const res = await api.get('/api/plugins');
      host.innerHTML = (res.plugins || []).map((p) => `
        <div class="change-row"><span class="p">${esc(p.name)}</span>
        <span class="meta small">${p.ok ? '✓' : '✕ ' + esc(p.error || '')}</span></div>`).join('')
        || '<div class="side-empty">none — plugins/ or .vanta/plugins/</div>';
    } catch { /* none */ }
  }

  async function reloadPlugins() {
    try {
      await api.post('/api/plugins/reload', { root: S.root || '' });
      toast('plugins reloaded', 'ok');
      loadPlugins();
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  /* -------------------------------- updates --------------------------------- */
  async function checkForUpdates() {
    const body = `
      <div id="updInfo" class="meta small">checking…</div>
      <div id="updNotes" class="modal-out"></div>
      <div id="updActions" class="row gap6 hidden">
        <button class="btn primary grow" id="btnUpdStage">Download & stage</button>
      </div>
      <div id="updStage" class="meta small"></div>`;
    openModal({ title: 'Software Update', body, ok: 'Close', cancel: 'Close' });
    try {
      const info = await api.get('/api/update/check');
      if (!info.ok) { $('#updInfo').textContent = `check failed: ${info.error}`; return; }
      $('#updInfo').innerHTML = info.update_available
        ? `Version <b>${esc(info.latest)}</b> is available (you have ${esc(info.current)}).`
        : `Up to date · v${esc(info.current)}`;
      if (info.notes) $('#updNotes').innerHTML = `<pre class="modal-pre">${esc(String(info.notes).slice(0, 1500))}</pre>`;
      if (info.update_available) {
        $('#updActions').classList.remove('hidden');
        $('#btnUpdStage').addEventListener('click', async () => {
          $('#btnUpdStage').disabled = true;
          $('#updStage').textContent = 'downloading…';
          try {
            const staged = await api.post('/api/update/stage', {});
            if (!staged.ok) { $('#updStage').textContent = staged.error; return; }
            $('#updStage').innerHTML = 'staged. <b>Restart & install</b> will exit Vanta, swap the files, and relaunch.';
            $('#btnUpdStage').textContent = 'Staged ✓';
            const applyBtn = document.createElement('button');
            applyBtn.className = 'btn danger';
            applyBtn.textContent = 'Restart & install';
            $('#updActions').appendChild(applyBtn);
            applyBtn.addEventListener('click', async () => {
              applyBtn.disabled = true;
              await api.post('/api/update/apply', {});
              toast('updating — Vanta will restart', 'ok');
            });
          } catch (err) {
            $('#updStage').textContent = err.message;
          }
        });
      }
    } catch (err) {
      $('#updInfo').textContent = `check failed: ${err.message}`;
    }
  }

  /* ------------------------------ ai services ------------------------------- */
  let servicesCache = null;

  async function loadServices() {
    const sel = $('#chatService');
    if (!sel || servicesCache) return;
    try {
      const res = await api.get('/api/ai/services');
      servicesCache = res.services || [];
      sel.innerHTML = '<option value="">— choose —</option>' +
        servicesCache.map((s, i) => {
          const envTag = s.envSet ? ' ·key ✓env' : (s.envKey ? ' ·env ' + s.envKey : '');
          return `<option value="${i}">${esc(s.name)}${envTag}</option>`;
        }).join('');
    } catch { /* none */ }
  }

  function onServicePick() {
    const sel = $('#chatService');
    const i = Number(sel.value);
    const note = $('#serviceEnvNote');
    if (Number.isNaN(i) || !servicesCache[i]) {
      $('#chatServiceModel').classList.add('hidden');
      $('#btnServiceUse').classList.add('hidden');
      note.textContent = '';
      return;
    }
    const s = servicesCache[i];
    note.textContent = s.envKey
      ? (s.envSet ? `key found in ${s.envKey} — no need to paste it`
                  : `set ${s.envKey} to skip pasting a key`)
      : 'local server — start it, then Use';
    const ms = $('#chatServiceModel');
    if ((s.models || []).length) {
      ms.classList.remove('hidden');
      ms.innerHTML = s.models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
    } else ms.classList.add('hidden');
    $('#btnServiceUse').classList.remove('hidden');
  }

  function applyService() {
    const i = Number($('#chatService').value);
    if (Number.isNaN(i) || !servicesCache[i]) return;
    const s = servicesCache[i];
    $('#chatApiBase').value = s.baseUrl;
    if ((s.models || []).length) {
      const m = $('#chatServiceModel').value;
      if (!$('#chatApiModel').querySelector(`option[value="${CSS.escape(m)}"]`)) {
        $('#chatApiModel').insertAdjacentHTML('beforeend',
          `<option value="${esc(m)}">${esc(m)}</option>`);
      }
      $('#chatApiModel').value = m;
    }
    saveChatConfig({ api: { baseUrl: s.baseUrl, envKey: s.envKey || '' } });
    toast(`${s.name} endpoint set${s.envSet ? ' (key from env)' : ''}`, 'ok');
    loadOllamaModels();
  }

  /* -------------------------- local model fit labels ------------------------ */
  async function paintLocalFit() {
    const host = $('#localFit');
    if (!host) return;
    let freeGB = null;
    try {
      const hw = await api.get('/api/hardware');
      const g = (hw.gpu || [])[0];
      if (g && g.free) freeGB = parseFloat(String(g.free).replace(/[^\d.]/g, ''));
    } catch { /* none */ }
    if (freeGB == null) { host.textContent = ''; return; }
    try {
      const res = await api.get('/api/ai/models', { provider: 'local' });
      const meta = res.meta || {};
      const rows = [];
      for (const [id, m] of Object.entries(meta)) {
        const fp16 = m.paramsB * 2.0;
        const fit = fp16 * 1.15 <= freeGB ? 'fp16'
          : (fp16 / 3.5) * 1.15 <= freeGB ? '4-bit' : 'airllm';
        rows.push({ id, fit, note: m.note });
      }
      rows.sort((a, b) => (a.fit === 'fp16' ? 0 : a.fit === '4-bit' ? 1 : 2)
        - (b.fit === 'fp16' ? 0 : b.fit === '4-bit' ? 1 : 2));
      const cur = $('#chatLocalModel').value.trim();
      const hits = rows.filter((r) => r.fit !== 'airllm').slice(0, 6);
      host.innerHTML = `<b>${freeGB.toFixed(1)} GB free VRAM</b> — good now: `
        + hits.map((r) => `<span class="fit-chip${cur === r.id ? ' on' : ''}"
           data-model="${esc(r.id)}" title="${esc(r.note)}">${esc(base(r.id))} · ${r.fit}</span>`).join('');
    } catch { /* none */ }
  }

  /* ------------------------------- ide interop ------------------------------- */
  async function loadEditors() {
    const host = $('#editorList');
    if (!host) return;
    try {
      const res = await api.get('/api/ide/editors');
      host.innerHTML = (res.editors || []).map((e, i) => `
        <div class="change-row" data-i="${i}">
          <span class="p">${esc(e.name)}</span>
          <span class="meta small">open ↗</span>
        </div>`).join('') || '<div class="side-empty">none detected</div>';
      host.dataset.payload = JSON.stringify(res.editors || []);
    } catch { /* none */ }
  }

  async function openInEditor(i) {
    const editors = JSON.parse($('#editorList').dataset.payload || '[]');
    const e = editors[i];
    if (!e) return;
    const tab = activeTab();
    try {
      const res = await api.post('/api/ide/open', {
        exe: e.exe, root: S.root || '',
        path: tab && tab.kind === 'file' ? tab.path : '',
        line: tab && tab.kind === 'file' && editor ? editor.getPosition().lineNumber : 0,
      });
      if (res.ok) toast(`opened in ${e.name}`, 'ok');
      else toast(res.error, 'err');
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  async function importVscode() {
    try {
      const res = await api.get('/api/ide/import-settings');
      const profiles = res.profiles || [];
      if (!profiles.length) { toast('no VS Code-family settings found', 'err'); return; }
      const p = profiles[0];
      if (p.fontSize) {
        S.settings.fontSize = Math.min(28, Math.max(9, p.fontSize));
        editor.updateOptions({ fontSize: S.settings.fontSize });
        localStorage.setItem('vanta.fontSize', String(S.settings.fontSize));
      }
      if (p.minimap !== null && p.minimap !== undefined) {
        S.settings.minimap = !!p.minimap;
        editor.updateOptions({ minimap: { enabled: S.settings.minimap } });
        localStorage.setItem('vanta.minimap', S.settings.minimap ? '1' : '0');
      }
      if (p.wordWrap !== null && p.wordWrap !== undefined) {
        S.settings.wordWrap = !!p.wordWrap;
        editor.updateOptions({ wordWrap: S.settings.wordWrap ? 'on' : 'off' });
        localStorage.setItem('vanta.wordWrap', S.settings.wordWrap ? '1' : '0');
      }
      await api.get('/api/ide/import-keybindings');
      keyOverrides = {};
      try {
        const kb = await api.get('/api/config/keybindings', { root: '' });
        keyOverrides = kb.keybindings || {};
      } catch { /* none */ }
      const names = profiles.map((x) => x.source).join(', ');
      toast(`imported from ${names}: font ${p.fontSize || 'n/a'}`
        + `${p.theme ? ' · theme "' + p.theme + '"' : ''} · keybindings`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  let snippetProviders = false;
  async function importSnippets() {
    try {
      const res = await api.get('/api/ide/snippets');
      if (!res.total) { toast('no VS Code snippets found', 'err'); return; }
      if (!snippetProviders) {
        snippetProviders = true;
        for (const [lang, items] of Object.entries(res.languages || {})) {
          monaco.languages.registerCompletionItemProvider(lang, {
            provideCompletionItems(model, pos) {
              const word = model.getWordUntilPosition(pos);
              return { suggestions: items.slice(0, 200).map((s) => ({
                label: s.prefix,
                kind: monaco.languages.CompletionItemKind.Snippet,
                insertText: s.body,
                insertTextRules:
                  monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
                detail: 'vscode snippet',
                range: new monaco.Range(pos.lineNumber, word.startColumn,
                  pos.lineNumber, word.endColumn),
              })) };
            },
          });
        }
      }
      toast(`imported ${res.total} snippets across ${Object.keys(res.languages).length} languages`, 'ok');
    } catch (err) {
      toast(err.message, 'err');
    }
  }

  function applyTheme() {
    const light = localStorage.getItem('vanta.theme') === 'light';
    document.body.classList.toggle('light', light);
    if (editor) editor.updateOptions({ theme: light ? 'vanta-light' : 'vanta' });
  }

  function toggleTheme() {
    localStorage.setItem('vanta.theme',
      localStorage.getItem('vanta.theme') === 'light' ? 'dark' : 'light');
    applyTheme();
    toast('theme: ' + localStorage.getItem('vanta.theme'), 'ok');
  }

  function showAbout() {
    openModal({
      title: 'Vanta IDE',
      ok: 'Close',
      cancel: 'Close',
      body: `<div>Local-first. No telemetry, no cloud, no account.<br/><br/>
        <b>server</b> python stdlib · <b>editor</b> monaco · <b>shell</b> ${esc(S.env.shell || 'powershell')}<br/>
        <b>airllm</b> one layer on the GPU at a time.<br/><br/>
        <span class="meta small">Ctrl+P for files, Ctrl+Shift+P for commands.</span></div>`,
    }).then(() => {});
  }

  /* -------------------------------- context -------------------------------- */
  function showCtx(x, y, items) {
    const menu = $('#ctxMenu');
    menu.innerHTML = '';
    for (const item of items) {
      if (item === 'hr') { menu.appendChild(document.createElement('hr')); continue; }
      const b = document.createElement('button');
      b.textContent = item.label;
      if (item.danger) b.style.color = 'var(--danger)';
      b.addEventListener('click', () => { hideCtx(); item.run(); });
      menu.appendChild(b);
    }
    menu.classList.remove('hidden');
    menu.style.left = `${Math.min(x, window.innerWidth - 220)}px`;
    menu.style.top = `${Math.min(y, window.innerHeight - menu.offsetHeight - 10)}px`;
  }
  function hideCtx() { $('#ctxMenu').classList.add('hidden'); }

  /* -------------------------------- watcher -------------------------------- */
  async function reloadTabFromDisk(tab, mtime) {
    if (!S.root) return;
    const full = await api.get('/api/file', { root: S.root, path: tab.path });
    if (full.binary || full.tooLarge) return;
    const pos = editor ? editor.getPosition() : null;
    tab.model.setValue(full.content);
    if (mtime) tab.mtime = mtime;
    tab.savedContent = full.content;
    tab.dirty = false;
    if (pos && S.active === tab.key) editor.setPosition(pos);
    renderTabs();
    toast(`${tab.name} reloaded from disk`, 'ok');
  }

  async function watchFiles() {
    if (!S.root) return;
    if (S.watch.supported) {
      try {
        const res = await api.get('/api/watch/poll', { since: S.watch.cursor });
        if (!res.supported) { S.watch.supported = false; return; }
        for (const ch of res.changes) {
          const tab = S.tabs.find((t) => t.kind === 'file' && t.path === ch.path);
          if (!tab || !tab.model) continue;
          if (tab.dirty) showBanner(tab);
          else await reloadTabFromDisk(tab, Math.round(ch.ts));
        }
        if (res.changes.length) {
          S.watch.cursor = Math.max(...res.changes.map((c) => c.ts));
          S.treeCache.clear();
          renderTree();
          refreshGit();
        }
      } catch { /* transient */ }
      return;
    }
    for (const tab of S.tabs) {
      if (tab.kind !== 'file' || !tab.model) continue;
      try {
        const data = await api.get('/api/stat', { root: S.root, path: tab.path });
        if (!data.mtime || data.mtime === tab.mtime) continue;
        if (tab.dirty) showBanner(tab);
        else {
          await reloadTabFromDisk(tab, data.mtime);
        }
      } catch { /* file may have been moved */ }
    }
  }

  function showBanner(tab) {
    const banner = $('#fileBanner');
    banner.innerHTML = `
      <span>${esc(tab.name)} changed on disk.</span>
      <button data-act="reload">Reload</button>
      <button data-act="keep" class="dismiss">Keep mine</button>`;
    banner.classList.remove('hidden');
    banner.onclick = async (e) => {
      const act = e.target.dataset.act;
      if (!act) return;
      if (act === 'keep') { banner.classList.add('hidden'); return; }
      const data = await api.get('/api/file', { root: S.root, path: tab.path });
      tab.model.setValue(data.content);
      tab.mtime = data.mtime;
      tab.savedContent = data.content;
      tab.dirty = false;
      renderTabs();
      banner.classList.add('hidden');
    };
    setTimeout(() => banner.classList.add('hidden'), 20000);
  }

  /* --------------------------------- recent -------------------------------- */
  function renderRecent() {
    const recent = S.env.recent || [];
    const mini = $('#welcomeRecent');
    const list = $('#recentList');
    if (mini) mini.innerHTML = recent.slice(0, 6).map((r) =>
      `<a href="#" data-path="${esc(r)}">${esc(r)}</a>`).join('');
    if (list) list.innerHTML = recent.map((r) =>
      `<div class="change-row" data-path="${esc(r)}"><span class="p">${esc(r)}</span></div>`).join('');
    $$('#welcomeRecent a, #recentList [data-path]').forEach((a) => {
      a.addEventListener('click', (e) => { e.preventDefault(); openProject(a.dataset.path); });
    });
  }

  /* ---------------------------------- init --------------------------------- */
  function addResizers() {
    const side = document.createElement('div');
    side.className = 'rz rz-v';
    $('#sidebar').after(side);
    let dragV = false;
    side.addEventListener('mousedown', (e) => {
      dragV = true; e.preventDefault();
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragV) return;
      const w = Math.min(620, Math.max(180, e.clientX - 46));
      document.documentElement.style.setProperty('--side-w', `${w}px`);
      if (editor) editor.layout();
    });
    window.addEventListener('mouseup', () => {
      dragV = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    });

    const panelRz = document.createElement('div');
    panelRz.className = 'rz rz-h';
    $('#panel').before(panelRz);
    let dragH = false;
    panelRz.addEventListener('mousedown', (e) => {
      dragH = true; e.preventDefault();
      document.body.style.cursor = 'row-resize';
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragH) return;
      const h = Math.min(window.innerHeight - 160, Math.max(90, window.innerHeight - e.clientY - 22));
      document.documentElement.style.setProperty('--panel-h', `${h}px`);
      if (editor) editor.layout();
    });
    window.addEventListener('mouseup', () => {
      dragH = false;
      document.body.style.cursor = '';
    });

    const dockRz = $('#dockRz');
    let dragD = false;
    dockRz.addEventListener('mousedown', (e) => {
      dragD = true; e.preventDefault();
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragD) return;
      const w = Math.min(window.innerWidth - 460, Math.max(260, window.innerWidth - e.clientX));
      document.documentElement.style.setProperty('--chat-w', `${w}px`);
      if (editor) editor.layout();
    });
    window.addEventListener('mouseup', () => {
      if (!dragD) return;
      dragD = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      try {
        localStorage.setItem(CHAT_W_KEY,
          document.documentElement.style.getPropertyValue('--chat-w'));
      } catch { /* private mode */ }
    });
  }

  async function init() {
    setupEditor();
    defineTheme();
    bindTerminal();
    bindKeys();
    bindDebugger();
    addResizers();
    applyTheme();
    api.get('/api/config/keybindings', { root: '' }).then((res) => {
      keyOverrides = res.keybindings || {};
    }).catch(() => {});

    try {
      const savedW = localStorage.getItem(CHAT_W_KEY);
      if (savedW) document.documentElement.style.setProperty('--chat-w', savedW);
      const pref = localStorage.getItem(CHAT_KEY);
      showChat(pref === null ? true : pref === '1');
    } catch {
      showChat(true);
    }
    chatEmpty();
    loadChatConfig();

    try {
      const env = await api.get('/api/env');
      S.env = env;
      if (env.admin) {
        $('#termAdminBadge')?.classList.remove('hidden');
        $('#btnTermElevate')?.classList.add('hidden');
      } else {
        $('#termAdminBadge')?.classList.add('hidden');
        $('#btnTermElevate')?.classList.remove('hidden');
      }
      $('#sbGpu').textContent = '';
      renderRecent();
      if (env.lastRoot) {
        await openProject(env.lastRoot);
        restoreSession();
      }
    } catch (err) {
      toast('server offline? ' + err.message, 'err');
    }

    if (isDesktop()) {
      document.documentElement.dataset.shell = 'desktop';
      ncall('app_info').then((info) => {
        if (info && info.url) ncall('log', `boot ${info.url} ownsServer=${info.ownsServer}`);
        if (S.name) ncall('set_title', `Vanta IDE — ${S.name}`);
      });
    }

    api.get('/api/hardware').then((hw) => {
      if (hw.gpu && hw.gpu.length) {
        const g = hw.gpu[0];
        $('#sbGpu').textContent = `${g.name} · ${g.vram}`;
        $('#sbGpu').title = `driver ${g.driver}`;
      }
      const gpuHtml = (hw.gpu || []).map((g) =>
        `${esc(g.name)}<br/>${esc(g.vram)} VRAM · driver ${esc(g.driver)}`).join('<br/>') ||
        'no NVIDIA GPU detected';
      const tsPort = location.port || '7777';
      const tsHtml = S.env && S.env.tailscale
        ? `<br/><span style="color:var(--accent)">Tailscale: http://${esc(S.env.tailscale)}:${esc(tsPort)}</span>`
        : '';
      const admHtml = S.env
        ? `<br/>Privilege: <b>${S.env.admin ? 'Administrator' : 'Standard User'}</b>`
        : '';
      $('#machineInfo').innerHTML = gpuHtml + tsHtml + admHtml;
    }).catch(() => {});

    await startTerminal(S.root || undefined);

    setInterval(pollTerminal, 400);
    setInterval(pollAi, 900);
    setInterval(watchFiles, 4000);
    setInterval(pollDebug, 500);
    setInterval(() => { if (S.root) refreshGit(); }, 12000);

    $('#tree').addEventListener('contextmenu', (e) => {
      if (e.target.closest('.node')) return;
      e.preventDefault();
      if (!S.root) return;
      showCtx(e.clientX, e.clientY, [
        { label: 'New File…', run: () => createEntry('', false) },
        { label: 'New Folder…', run: () => createEntry('', true) },
        'hr',
        { label: 'Refresh', run: () => { S.treeCache.clear(); renderTree(); } },
      ]);
    });
    document.addEventListener('click', (e) => {
      hideCtx();
      if (!e.target.closest('#menubar') && !e.target.closest('#menuDrop')) hideMenu();
    });
    $('#editorHost').addEventListener('scroll', () => {}, true);
    $('#termPane').addEventListener('click', () => $('#termInput').focus());
    $('#termOut').addEventListener('click', () => $('#termInput').focus());
  }

  function bindKeys() {
    const KEY_COMMANDS = {
      quickopen: openQuickOpen, palette: openPalette,
      search: () => setView('search'), explorer: () => setView('explorer'),
      scm: () => setView('scm'), saveall: saveAll, save: saveActive,
      sidebar: toggleSidebar, chat: () => setView('ai'),
      panel: () => showPanel(!S.settings.panel),
      close: () => S.active && closeTab(S.active), theme: toggleTheme,
    };
    const keyId = (e) => (e.ctrlKey || e.metaKey ? 'ctrl+' : '')
      + (e.shiftKey ? 'shift+' : '') + e.key.toLowerCase();
    document.addEventListener('keydown', (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      const override = keyOverrides[keyId(e)];
      if (override && KEY_COMMANDS[override]) {
        e.preventDefault();
        KEY_COMMANDS[override]();
        return;
      }
      const k = e.key.toLowerCase();
      if (e.shiftKey && k === 'p') { e.preventDefault(); openPalette(); }
      else if (e.shiftKey && k === 'f') { e.preventDefault(); setView('search'); $('#searchInput').focus(); }
      else if (e.shiftKey && k === 'e') { e.preventDefault(); setView('explorer'); }
      else if (e.shiftKey && k === 'g') { e.preventDefault(); setView('scm'); }
      else if (e.shiftKey && k === 's') { e.preventDefault(); saveAll(); }
      else if (k === 'p') { e.preventDefault(); openQuickOpen(); }
      else if (k === 's') { e.preventDefault(); saveActive(); }
      else if (k === 'b') { e.preventDefault(); toggleSidebar(); }
      else if (k === 'j') { e.preventDefault(); setView('ai'); }
      else if (k === '`') { e.preventDefault(); showPanel(!S.settings.panel); }
      else if (k === 'w' && S.active) { e.preventDefault(); closeTab(S.active); }
    }, true);

    document.addEventListener('keydown', (e) => {
      if (e.key === 'F5') { e.preventDefault(); const t = activeTab(); if (t) runFile(t.path); }
      if (e.key === 'F11') {
        if (isDesktop()) { e.preventDefault(); ncall('toggle_fullscreen'); return; }
      }
      if (e.key === 'Escape') {
        hideCtx();
        hideMenu();
        if (!$('#overlay').classList.contains('hidden')) closeOverlay();
        else if (!$('#modalWrap').classList.contains('hidden')) finishModal(null);
      }
      if (e.key === 'Enter' && e.ctrlKey && !$('#modalWrap').classList.contains('hidden')) {
        e.preventDefault();
        $('#modalOk').click();
      }
    });
  }

  /* ------------------------------- listeners ------------------------------- */
  $$('.act').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
  $$('#menubar button').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = b.classList.contains('open');
    hideMenu();
    if (!open) { b.classList.add('open'); openMenu(b.dataset.menu, b); }
  }));
  $('#overlayInput').addEventListener('input', (e) => {
    OVERLAY.filter = e.target.value;
    OVERLAY.index = 0;
    renderOverlay();
  });
  $('#overlayInput').addEventListener('keydown', overlayKey);
  $('#overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') closeOverlay(); });
  $('#quickOpenBtn').addEventListener('click', openPalette);
  $('#btnReload').addEventListener('click', () => location.reload());
  $('#btnOpenFolder2').addEventListener('click', () => browseModal());
  $('#welcomeOpen').addEventListener('click', () => browseModal());
  $('#btnNewFile').addEventListener('click', () => createEntry('', false));
  $('#btnNewFolder').addEventListener('click', () => createEntry('', true));
  $('#btnRefreshTree').addEventListener('click', () => { S.treeCache.clear(); renderTree(); });
  $('#btnCollapseAll').addEventListener('click', () => { S.expanded = new Set(['']); renderTree(); });
  $('#btnGitRefresh').addEventListener('click', refreshGit);
  $('#btnCommit').addEventListener('click', commitChanges);
  $('#gitMessage').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commitChanges(); }
  });
  $('#searchRun').addEventListener('click', runSearch);
  $('#searchInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });
  $('#searchReplaceBtn').addEventListener('click', replaceAll);
  $('#searchCase').addEventListener('click', () => $('#searchCase').classList.toggle('on'));
  $('#searchRegex').addEventListener('click', () => $('#searchRegex').classList.toggle('on'));
  $('#btnGitPull').addEventListener('click', () => gitPushPull('pull'));
  $('#btnGitPush').addEventListener('click', () => gitPushPull('push'));
  $('#gitBranch').addEventListener('change', () => checkoutBranch($('#gitBranch').value));
  $('#btnGitPRs').addEventListener('click', loadPrs);
  $('#remoteList').addEventListener('click', (e) => {
    const row = e.target.closest('[data-kind]');
    if (!row) return;
    if (row.dataset.kind === 'wsl') {
      const info = ((S.remotes || {}).wsl || []).find((w) => w.name === row.dataset.distro);
      if (info && info.home) openProject(info.home);
      openRemoteShell('wsl', row.dataset.distro);
    } else {
      api.post('/api/remotes/open', { kind: 'ssh', id: row.dataset.id, root: S.root || '' })
        .then(() => {
          openRemoteShell('ssh', null, row.dataset.id);
          sshBrowseModal(row.dataset.id);
        })
        .catch((err) => toast(err.message, 'err'));
    }
  });
  $('#btnPluginReload').addEventListener('click', reloadPlugins);
  $('#btnUpdateCheck').addEventListener('click', checkForUpdates);  $('#gitPRs').addEventListener('click', (e) => {
    const row = e.target.closest('[data-pr]');
    if (row) checkoutPr(Number(row.dataset.pr));
  });
  $('#btnOllamaPull').addEventListener('click', pullOllamaModel);
  $('#btnProfileSave').addEventListener('click', saveProfile);
  $('#btnProfileDel').addEventListener('click', deleteProfile);
  $('#chatService').addEventListener('focus', loadServices);
  $('#chatService').addEventListener('change', onServicePick);
  $('#btnServiceUse').addEventListener('click', applyService);
  $('#localFit').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-model]');
    if (!chip) return;
    $('#chatLocalModel').value = chip.dataset.model;
    paintLocalFit();
  });
  $('#editorList').addEventListener('click', (e) => {
    const row = e.target.closest('[data-i]');
    if (row) openInEditor(Number(row.dataset.i));
  });
  $('#btnImportVscode').addEventListener('click', importVscode);
  $('#btnImportSnips').addEventListener('click', importSnippets);
  $('#btnChatLoad').addEventListener('click', () => { loadLocalModel(); paintLocalFit(); });
  $('#chatProfileList').addEventListener('change', () => {
    saveChatConfig({ profile: $('#chatProfileList').value || '' });
    paintChatTag();
  });
  $('#chatInstructions').addEventListener('change', () => {
    saveChatConfig({ customInstructions: $('#chatInstructions').value });
  });
  $('#chatBudget').addEventListener('change', () => {
    saveChatConfig({ contextBudget: Number($('#chatBudget').value) || 32000 });
  });  $('#ollamaList').addEventListener('click', (e) => {
    const row = e.target.closest('[data-model]');
    if (row && e.target.classList.contains('ollama-del')) {
      deleteOllamaModel(row.dataset.model);
    }
  });
  $('#btnChatSettings').addEventListener('click', () => {
    const open = $('#chatSettings').classList.toggle('hidden');
    if (!open) { loadOllamaModels(); paintLocalFit(); loadServices(); }
  });

  $('#btnChatClose').addEventListener('click', () => showChat(false));
  $('#btnChatReset').addEventListener('click', resetChat);
  $('#btnChatSend').addEventListener('click', chatSend);
  $('#btnChatStop').addEventListener('click', stopChat);
  $('#btnChatApply').addEventListener('click', applyChatReply);
  bindAttachments();
  $('#chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); chatSend(); }
  });
  $('#chatLog').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-cid]');
    if (!btn) return;
    answerConfirm(btn.dataset.cid, btn.dataset.approve === '1');
    btn.disabled = true;
  });
  $$('#chatProvider .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setProvider(b.dataset.provider)));
  $('#btnChatLoad').addEventListener('click', loadLocalModel);
  $('#btnChatFetch').addEventListener('click', fetchChatModels);
  $('#chatMode').addEventListener('change', () => {
    S.ai.mode = $('#chatMode').value;
    saveChatConfig({ mode: S.ai.mode });
    paintChatTag();
  });
  $('#chatApiModel').addEventListener('change', () => {
    paintChatTag();
    saveChatConfig({ api: { model: $('#chatApiModel').value } });
  });
  $('#chatApiBase').addEventListener('change', () =>
    saveChatConfig({ api: { baseUrl: $('#chatApiBase').value.trim() } }));
  $('#chatApiKey').addEventListener('change', () => {
    if ($('#chatApiKey').value.trim()) saveChatConfig(gatherChatPatch());
  });
  $('#chatClearKey').addEventListener('change', () => {
    if ($('#chatClearKey').checked) {
      saveChatConfig(gatherChatPatch(), true);
      $('#chatClearKey').checked = false;
    }
  });
  $('#chatTemp').addEventListener('change', () =>
    saveChatConfig({ temperature: Number($('#chatTemp').value) || 0.6 }));
  $('#chatMax').addEventListener('change', () =>
    saveChatConfig({ maxTokens: Number($('#chatMax').value) || 512 }));
  $('#chatContext').addEventListener('change', () =>
    saveChatConfig({ contextFile: $('#chatContext').checked }));
  $('#chatAgentTools')?.addEventListener('change', () =>
    saveChatConfig({ agentTools: $('#chatAgentTools').checked }));
  $('#chatFailsafe')?.addEventListener('change', () => {
    S.ai.failsafe = $('#chatFailsafe').checked;
    paintChatTag();
    saveChatConfig({ failsafe: S.ai.failsafe });
  });
  $('#chatLocalModel').addEventListener('change', () =>
    saveChatConfig({ local: { model: $('#chatLocalModel').value.trim() } }));

  $('#btnTermElevate')?.addEventListener('click', async () => {
    const ok = await confirmModal('Administrator Privileges',
      'Restart Vanta IDE with full Administrator privileges? Windows will prompt for UAC elevation.',
      'Elevate (UAC)');
    if (!ok) return;
    try {
      await api.post('/api/admin/elevate', {});
      toast('Elevating with Administrator privileges…', 'ok');
    } catch (err) {
      toast('Elevation failed: ' + err.message, 'err');
    }
  });
  $('#btnTermNew').addEventListener('click', () => startTerminal(S.root || undefined));
  $('#btnTermClear').addEventListener('click', () => { $('#termOut').textContent = ''; });
  $('#btnTermStop').addEventListener('click', async () => {
    if (!S.term.id) return;
    await api.post('/api/term/interrupt', { id: S.term.id }).catch(() => {});
    termWrite('\n* shell restarted\n');
  });
  $('#btnPanelClose').addEventListener('click', () => showPanel(false));
  $$('.ptab').forEach((b) => b.addEventListener('click', () => switchPanelTab(b.dataset.ptab)));

  window.addEventListener('load', init);
  window.addEventListener('resize', () => { if (editor) editor.layout(); if (diffEditor) diffEditor.layout(); });
  window.addEventListener('beforeunload', (e) => {
    if (isDesktop()) return;               // the shell owns the close guard
    if (!window.__vantaDirty()) return;
    e.preventDefault();
    e.returnValue = '';
  });
})();
