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
  };

  const api = {
    async get(path, params) {
      const url = new URL(path, location.origin);
      if (params) for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) url.searchParams.set(k, v);
      }
      const res = await fetch(url);
      const data = await res.json().catch(() => ({ ok: false, error: 'bad json' }));
      if (!res.ok || data.ok === false) throw new Error(data.error || `${res.status}`);
      return data;
    },
    async post(path, body) {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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

  window.__vantaDirty = () => S.tabs.filter((t) => t.dirty && t.kind === 'file').length;
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
    editor.onDidChangeModelLanguage?.(() => {});

    editor.addAction({
      id: 'vanta.saveless',
      label: 'Save File (handled globally)',
      run: () => saveActive(),
    });
    $('#editor').addEventListener('mouseleave', () => {});

    defineTheme();
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

  /* ---------------------------------- tabs --------------------------------- */
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
      });
      S.tabs.push(tab);
      activateTab(tab.key);
      if (line) gotoLine(tab, line);
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
    if (!tab || tab.kind !== 'file' || !tab.model) return;
    try {
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
  async function openProject(path) {
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
    for (const entry of entries) {
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
        try {
          await renderDirNode(entry.path, childHost, depth + 1, token);
        } catch (err) {
          childHost.innerHTML = `<div class="side-empty">${esc(err.message)}</div>`;
        }
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
    } catch (err) {
      $('#gitBranchRow').textContent = err.message;
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
    const provider = S.ai.provider === 'api' ? 'api' : 'local';
    const label = provider === 'api' ? 'vps' : 'local';
    const model = provider === 'api'
      ? (($('#chatApiModel') && $('#chatApiModel').value) || S.ai.model || 'auto')
      : (S.ai.model || ($('#chatLocalModel') ? $('#chatLocalModel').value.trim() : ''));
    const fsOn = $('#chatFailsafe') ? $('#chatFailsafe').checked : S.ai.failsafe !== false;
    if (tag) {
      tag.textContent = `${label} · ${model || 'auto'}`;
      tag.classList.toggle('api', provider === 'api');
      tag.title = provider === 'api'
        ? `Primary: Remote Ollama VPS${fsOn ? ' (Local AirLLM failsafe armed)' : ''}`
        : 'Local AirLLM engine';
    }
    const chip = $('#sbAi');
    if (chip) {
      chip.textContent = `ai: ${label}${model ? ' · ' + base(model) : ''}${provider === 'api' && fsOn ? ' (+fs)' : ''}`;
      chip.classList.toggle('busy', S.ai.state === 'generating' || S.ai.state === 'loading');
      chip.title = provider === 'api'
        ? `Remote Ollama VPS (${$('#chatApiBase')?.value || ''}) — Failsafe: ${fsOn ? 'Local AirLLM' : 'off'}`
        : `Local AirLLM (${model || ''})`;
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
    S.ai.provider = cfg.provider === 'local' ? 'local' : 'api';
    S.ai.failsafe = cfg.failsafe !== false;
    S.ai.mode = cfg.mode || 'agent';
    $$('#chatProvider .seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.provider === S.ai.provider));
    $('#chatLocalGroup').classList.toggle('hidden', S.ai.provider !== 'local');
    $('#chatApiGroup').classList.toggle('hidden', S.ai.provider !== 'api');

    $('#chatMode').value = S.ai.mode;
    if (cfg.temperature !== undefined) $('#chatTemp').value = cfg.temperature;
    if (cfg.maxTokens !== undefined) $('#chatMax').value = cfg.maxTokens;
    $('#chatContext').checked = cfg.contextFile !== false;
    if ($('#chatAgentTools')) $('#chatAgentTools').checked = cfg.agentTools !== false;
    if ($('#chatFailsafe')) $('#chatFailsafe').checked = S.ai.failsafe;

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

  function gatherChatPatch(extra) {
    const patch = {
      provider: S.ai.provider,
      failsafe: $('#chatFailsafe') ? $('#chatFailsafe').checked : true,
      mode: $('#chatMode').value,
      contextFile: $('#chatContext').checked,
      agentTools: $('#chatAgentTools') ? $('#chatAgentTools').checked : true,
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
    S.ai.provider = provider === 'local' ? 'local' : 'api';
    $$('#chatProvider .seg-btn').forEach((b) =>
      b.classList.toggle('active', b.dataset.provider === S.ai.provider));
    $('#chatLocalGroup').classList.toggle('hidden', S.ai.provider !== 'local');
    $('#chatApiGroup').classList.toggle('hidden', S.ai.provider !== 'api');
    $('#chatHint').textContent = S.ai.provider === 'api'
      ? 'Primary: Remote Ollama on VPS. If unreachable and failsafe is checked, requests automatically route to local AirLLM.'
      : "Failsafe / Local: AirLLM streams layers off disk onto this machine's GPU.";
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
    if (chatStream) chatStream.classList.remove('streaming');
    chatStream = null;
  }

  function finishChatReply(text) {
    const div = (chatStream && chatStream.isConnected)
      ? chatStream : aiWrite('assistant', '', 'assistant');
    div.classList.remove('streaming');
    div.querySelector('.who').textContent = S.ai.provider === 'api' ? 'api' : 'local';
    const body = div.querySelector('.msg-body');
    const reply = text !== undefined && text !== null && text !== '' ? text : body.textContent;
    if (reply && body.textContent !== reply) body.textContent = reply;
    if (!reply) body.textContent = '…';
    S.ai.lastReply = reply;
    S.ai.busy = false;
    stopChatClock();
    $('#btnChatSend').disabled = false;
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
    const body = {
      provider: S.ai.provider,
      failsafe: $('#chatFailsafe') ? $('#chatFailsafe').checked : true,
      mode: $('#chatMode').value,
      messages: [],
      temperature: Number($('#chatTemp').value),
      maxTokens: Number($('#chatMax').value) || 512,
      contextFile: $('#chatContext').checked,
      agentTools: $('#chatAgentTools') ? $('#chatAgentTools').checked : true,
      localModel: $('#chatLocalModel').value.trim(),
      apiBaseUrl: $('#chatApiBase').value.trim(),
      apiModel: $('#chatApiModel').value,
      root: S.root || '',
      file: tab ? tab.path : '',
      fileText,
      selection: selectionText(),
    };
    input.value = '';
    S.ai.messages.push({ role: 'user', content: text });
    body.messages = S.ai.messages.slice(-16);
    aiWrite('you', text, 'user');
    S.ai.busy = true;
    S.ai.lastReply = '';
    $('#btnChatSend').disabled = true;
    $('#btnChatApply').classList.add('hidden');
    startChatClock();
    const pending = aiWrite(S.ai.provider === 'api' ? 'vps' : 'local', '', 'assistant streaming', true);
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
            chatStream = aiWrite(S.ai.provider === 'api' ? 'api' : 'local',
              '', 'assistant streaming', true);
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
            aiWrite(S.ai.provider === 'api' ? 'api' : 'local', ev.text, 'assistant');
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
        } else if (ev.type === 'reset') {
          clearChatView();
        }
      }
      if (res.state) setAiState(res.state, res.model);
    } catch { /* server may be restarting */ }
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
    return COMMANDS.map((c) => ({
      label: c.label, path: c.hint || '', kind: 'cmd', run: c.run,
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
        const data = await fetch('/api/browse?path=' + encodeURIComponent(p)).then((r) => r.json());
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
  async function watchFiles() {
    if (!S.root) return;
    for (const tab of S.tabs) {
      if (tab.kind !== 'file' || !tab.model) continue;
      try {
        const data = await api.get('/api/stat', { root: S.root, path: tab.path });
        if (!data.mtime || data.mtime === tab.mtime) continue;
        if (tab.dirty) showBanner(tab);
        else {
          const full = await api.get('/api/file', { root: S.root, path: tab.path });
          if (full.binary || full.tooLarge) continue;
          const pos = editor.getPosition();
          tab.model.setValue(full.content);
          tab.mtime = data.mtime;
          tab.savedContent = full.content;
          tab.dirty = false;
          if (pos) editor.setPosition(pos);
          renderTabs();
          toast(`${tab.name} reloaded from disk`, 'ok');
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
    addResizers();

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
      if (env.lastRoot) await openProject(env.lastRoot);
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
    document.addEventListener('keydown', (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
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
  $('#searchCase').addEventListener('click', () => $('#searchCase').classList.toggle('on'));
  $('#searchRegex').addEventListener('click', () => $('#searchRegex').classList.toggle('on'));

  $('#btnChatClose').addEventListener('click', () => showChat(false));
  $('#btnChatSettings').addEventListener('click', () => $('#chatSettings').classList.toggle('hidden'));
  $('#btnChatReset').addEventListener('click', resetChat);
  $('#btnChatSend').addEventListener('click', chatSend);
  $('#btnChatApply').addEventListener('click', applyChatReply);
  $('#chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); chatSend(); }
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
