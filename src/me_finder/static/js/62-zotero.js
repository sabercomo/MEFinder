/* 设置 → 来源 → Zotero：连接状态、分类树、自动同步与立即同步明细。
   分类以 Zotero 为准：这里只保存用户勾选的分类 key；勾父分类等于勾全部子分类。
   同步本身在后端（/api/zotero/*），前端只展示后端能确认的状态。DOM 一律用
   createElement 构造，Zotero 返回的标题不经 innerHTML。 */
(function (global) {  // module: 62-zotero.js
  var FREQUENCY_LABELS = { manual: '仅手动', launch: '启动 MEFinder 时', interval: '启动时及每 30 分钟' };
  var CONNECTION_NOTES = {
    connected: '127.0.0.1:23119 · 我的文库',
    api_disabled: 'Zotero 已打开，但未开放本机接口',
    not_running: '默认端口 23119',
    unsupported: '需要 Zotero 7 或更新',
    error: '读取 Zotero 时出错'
  };
  var CONNECTION_HINTS = {
    api_disabled: '在 Zotero 里打开一次即可：设置 → 高级 → 勾选 “Allow other applications on this computer to communicate with Zotero”。这是 Zotero 自带的本机只读接口，数据不出电脑，也不用装 Zotero 插件',
    not_running: '打开 Zotero 后自动连接。Zotero 没开时，已导入的文献照常检索，只是暂停同步'
  };
  var state = {
    connection: null,
    collections: [],
    selected: [],
    enabled: false,
    frequency: 'launch',
    synced: [],
    folded: {},
    status: null,
    preview: null,
    pollTimer: null,
    loading: false
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svg(pathD, size) {
    var ns = 'http://www.w3.org/2000/svg';
    var icon = document.createElementNS(ns, 'svg');
    icon.setAttribute('viewBox', '0 0 12 12');
    icon.setAttribute('width', size);
    icon.setAttribute('height', size);
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', '1.8');
    icon.setAttribute('stroke-linecap', 'round');
    icon.setAttribute('stroke-linejoin', 'round');
    icon.setAttribute('aria-hidden', 'true');
    var path = document.createElementNS(ns, 'path');
    path.setAttribute('d', pathD);
    icon.appendChild(path);
    return icon;
  }

  // 统一请求出口（07-api.js）的本地转发。
  function getJSON(url, options) {
    return MEFinderApi.requestJSON(url, options);
  }

  async function savePreferences(updates) {
    return getJSON('/api/preferences', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    });
  }

  // ── collection tree model ──

  function childrenOf(key) {
    return state.collections.filter(function (row) { return row.parent === key; });
  }

  function descendants(key) {
    var out = [];
    childrenOf(key).forEach(function (child) { out.push(child.key); out = out.concat(descendants(child.key)); });
    return out;
  }

  function ancestors(key) {
    var byKey = {};
    state.collections.forEach(function (row) { byKey[row.key] = row; });
    var out = [];
    var row = byKey[key];
    while (row && row.parent && byKey[row.parent]) { out.push(row.parent); row = byKey[row.parent]; }
    return out;
  }

  function isChecked(key) {
    return state.selected.indexOf(key) >= 0 || ancestors(key).some(function (a) { return state.selected.indexOf(a) >= 0; });
  }

  function checkState(key) {
    if (isChecked(key)) return 'true';
    return descendants(key).some(function (d) { return state.selected.indexOf(d) >= 0; }) ? 'mixed' : 'false';
  }

  function effectiveKeys() {
    var keys = {};
    state.selected.forEach(function (key) {
      keys[key] = true;
      descendants(key).forEach(function (d) { keys[d] = true; });
    });
    return keys;
  }

  function toggleCollection(key) {
    var next = state.selected.slice();
    if (isChecked(key)) {
      // 取消一个被父分类覆盖的子分类：父分类换成除这条路径以外的兄弟分类。
      var chain = [key].concat(ancestors(key));
      var owner = chain.filter(function (k) { return next.indexOf(k) >= 0; }).pop();
      next = next.filter(function (k) { return k !== owner; });
      for (var i = chain.indexOf(owner); i > 0; i--) {
        var onPath = chain[i - 1];
        childrenOf(chain[i]).forEach(function (child) {
          if (child.key !== onPath && next.indexOf(child.key) < 0) next.push(child.key);
        });
      }
      next = next.filter(function (k) { return k !== key && descendants(key).indexOf(k) < 0; });
    } else {
      var inside = descendants(key);
      next = next.filter(function (k) { return inside.indexOf(k) < 0; });
      next.push(key);
    }
    state.selected = next;
    renderTree();
    persistSelection();
  }

  var selectionTimer = null;
  function persistSelection() {
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(async function () {
      try {
        var saved = await savePreferences({ zotero_sync_collections: state.selected });
        state.selected = saved.zotero_sync_collections || [];
      } catch (e) {
        showToast('分类选择保存失败：' + e.message);
      }
      loadPreview();
    }, 250);
  }

  async function loadPreview() {
    try {
      state.preview = await getJSON('/api/zotero/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ collections: state.selected })
      });
    } catch (e) {
      state.preview = null;
    }
    renderFoot();
  }

  // ── rendering ──

  function rowStatus(row, checked) {
    var synced = state.synced.indexOf(row.key) >= 0;
    if (checked && synced) {
      return row.unsynced_count > 0 ? ['待同步 ' + row.unsynced_count + ' 篇', 'is-accent'] : ['已同步', 'is-ok'];
    }
    if (checked) return row.item_count > 0 ? ['待同步 ' + row.item_count + ' 篇', 'is-accent'] : ['', ''];
    if (synced && row.linked_count > 0) return ['同步时移除 ' + row.linked_count + ' 篇', 'is-warn'];
    return ['', ''];
  }

  function appendRows(container, parentKey, depth) {
    state.collections.filter(function (row) { return (row.parent || null) === parentKey; }).forEach(function (row) {
      var kids = childrenOf(row.key);
      var line = el('div', 'zotero-tree-row');
      line.setAttribute('role', 'treeitem');
      line.style.setProperty('--zotero-depth', String(depth));
      if (kids.length) {
        var folded = !!state.folded[row.key];
        line.setAttribute('aria-expanded', folded ? 'false' : 'true');
        var fold = el('button', 'zotero-fold' + (folded ? '' : ' is-open'));
        fold.type = 'button';
        fold.setAttribute('aria-label', (folded ? '展开' : '收起') + row.name);
        fold.appendChild(svg('M4.5 3 7.5 6l-3 3', 12));
        fold.addEventListener('click', function () { state.folded[row.key] = !folded; renderTree(); });
        line.appendChild(fold);
      } else {
        line.appendChild(el('span', 'zotero-fold-spacer'));
      }
      var aria = checkState(row.key);
      var check = el('button', 'zotero-check');
      check.type = 'button';
      check.setAttribute('role', 'checkbox');
      check.setAttribute('aria-checked', aria);
      var box = el('span', 'zotero-box' + (aria === 'false' ? '' : ' is-on'));
      if (aria === 'true') box.appendChild(svg('M2.5 6.2 5 8.6l4.5-5', 11));
      if (aria === 'mixed') box.appendChild(svg('M3 6h6', 11));
      check.appendChild(box);
      check.appendChild(el('span', 'zotero-name' + (kids.length ? ' is-parent' : ''), row.name));
      check.appendChild(el('span', 'zotero-count', row.item_count + ' 篇'));
      check.addEventListener('click', function () { toggleCollection(row.key); });
      line.appendChild(check);
      var status = rowStatus(row, aria === 'true');
      line.appendChild(el('span', 'zotero-state ' + status[1], status[0]));
      container.appendChild(line);
      if (kids.length && !state.folded[row.key]) appendRows(container, row.key, depth + 1);
    });
  }

  function renderTree() {
    var tree = document.getElementById('zotero-tree');
    if (!tree) return;
    tree.replaceChildren();
    if (!state.collections.length) {
      var connected = state.connection && state.connection.state === 'connected';
      tree.appendChild(el('p', 'zotero-empty', connected ? '我的文库里还没有分类' : '连接 Zotero 后显示分类'));
    } else {
      appendRows(tree, null, 0);
    }
    renderFoot();
  }

  function renderFoot() {
    var foot = document.getElementById('zotero-tree-foot');
    if (!foot) return;
    foot.replaceChildren();
    // 不显示「共 N 篇」：各分类计数相加会把跨分类的同一条目算两次。
    var keys = Object.keys(effectiveKeys());
    foot.appendChild(el('span', '', keys.length
      ? '已选 ' + keys.length + ' 个分类；同一条目在多个分类中只导入一次'
      : '还没有选择分类'));
    var preview = state.preview;
    if (preview && preview.known && preview.remove_count > 0) {
      foot.appendChild(el('span', 'zotero-state is-warn', '同步时移除 ' + preview.remove_count + ' 篇'));
    }
    if (preview && preview.known && preview.unlink_count > 0) {
      foot.appendChild(el('span', 'zotero-state', preview.unlink_count + ' 篇同步前已在文库中，只解除关联'));
    }
  }

  function renderConnection() {
    var info = state.connection || { state: 'checking', label: '检测中', message: '' };
    var stateEl = document.getElementById('zotero-connection-state');
    var note = document.getElementById('zotero-connection-note');
    var hint = document.getElementById('zotero-connection-hint');
    var body = document.getElementById('zotero-body');
    if (stateEl) {
      stateEl.textContent = info.label || '';
      stateEl.className = 'zotero-state ' + ({ connected: 'is-ok', api_disabled: 'is-warn', error: 'is-warn', unsupported: 'is-warn' }[info.state] || '');
    }
    if (note) note.textContent = CONNECTION_NOTES[info.state] || info.message || '';
    if (note && info.state === 'error' && info.message) note.textContent = info.message;
    if (hint) {
      hint.textContent = CONNECTION_HINTS[info.state] || '';
      hint.hidden = !CONNECTION_HINTS[info.state];
    }
    if (body) body.classList.toggle('is-dim', info.state !== 'connected');
  }

  function renderSettings() {
    var toggle = document.getElementById('zotero-sync-enabled');
    if (toggle) toggle.checked = state.enabled;
    var toggleState = document.getElementById('zotero-sync-enabled-state');
    if (toggleState) toggleState.textContent = state.enabled ? '开启' : '关闭';
    var label = document.getElementById('zotero-frequency-label');
    if (label) label.textContent = FREQUENCY_LABELS[state.frequency] || FREQUENCY_LABELS.launch;
    document.querySelectorAll('[data-zotero-frequency]').forEach(function (option) {
      var on = option.getAttribute('data-zotero-frequency') === state.frequency;
      option.classList.toggle('is-selected', on);
      option.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function formatTime(iso) {
    if (!iso) return '';
    var date = new Date(iso);
    if (isNaN(date.getTime())) return '';
    var now = new Date();
    var hh = String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
    if (date.toDateString() === now.toDateString()) return '今天 ' + hh;
    return (date.getMonth() + 1) + ' 月 ' + date.getDate() + ' 日 ' + hh;
  }

  function renderStatus() {
    var status = state.status || {};
    var running = status.phase === 'running';
    var button = document.getElementById('zotero-sync-button');
    var connected = state.connection && state.connection.state === 'connected';
    if (button) {
      button.textContent = running ? '同步中' : '立即同步';
      button.disabled = running || !connected || !state.enabled;
      button.title = !state.enabled ? '先开启同步 Zotero 分类' : (!connected ? 'Zotero 未连接' : '');
    }
    var progress = document.getElementById('zotero-progress');
    if (progress) progress.hidden = !running;
    var note = document.getElementById('zotero-sync-note');
    if (note) {
      var last = (status.last_result && status.last_result.message) || '';
      if (running) note.textContent = status.message || '正在同步';
      else if (status.phase && status.phase !== 'idle' && status.message) note.textContent = '上次同步：' + (formatTime(status.finished_at) || '刚刚') + ' · ' + status.message;
      else if (status.last_attempt_at) note.textContent = '上次同步：' + formatTime(status.last_attempt_at) + (last ? ' · ' + last : '');
      else note.textContent = '还没有同步过';
    }
    var log = document.getElementById('zotero-log');
    if (!log) return;
    var rows = status.rows || [];
    log.hidden = !rows.length;
    log.replaceChildren();
    rows.forEach(function (row) {
      var line = el('div', 'zotero-log-row');
      line.appendChild(el('span', 'zotero-log-kind', row.action));
      var copy = el('div', 'zotero-log-copy');
      copy.appendChild(el('span', 'zotero-log-title', row.title));
      if (row.meta) copy.appendChild(el('span', 'zotero-log-meta', row.meta));
      line.appendChild(copy);
      var tone = { ok: 'is-ok', warn: 'is-warn', busy: 'is-busy' }[row.tone] || '';
      line.appendChild(el('span', 'zotero-state ' + tone, row.status_text));
      log.appendChild(line);
    });
  }

  // ── loading & polling ──

  async function loadStatus() {
    try {
      state.status = await getJSON('/api/zotero/status');
      state.synced = state.status.synced_collections || [];
    } catch (e) {
      state.status = null;
    }
    renderStatus();
    var busy = state.status && (state.status.phase === 'running' || (state.status.rows || []).some(function (r) { return r.tone === 'busy'; }));
    clearTimeout(state.pollTimer);
    var section = document.getElementById('zotero-settings');
    if (busy && section && section.classList.contains('active')) {
      state.pollTimer = setTimeout(function () {
        loadStatus().then(function () {
          if (state.status && state.status.phase !== 'running') loadOverview();
        });
      }, state.status.phase === 'running' ? 1200 : 3000);
    }
  }

  async function loadOverview() {
    try {
      var overview = await getJSON('/api/zotero/overview');
      state.connection = overview.connection;
      state.collections = overview.collections || [];
      var parse = document.getElementById('zotero-parse-mode');
      if (parse && overview.pdf_parse_mode) parse.textContent = overview.pdf_parse_mode.label;
    } catch (e) {
      state.connection = { state: 'error', label: '连接失败', message: e.message };
      state.collections = [];
    }
    renderConnection();
    renderTree();
    renderStatus();
  }

  async function openZoteroSettings() {
    if (state.loading) return;
    state.loading = true;
    try {
      var prefs = await getJSON('/api/preferences');
      state.enabled = !!prefs.zotero_sync_enabled;
      state.selected = prefs.zotero_sync_collections || [];
      state.frequency = prefs.zotero_sync_frequency || 'launch';
      renderSettings();
      await loadStatus();
      await loadOverview();
      loadPreview();
    } finally {
      state.loading = false;
    }
  }

  async function recheckZotero() {
    var button = document.getElementById('zotero-recheck');
    if (button) button.disabled = true;
    state.connection = { state: 'checking', label: '检测中' };
    renderConnection();
    try { await loadOverview(); } finally { if (button) button.disabled = false; }
  }

  async function syncZoteroNow() {
    try {
      await getJSON('/api/zotero/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    } catch (e) {
      showToast('无法开始同步：' + e.message);
      return;
    }
    state.status = { phase: 'running', message: '正在读取 Zotero', rows: [] };
    renderStatus();
    setTimeout(loadStatus, 400);
  }

  async function setZoteroEnabled(enabled) {
    var previous = state.enabled;
    state.enabled = !!enabled;
    renderSettings();
    renderStatus();
    try {
      var saved = await savePreferences({ zotero_sync_enabled: state.enabled });
      state.enabled = !!saved.zotero_sync_enabled;
    } catch (e) {
      state.enabled = previous;
      showToast('同步开关保存失败：' + e.message);
    }
    renderSettings();
    renderStatus();
  }

  async function setZoteroFrequency(frequency) {
    var select = document.getElementById('zotero-frequency-select');
    if (select) {
      select.classList.remove('is-open');
      var trigger = select.querySelector('.app-select-trigger');
      if (trigger) { trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); }
    }
    if (!FREQUENCY_LABELS[frequency]) return;
    var previous = state.frequency;
    state.frequency = frequency;
    renderSettings();
    try {
      var saved = await savePreferences({ zotero_sync_frequency: frequency });
      state.frequency = saved.zotero_sync_frequency || frequency;
    } catch (e) {
      state.frequency = previous;
      showToast('自动同步设置保存失败：' + e.message);
    }
    renderSettings();
  }

  var zoteroAPI = {
    open: openZoteroSettings,
    recheck: recheckZotero,
    sync: syncZoteroNow,
    setEnabled: setZoteroEnabled,
    setFrequency: setZoteroFrequency
  };
  global.MEFinder = global.MEFinder || {};
  global.MEFinder.zotero = zoteroAPI;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
