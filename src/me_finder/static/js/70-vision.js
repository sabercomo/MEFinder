/* ═══ MinerU API settings ═══ */
var mineruAccounts = [];
var mineruStatistics = {parsed_book_count:0, parsed_page_count:0, credentials:[]};
var parserStatistics = {total:{parsed_book_count:0, parsed_page_count:0, provider_count:0}, providers:[]};
var mineruSelectedAccountId = '';

async function loadMineruConfig() {
  var status = document.getElementById('mineru-config-status');
  if (!status) return;
  status.className = 'settings-status';
  status.textContent = '读取中…';
  try {
    var resp = await fetch('/api/mineru-accounts');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    mineruAccounts = Array.isArray(data.accounts) ? data.accounts : [];
    mineruAccounts.sort(function(left, right) {
      return String(left.display_name || left.account_id || '').localeCompare(
        String(right.display_name || right.account_id || ''),
        'zh-CN',
        {numeric:true, sensitivity:'base'}
      );
    });
    mineruStatistics = data.statistics || {parsed_book_count:0, parsed_page_count:0, credentials:[]};
    document.getElementById('mineru-api-base').value = data.api_base || 'https://mineru.net';
    renderMineruLocalSettings(data.local_deployment || {});
    renderMineruAccountList();
    var addButton = document.getElementById('mineru-add-account');
    if (addButton) addButton.hidden = !mineruAccounts.length;
    if (!mineruAccounts.length) startAddMineruAccount(false);
    var enabledCount = mineruAccounts.filter(function(item) { return item.enabled && item.configured; }).length;
    if (enabledCount) {
      status.className = 'settings-status ready';
      status.textContent = '已配置 ' + enabledCount + ' 个可用账号';
    } else {
      status.className = 'settings-status warning';
      status.textContent = mineruAccounts.length ? '账号均未启用' : '尚未添加账号';
    }
    mineruConfigLoaded = true;
  } catch (e) {
    status.className = 'settings-status warning';
    status.textContent = '读取失败';
    showToast('读取 MinerU 配置失败：' + e.message);
  }
}

function renderMineruLocalSettings(config) {
  var endpoint = document.getElementById('mineru-local-endpoint');
  var backend = document.getElementById('mineru-local-backend');
  var enabled = document.getElementById('mineru-local-enabled');
  if (endpoint) endpoint.value = config.endpoint || 'http://127.0.0.1:8000';
  if (backend) backend.value = config.backend || 'pipeline';
  if (enabled) enabled.checked = !!config.enabled;
  syncMineruLocalImportOption(!!config.enabled);
  updateMineruLocalStatus(!!config.enabled);
}

function syncMineruLocalImportOption(enabled) {
  var option = document.getElementById('mineru-local-parse-option');
  if (!option) return;
  option.hidden = !enabled;
  var input = option.querySelector('input[name="pdf-parse-mode"]');
  if (!enabled && input && input.checked) {
    var automatic = document.querySelector('input[name="pdf-parse-mode"][value="auto"]');
    if (automatic) automatic.checked = true;
  } else if (enabled && input && currentPdfParseMode === 'mineru-local') {
    input.checked = true;
  }
}

function updateMineruLocalStatus(enabled, label) {
  var status = document.getElementById('mineru-local-status');
  if (!status) return;
  status.className = 'settings-status' + (enabled ? ' ready' : '');
  status.textContent = label || (enabled ? '已启用' : '未启用');
}

function mineruLocalPayload() {
  return {
    endpoint: document.getElementById('mineru-local-endpoint').value.trim(),
    backend: document.getElementById('mineru-local-backend').value.trim(),
    enabled: document.getElementById('mineru-local-enabled').checked
  };
}

async function saveMineruLocalSettings() {
  var button = document.getElementById('mineru-local-save');
  var hint = document.getElementById('mineru-local-hint');
  button.disabled = true;
  button.textContent = '保存中…';
  if (hint) hint.textContent = '';
  try {
    var response = await fetch('/api/mineru-local', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(mineruLocalPayload())
    });
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '保存失败');
    renderMineruLocalSettings(data);
    importQueue.filter(function(item) {
      return item.jobId && (item.status === 'failed' || item.status === 'paused');
    }).forEach(function(item) { pollImportJob(item.id); });
    if (hint) hint.textContent = data.enabled ? '已保存；导入时可直接选择“本地 MinerU”' : '已关闭本地部署选项';
  } catch (error) {
    if (hint) hint.textContent = '未保存：' + error.message;
  } finally {
    button.disabled = false;
    button.textContent = '保存设置';
  }
}

async function testMineruLocalConnection() {
  var button = document.getElementById('mineru-local-test');
  var hint = document.getElementById('mineru-local-hint');
  button.disabled = true;
  button.textContent = '检测中…';
  if (hint) hint.textContent = '正在连接本地服务…';
  try {
    var response = await fetch('/api/mineru-local/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(mineruLocalPayload())
    });
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '连接失败');
    if (hint) hint.textContent = '连接成功 · ' + data.latency_ms + ' ms';
  } catch (error) {
    if (hint) hint.textContent = '连接失败：' + error.message;
    var status = document.getElementById('mineru-local-status');
    if (status) { status.className = 'settings-status warning'; status.textContent = '连接失败'; }
  } finally {
    button.disabled = false;
    button.textContent = '检测连接';
  }
}

function renderMineruAccountList() {
  // Original table: 账号 / 状态 / 到期日期 / 本地解析 / 操作. Edit opens the inline
  // editor panel below (no modal).
  var list = document.getElementById('mineru-account-list');
  if (!list) return;
  var count = document.getElementById('mineru-account-count');
  if (count) count.textContent = mineruAccounts.length.toLocaleString() + ' 个账号';
  if (!mineruAccounts.length) { list.innerHTML = ''; return; }
  var usageByAccount = {};
  (Array.isArray(mineruStatistics.credentials) ? mineruStatistics.credentials : []).forEach(function(item) {
    usageByAccount[item.account_id] = item;
  });
  var rows = mineruAccounts.map(function(item) {
    var usage = usageByAccount[item.account_id] || {};
    var healthy = item.health_status === 'healthy' || !item.health_status;
    var state = !item.configured ? '缺少 Token' : !item.enabled ? '已停用'
      : item.health_status === 'unauthorized' ? '认证失效'
      : item.health_status === 'cooldown' ? '冷却中' : '可用';
    var stateClass = item.enabled && item.configured && healthy ? 'ready' : 'warning';
    var expires = item.expires_at ? esc(item.expires_at.replace(/-/g, '/')) : '—';
    var usageLabel = Number(usage.parsed_book_count || 0).toLocaleString() + ' 本 · ' + Number(usage.parsed_page_count || 0).toLocaleString() + ' 页';
    return '<tr><td data-label="账号"><span class="mineru-account-identity"><span class="mineru-account-avatar" aria-hidden="true"><span class="mineru-brand-glyph"></span></span><span class="mineru-account-copy"><strong>' + esc(item.display_name) + '</strong><small>' + (item.configured ? 'Token 已保存' : '需要 Token') + '</small></span></span></td>' +
      '<td data-label="状态"><span class="mineru-account-status-cell"><span class="mineru-account-state ' + stateClass + '">' + esc(state) + '</span><label class="ui-switch mineru-row-switch" title="' + (item.enabled ? '停用账号' : '启用账号') + '"><input type="checkbox" data-account-id="' + esc(item.account_id) + '" ' + (item.enabled ? 'checked ' : '') + 'onchange="toggleMineruAccountEnabled(this)"><span class="ui-switch-track" aria-hidden="true"></span><span class="visually-hidden">' + (item.enabled ? '停用' : '启用') + ' ' + esc(item.display_name) + '</span></label></span></td>' +
      '<td data-label="到期日期"><span class="mineru-table-date">' + expires + '</span></td>' +
      '<td data-label="本地解析"><span class="mineru-table-usage">' + usageLabel + '</span></td>' +
      '<td data-label="操作"><span class="mineru-row-actions"><button class="mineru-text-action" type="button" data-account-id="' + esc(item.account_id) + '" onclick="testMineruConnection(this.dataset.accountId, this)">测试</button><button class="mineru-text-action" type="button" data-account-id="' + esc(item.account_id) + '" onclick="selectMineruAccount(this.dataset.accountId)">编辑</button><button class="mineru-text-action danger" type="button" data-account-id="' + esc(item.account_id) + '" onclick="deleteMineruAccount(this.dataset.accountId)">删除</button></span></td></tr>';
  }).join('');
  list.innerHTML = '<div class="mineru-account-table-scroll"><table class="mineru-account-table"><thead><tr><th>账号</th><th>状态</th><th>到期日期</th><th>本地解析</th><th><span class="visually-hidden">操作</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}

// The inline editor panel expands below the table on 添加账号 / 编辑, and folds
// away on save / cancel — no modal.
function showMineruEditor() {
  var card = document.getElementById('mineru-editor-card');
  if (card) { card.hidden = false; card.scrollIntoView({behavior: 'smooth', block: 'nearest'}); }
}
function hideMineruEditor() {
  var card = document.getElementById('mineru-editor-card');
  if (card) card.hidden = true;
  mineruSelectedAccountId = '';
}

function mineruEditorPrep() {
  var error = document.getElementById('mineru-dialog-error');
  if (error) { error.hidden = true; error.textContent = ''; }
  var token = document.getElementById('mineru-token'); if (token) token.type = 'password';
  var toggle = document.getElementById('mineru-token-toggle'); if (toggle) toggle.textContent = '显示';
  var editing = !!document.getElementById('mineru-account-id').value.trim();
  var test = document.getElementById('mineru-account-test'); if (test) test.hidden = !editing;
}

function startAddMineruAccount(shouldFocus) {
  var firstAccount = mineruAccounts.length === 0;
  mineruSelectedAccountId = '';
  document.getElementById('mineru-account-id').value = '';
  document.getElementById('mineru-account-name').value = firstAccount ? 'MinerU 账号' : 'MinerU 账号 ' + (mineruAccounts.length + 1);
  document.getElementById('mineru-token').value = '';
  document.getElementById('mineru-expires-at').value = '';
  document.getElementById('mineru-account-enabled').checked = true;
  document.getElementById('mineru-editor-title').textContent = firstAccount ? '配置 MinerU API' : '添加 MinerU 账号';
  document.getElementById('mineru-token-help').textContent = '新账号必填；可粘贴原始 Token 或完整 Bearer 值。Token 只保存在本机';
  document.getElementById('mineru-account-save').textContent = firstAccount ? '保存配置' : '保存账号';
  document.getElementById('mineru-account-cancel').hidden = firstAccount;
  mineruEditorPrep();
  showMineruEditor();
  if (shouldFocus !== false) setTimeout(function() { var n = document.getElementById('mineru-account-name'); if (n) n.focus(); }, 0);
}

function selectMineruAccount(accountId) {
  var item = mineruAccounts.find(function(account) { return account.account_id === accountId; });
  if (!item) return;
  mineruSelectedAccountId = item.account_id;
  document.getElementById('mineru-account-id').value = item.account_id;
  document.getElementById('mineru-account-name').value = item.display_name || '';
  document.getElementById('mineru-token').value = '';
  document.getElementById('mineru-expires-at').value = item.expires_at || '';
  document.getElementById('mineru-account-enabled').checked = !!item.enabled;
  document.getElementById('mineru-editor-title').textContent = '编辑 ' + item.display_name;
  document.getElementById('mineru-token-help').textContent = '留空会保留已保存的 Token';
  document.getElementById('mineru-account-save').textContent = '保存更改';
  document.getElementById('mineru-account-cancel').hidden = false;
  mineruEditorPrep();
  showMineruEditor();
}

// Kept for existing call sites. Cancel/close just folds the inline editor away.
function openMineruAccountDialog() { showMineruEditor(); }
function closeMineruAccountDialog() { hideMineruEditor(); }

function mineruDialogError(message) {
  var error = document.getElementById('mineru-dialog-error');
  if (!error) return;
  error.textContent = message;
  error.hidden = false;
}

function mineruPageRangesLabel(ranges) {
  if (!Array.isArray(ranges) || !ranges.length) return '—';
  return ranges.map(function(range) {
    if (!Array.isArray(range) || range.length < 2) return '';
    return Number(range[0]).toLocaleString() + '–' + Number(range[1]).toLocaleString();
  }).filter(Boolean).join('、');
}

function parserDateLabel(value) {
  if (!value) return '—';
  var date = new Date(value);
  if (Number.isNaN(date.getTime())) return esc(String(value));
  return date.toLocaleDateString('zh-CN', {year:'numeric', month:'2-digit', day:'2-digit'});
}

function renderParserProviderBooks(provider) {
  var rows = (Array.isArray(provider.books) ? provider.books : []).map(function(book) {
    var model = book.model ? esc(book.model) : '—';
    return '<tr><td data-label="文献"><span class="parser-book-title"><strong>' + esc(book.title || book.file_name || '未命名文献') + '</strong><small>' + esc(book.file_name || book.source_file_id || '') + '</small></span></td><td data-label="模型">' + model + '</td><td data-label="完成日期">' + parserDateLabel(book.completed_at) + '</td><td data-label="页数"><b>' + Number(book.parsed_page_count || 0).toLocaleString() + '</b> 页</td></tr>';
  }).join('');
  return '<div class="parser-detail-label">解析文献</div><div class="parser-book-table-scroll"><table class="parser-book-table"><thead><tr><th>文献</th><th>模型</th><th>完成日期</th><th>页数</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
}

function renderMineruCredentialAttribution(credentials) {
  if (!Array.isArray(credentials) || !credentials.length) return '<div class="parser-credential-empty">这些 MinerU 文献没有可匹配的本地账号归属记录。</div>';
  return '<div class="parser-detail-label mineru-attribution-label">MinerU 账号归属 <small>本地记录，不是官网用量或计费数据</small></div><div class="parser-credential-list">' + credentials.map(function(item) {
    var bookRows = (Array.isArray(item.books) ? item.books : []).map(function(book) {
      return '<div class="parser-credential-book"><span><strong>' + esc(book.source_file_name || book.document_id || '未命名文献') + '</strong><small>原书页 ' + esc(mineruPageRangesLabel(book.page_ranges)) + '</small></span><b>' + Number(book.parsed_page_count || 0).toLocaleString() + ' 页</b></div>';
    }).join('');
    return '<details class="parser-credential-account"><summary><span><strong>' + esc(item.display_name || item.account_id) + '</strong><small>' + Number(item.parsed_book_count || 0).toLocaleString() + ' 本文献</small></span><b>' + Number(item.parsed_page_count || 0).toLocaleString() + ' 页</b><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></summary><div class="parser-credential-books">' + bookRows + '</div></details>';
  }).join('') + '</div>';
}

function renderParserStatistics() {
  var total = parserStatistics.total || {};
  document.getElementById('parser-stat-books').textContent = Number(total.parsed_book_count || 0).toLocaleString();
  document.getElementById('parser-stat-pages').textContent = Number(total.parsed_page_count || 0).toLocaleString();
  document.getElementById('parser-stat-providers').textContent = Number(total.provider_count || 0).toLocaleString();
  var list = document.getElementById('parser-provider-list');
  var providers = Array.isArray(parserStatistics.providers) ? parserStatistics.providers : [];
  if (!providers.length) {
    list.innerHTML = '<div class="parser-statistics-empty"><strong>还没有解析统计</strong><small>导入并完成一本 PDF 的页级解析后，这里会按解析服务显示文献和页数。</small></div>';
    return;
  }
  var orderedProviders = providers.slice().sort(function(a, b) {
    var aLocal = a.provider_kind === 'local' ? 0 : 1;
    var bLocal = b.provider_kind === 'local' ? 0 : 1;
    return aLocal - bLocal;
  });
  list.innerHTML = orderedProviders.map(function(provider, index) {
    var isMineru = provider.provider_id === 'mineru-cloud' || provider.provider_id === 'mineru-local';
    var isCloudMineru = provider.provider_id === 'mineru-cloud';
    var kind = provider.provider_kind === 'local' ? '本地' : 'API';
    var details = renderParserProviderBooks(provider);
    if (isCloudMineru) details += renderMineruCredentialAttribution(provider.credentials || []);
    var providerMark = isMineru ? '<span class="mineru-brand-glyph"></span>' : esc(String(provider.provider_name || '?').charAt(0).toUpperCase());
    return '<details class="parser-provider-group" open><summary><span class="parser-provider-identity"><span class="parser-provider-mark ' + (isMineru ? 'mineru' : '') + '" aria-hidden="true">' + providerMark + '</span><span><strong>' + esc(provider.provider_name || provider.provider_id) + '</strong><small>' + kind + '</small></span></span><span class="parser-provider-number"><b>' + Number(provider.parsed_book_count || 0).toLocaleString() + '</b> 本</span><span class="parser-provider-number"><b>' + Number(provider.parsed_page_count || 0).toLocaleString() + '</b> 页</span><svg class="parser-provider-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg></summary><div class="parser-provider-detail">' + details + '</div></details>';
  }).join('');
}

async function loadParserStatistics() {
  var status = document.getElementById('parser-statistics-status');
  if (status) {
    status.className = 'settings-status';
    status.textContent = '刷新中…';
  }
  try {
    var response = await fetch('/api/parser-statistics');
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '读取失败');
    parserStatistics = data || {total:{parsed_book_count:0, parsed_page_count:0, provider_count:0}, providers:[]};
    renderParserStatistics();
    renderVisionProviders();
    if (status) {
      status.className = 'settings-status ready';
      status.textContent = '已刷新';
    }
  } catch (error) {
    if (status) {
      status.className = 'settings-status warning';
      status.textContent = '读取失败';
    }
    showToast('读取本地解析统计失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
  }
}

function loadMineruStatistics() {
  return loadParserStatistics();
}

async function exportBackup() {
  var hint = document.getElementById('backup-export-hint');
  try {
    var outputDirectory = await chooseDesktopExportDirectory();
    if (outputDirectory === null) return;
    if (hint) hint.textContent = '正在导出…';
    var payload = {};
    if (outputDirectory) payload.output_dir = outputDirectory;
    var resp = await fetch('/api/backup/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导出失败');
    if (hint) hint.textContent = '已导出到：' + data.path;
    showToast('备份已导出（' + formatFileSize(data.size_bytes) + '）');
  } catch (e) {
    if (hint) hint.textContent = '仅备份页码、书目和偏好，不含 PDF';
    showToast('导出备份失败：' + e.message);
  }
}

async function importBackup() {
  var button = document.getElementById('backup-import-choose');
  if (button && button.disabled) return;
  if (button) { button.disabled = true; button.textContent = '正在选择…'; }
  try {
    var chooseResp = await fetch('/api/backup/import/choose', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    var chosen = await chooseResp.json();
    if (!chooseResp.ok || chosen.error) throw new Error(chosen.error || '选择备份失败');
    if (chosen.cancelled) return;
    if (!await showAppConfirm(
      '将从“' + (chosen.name || '所选备份') + '”恢复，并覆盖当前的页码映射与书目信息。',
      {title:'导入并覆盖当前数据？', confirmText:'确认导入', tone:'danger'}
    )) return;
    if (button) button.textContent = '正在导入…';
    var resp = await fetch('/api/backup/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: chosen.path})});
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
    showToast('已恢复备份，正在重建索引…');
    pollBackupRestore(data.job_id);
  } catch (e) {
    showToast('导入备份失败：' + e.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = '选择备份并恢复'; }
  }
}

function pollBackupRestore(jobId) {
  fetch('/api/import-status?job_id=' + encodeURIComponent(jobId))
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      if (data.status === 'completed') {
        showToast(data.message || '备份已恢复');
        invalidateLibraryCatalog();
        loadMeta();
        return;
      }
      if (data.status === 'failed' || data.error) {
        showToast('恢复失败：' + (data.message || data.error || '未知错误'));
        return;
      }
      setTimeout(function() { pollBackupRestore(jobId); }, 2000);
    })
    .catch(function() { setTimeout(function() { pollBackupRestore(jobId); }, 4000); });
}

function toggleMineruSecret(inputId, buttonId) {
  var input = document.getElementById(inputId);
  var button = document.getElementById(buttonId);
  if (!input || !button) return;
  var visible = input.type === 'text';
  input.type = visible ? 'password' : 'text';
  button.textContent = visible ? '显示' : '隐藏';
}

async function saveMineruConfig(event) {
  if (event) event.preventDefault();
  var accountId = document.getElementById('mineru-account-id').value.trim();
  var saveButton = document.getElementById('mineru-account-save');
  var payload = {
    account_id: accountId || null,
    display_name: document.getElementById('mineru-account-name').value.trim(),
    token: document.getElementById('mineru-token').value.trim(),
    api_base: document.getElementById('mineru-api-base').value.trim(),
    expires_at: document.getElementById('mineru-expires-at').value,
    enabled: document.getElementById('mineru-account-enabled').checked
  };
  if (!payload.display_name) { mineruDialogError('请填写账号名称。'); return; }
  if (!accountId && !payload.token) { mineruDialogError('新账号必须填写 API Token。'); return; }
  var idleLabel = accountId ? '保存更改' : (mineruAccounts.length ? '添加账号' : '保存配置');
  saveButton.disabled = true;
  saveButton.textContent = '保存中…';
  try {
    var resp = await fetch('/api/mineru-accounts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    mineruSelectedAccountId = data.saved_account_id || accountId;
    mineruConfigLoaded = false;
    await loadMineruConfig();
    hideMineruEditor();
  } catch (e) {
    mineruDialogError('账号未保存。' + e.message);
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = idleLabel;
  }
}

async function saveMineruServiceAddress() {
  var button = document.getElementById('mineru-service-save');
  var status = document.getElementById('mineru-config-status');
  var apiBase = document.getElementById('mineru-api-base').value.trim();
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    var resp = await fetch('/api/mineru-accounts/service', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_base: apiBase})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    document.getElementById('mineru-api-base').value = data.api_base || apiBase;
    if (status) { status.className = 'settings-status ready'; status.textContent = '地址已保存'; }
  } catch (e) {
    showToast('MinerU 服务地址未保存：' + e.message, 'danger');
  } finally {
    button.disabled = false;
    button.textContent = '保存地址';
  }
}

async function toggleMineruAccountEnabled(input) {
  var item = mineruAccounts.find(function(account) { return account.account_id === input.dataset.accountId; });
  if (!item) return;
  var previous = !!item.enabled;
  item.enabled = !!input.checked;
  renderMineruAccountList();
  try {
    var resp = await fetch('/api/mineru-accounts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        account_id: item.account_id,
        display_name: item.display_name,
        token: '',
        api_base: document.getElementById('mineru-api-base').value.trim(),
        expires_at: item.expires_at || '',
        enabled: item.enabled
      })
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    mineruAccounts = Array.isArray(data.accounts) ? data.accounts : mineruAccounts;
    mineruStatistics = data.statistics || mineruStatistics;
    renderMineruAccountList();
  } catch (e) {
    item.enabled = previous;
    renderMineruAccountList();
    showToast('账号状态未保存：' + e.message, 'danger');
  }
}

async function deleteMineruAccount(accountId) {
  var item = mineruAccounts.find(function(account) { return account.account_id === accountId; });
  if (!item || !await showAppConfirm(
    '将删除 MinerU 账号“' + item.display_name + '”及其在本机保存的 Token。已完成的解析统计会保留。',
    {title:'删除 MinerU 账号？', confirmText:'删除', tone:'danger'}
  )) return;
  try {
    var resp = await fetch('/api/mineru-accounts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'delete_account', account_id: accountId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '删除失败');
    if (mineruSelectedAccountId === accountId) hideMineruEditor();
    mineruConfigLoaded = false;
    await loadMineruConfig();
    showToast('MinerU 账号已删除');
  } catch (e) {
    showToast('删除 MinerU 账号失败：' + e.message, 'danger');
  }
}

async function testMineruConnection(accountId, button) {
  if (!accountId) return;
  var status = document.getElementById('mineru-config-status');
  // Button may be an icon or a text button — preserve its content.
  var buttonHTML = button ? button.innerHTML : null;
  if (button) { button.disabled = true; }
  if (status) { status.className = 'settings-status'; status.textContent = '测试连接中…'; }
  try {
    var resp = await fetch('/api/mineru-accounts/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({account_id: accountId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '测试失败');
    if (status) { status.className = 'settings-status ready'; status.textContent = '连接正常 · ' + data.latency_ms + ' ms'; }
  } catch (e) {
    if (status) { status.className = 'settings-status warning'; status.textContent = '连接失败'; }
    showToast('MinerU 连接失败：' + e.message, 'danger');
  } finally {
    if (button) { button.disabled = false; if (buttonHTML !== null) button.innerHTML = buttonHTML; }
  }
}

function bindMineruAccountDialogDismissal() {
  // The MinerU editor is inline now (no modal). Kept as a no-op so the
  // existing init call site stays valid.
}

/* ═══ Optional OpenAI-compatible vision providers ═══ */
var VISION_BRAND_RULES = [
  {re: /dashscope|aliyuncs/i, name: '通义千问', color: '#615CED', icon: 'qwen-color.svg', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1'},
  {re: /moonshot/i, name: '月之暗面 Kimi', color: '#1E1F24', icon: 'kimi-color.svg', iconBg: '#101319', base: 'https://api.moonshot.cn/v1'},
  {re: /deepseek/i, name: '深度求索 DeepSeek', color: '#4D6BFE', icon: 'deepseek-color.svg', base: 'https://api.deepseek.com'},
  {re: /bigmodel|zhipu/i, name: '智谱 GLM', color: '#3859FF', icon: 'zhipu-color.svg', base: 'https://open.bigmodel.cn/api/paas/v4'},
  {re: /siliconflow/i, name: '硅基流动', color: '#7C3AED', icon: 'siliconcloud-color.svg', base: 'https://api.siliconflow.cn/v1'},
  {re: /volces|volcengine|doubao/i, name: '火山方舟（豆包）', color: '#3370FF', icon: 'doubao-color.svg', base: 'https://ark.cn-beijing.volces.com/api/v3'},
  {re: /hunyuan/i, name: '腾讯混元', color: '#0052D9', icon: 'hunyuan-color.svg', base: 'https://api.hunyuan.cloud.tencent.com/v1'},
  {re: /baidubce|qianfan/i, name: '百度千帆', color: '#2932E1', icon: 'wenxin-color.svg', base: 'https://qianfan.baidubce.com/v2'},
  {re: /stepfun/i, name: '阶跃星辰', color: '#0057FF', icon: 'stepfun-color.svg', base: 'https://api.stepfun.com/v1'},
  {re: /minimax/i, name: 'MiniMax', color: '#F23F5D', icon: 'minimax-color.svg', base: 'https://api.minimaxi.com/v1'},
  {re: /openrouter/i, name: 'OpenRouter', color: '#8B5CF6', icon: 'openrouter-color.svg', base: 'https://openrouter.ai/api/v1'},
  {re: /openai\.com/i, name: 'OpenAI', color: '#10A37F', icon: 'openai.svg', base: 'https://api.openai.com/v1'},
  {re: /googleapis|gemini/i, name: 'Gemini', color: '#4285F4', icon: 'gemini-color.svg', base: 'https://generativelanguage.googleapis.com/v1beta/openai'},
  {re: /anthropic/i, name: 'Claude', color: '#D97757', icon: 'claude-color.svg', base: 'https://api.anthropic.com/v1'},
  {re: /(^|\W)x\.ai|grok/i, name: 'Grok', color: '#1D1F23', icon: 'grok.svg', base: 'https://api.x.ai/v1'},
  {re: /mistral/i, name: 'Mistral', color: '#FA520F', icon: 'mistral-color.svg', base: 'https://api.mistral.ai/v1'},
  {re: /groq/i, name: 'Groq', color: '#F55036', icon: 'groq.svg', base: 'https://api.groq.com/openai/v1'},
  {re: /together/i, name: 'Together', color: '#0F6FFF', icon: 'together-color.svg', base: 'https://api.together.xyz/v1'}
];
var VISION_AVATAR_PALETTE = ['#1677FF', '#7B5EC7', '#C9446A', '#B85C2B', '#637A50', '#0E8A8A', '#B0499B', '#4D6BFE'];
var VISION_PLUS_SVG = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M10 4.5v11M4.5 10h11"/></svg>';
var VISION_BOLT_SVG = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M11 2.5 4.5 11H9l-1 6.5L14.5 9H10l1-6.5z"/></svg>';
var VISION_TRASH_SVG = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 5.5h13M8 5.2V3.5h4v1.7M5.2 5.5l.7 11h8.2l.7-11M8.2 8.5v5.2M11.8 8.5v5.2"/></svg>';

function visionBrandFromBase(apiBase) {
  if (!apiBase) return null;
  for (var i = 0; i < VISION_BRAND_RULES.length; i++) {
    if (VISION_BRAND_RULES[i].re.test(apiBase)) return VISION_BRAND_RULES[i];
  }
  var host = '';
  try {
    host = new URL(apiBase.indexOf('://') >= 0 ? apiBase : 'https://' + apiBase).hostname;
  } catch (e) {
    return null;
  }
  if (!host) return null;
  var parts = host.split('.').filter(Boolean);
  var label = parts.length > 1 ? parts[parts.length - 2] : parts[0];
  if ((label === 'api' || !label) && parts.length) label = parts[0];
  if (!label) return null;
  return {
    name: label.charAt(0).toUpperCase() + label.slice(1),
    color: VISION_AVATAR_PALETTE[visionHash(host) % VISION_AVATAR_PALETTE.length]
  };
}

function visionAvatarFor(provider) {
  var brand = visionBrandFromBase(provider.api_base);
  var name = (provider.name || (brand && brand.name) || '').trim();
  var color = (brand && brand.color)
    || VISION_AVATAR_PALETTE[visionHash(name || '?') % VISION_AVATAR_PALETTE.length];
  return {letter: (name.charAt(0) || '?').toUpperCase(), color: color};
}

function visionAvatarHtml(provider, extraClass) {
  var brand = visionBrandFromBase(provider.api_base);
  var cls = 'vision-avatar' + (extraClass ? ' ' + extraClass : '');
  if (brand && brand.icon) {
    return '<span class="' + cls + ' has-icon"' + (brand.iconBg ? ' style="background:' + brand.iconBg + '"' : '')
      + '><img src="/static/brands/' + brand.icon + '" alt=""></span>';
  }
  var info = visionAvatarFor(provider);
  return '<span class="' + cls + '" style="background:' + info.color + '">' + esc(info.letter) + '</span>';
}

/* API 地址的常见服务商下拉 */
var visionBasePopOpen = false;
var visionBaseActiveIndex = -1;
var visionBaseFlat = [];
var visionBaseShowAll = false;

function visionBaseFiltered() {
  var input = document.getElementById('vision-api-base');
  var query = input ? input.value.trim().toLowerCase() : '';
  var presets = VISION_BRAND_RULES.filter(function(rule) { return rule.base; });
  if (!query || visionBaseShowAll) return presets;
  // A field holding exactly one preset's address should still offer the others,
  // so switching providers does not require clearing it first.
  if (presets.some(function(rule) { return rule.base.toLowerCase() === query; })) return presets;
  return presets.filter(function(rule) {
    return rule.name.toLowerCase().indexOf(query) >= 0
      || rule.base.toLowerCase().indexOf(query) >= 0;
  });
}

// 两套下拉（base 服务商 / model 模型）弹层的可见性样板完全同构：命中「关」时
// 隐藏并清空、置 aria-expanded=false；命中「开」时显示、置 aria-expanded=true
// 并把选中项滚入视野。抽出复用，两套 render 各自的分组构建、头像、
// visionModelFlat 填充等差异逻辑一律不动。
function hideVisionPop(pop, input) {
  pop.hidden = true;
  pop.innerHTML = '';
  if (input) input.setAttribute('aria-expanded', 'false');
}

function revealVisionPop(pop, input) {
  pop.hidden = false;
  if (input) input.setAttribute('aria-expanded', 'true');
  var active = pop.querySelector('.vision-model-item.active');
  if (active) active.scrollIntoView({block: 'nearest'});
}

function renderVisionBasePop() {
  var pop = document.getElementById('vision-base-pop');
  var input = document.getElementById('vision-api-base');
  var toggle = document.getElementById('vision-base-toggle');
  if (!pop) return;
  visionBaseFlat = visionBaseFiltered();
  if (!visionBasePopOpen || !visionBaseFlat.length) {
    hideVisionPop(pop, input);
    if (toggle) toggle.classList.remove('is-open');
    return;
  }
  if (toggle) toggle.classList.add('is-open');
  pop.innerHTML = '<div class="vision-model-group">常见服务商</div>'
    + visionBaseFlat.map(function(rule, index) {
        return '<div class="vision-model-item vision-base-item' + (index === visionBaseActiveIndex ? ' active' : '')
          + '" data-base="' + esc(rule.base) + '">'
          + visionAvatarHtml({api_base: rule.base, name: rule.name}, 'vision-avatar-sm')
          + '<span class="vision-base-name">' + esc(rule.name) + '</span>'
          + (rule.unsupported ? '<span class="vision-model-badge capability-unsupported">不支持图片</span>' : '')
          + '<span class="vision-base-url">' + esc(rule.base.replace(/^https?:\/\//, '')) + '</span>'
          + '</div>';
      }).join('');
  revealVisionPop(pop, input);
}

function openVisionBasePop() {
  closeVisionModelPop();
  visionBasePopOpen = true;
  renderVisionBasePop();
}

function closeVisionBasePop() {
  if (!visionBasePopOpen) return;
  visionBasePopOpen = false;
  visionBaseActiveIndex = -1;
  visionBaseShowAll = false;
  renderVisionBasePop();
}

function toggleVisionBaseList(event) {
  if (event) event.stopPropagation();
  if (visionBasePopOpen && visionBaseShowAll) {
    closeVisionBasePop();
    return;
  }
  visionBaseShowAll = true;
  visionBaseActiveIndex = -1;
  openVisionBasePop();
  var input = document.getElementById('vision-api-base');
  if (input) input.focus();
}

function pickVisionBase(base) {
  var input = document.getElementById('vision-api-base');
  if (input) input.value = base || '';
  closeVisionBasePop();
  autoFillVisionName();
  maybeAutoFetchVisionModels();
  if (input) input.focus();
}

function visionBaseKeydown(event) {
  if (event.key === 'Escape') { closeVisionBasePop(); return; }
  if (!visionBasePopOpen) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      openVisionBasePop();
    }
    return;
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!visionBaseFlat.length) return;
    var delta = event.key === 'ArrowDown' ? 1 : -1;
    visionBaseActiveIndex = (visionBaseActiveIndex + delta + visionBaseFlat.length) % visionBaseFlat.length;
    renderVisionBasePop();
  } else if (event.key === 'Enter') {
    if (visionBaseActiveIndex >= 0 && visionBaseActiveIndex < visionBaseFlat.length) {
      event.preventDefault();
      pickVisionBase(visionBaseFlat[visionBaseActiveIndex].base);
    }
  }
}

function setVisionModelHint(message, state) {
  var hint = document.getElementById('vision-model-hint');
  if (!hint) return;
  hint.textContent = message;
  hint.className = 'vision-model-hint' + (state ? ' ' + state : '');
}

var visionModelPopOpen = false;
var visionModelActiveIndex = -1;
var visionModelFlat = [];

function visionModelFiltered() {
  var input = document.getElementById('vision-model');
  var query = input ? input.value.trim().toLowerCase() : '';
  if (!query) return visionModelOptions;
  var exactMatch = visionModelOptions.some(function(item) { return item.id.toLowerCase() === query; });
  if (exactMatch) return visionModelOptions;
  return visionModelOptions.filter(function(item) {
    return item.id.toLowerCase().indexOf(query) >= 0
      || String(item.owned_by || '').toLowerCase().indexOf(query) >= 0
      || String(item.capability_label || '').toLowerCase().indexOf(query) >= 0;
  });
}

function visionModelCapability(item) {
  var capability = String((item || {}).capability || '');
  if (capability === 'ocr') return 'ocr';
  if (capability === 'vision' || capability === 'omni') return 'vision';
  if (capability === 'text' || capability === 'unsupported') return 'text';
  if (capability === 'unknown') return 'unknown';
  return item && item.likely_vision ? 'vision' : 'unknown';
}

function visionModelPriority(item) {
  var priority = Number((item || {}).capability_priority);
  if (Number.isFinite(priority)) return priority;
  var fallback = {ocr: 0, vision: 100, unknown: 500, text: 900};
  return fallback[visionModelCapability(item)];
}

function visionModelBadgeHTML(item) {
  var label = String((item || {}).capability_label || '');
  var capability = visionModelCapability(item);
  if (capability === 'vision') label = '支持图片';
  else if (capability === 'text') label = '不支持图片';
  else if (capability === 'unknown') label = '待确认 · 请测试';
  else if (!label) label = 'OCR 专用';
  return '<span class="vision-model-badge capability-' + capability + '">' + esc(label) + '</span>';
}

function renderVisionModelPop() {
  var pop = document.getElementById('vision-model-pop');
  var input = document.getElementById('vision-model');
  if (!pop) return;
  var items = visionModelFiltered();
  visionModelFlat = [];
  if (!visionModelPopOpen || !items.length) {
    hideVisionPop(pop, input);
    return;
  }
  var groups = [
    {key: 'ocr', label: 'OCR 专用 · 优先'},
    {key: 'vision', label: '支持图片'},
    {key: 'unknown', label: '待确认 · 请测试'},
    {key: 'text', label: '不支持图片'}
  ];
  var byCapability = {};
  items.forEach(function(item) {
    var capability = visionModelCapability(item);
    if (!byCapability[capability]) byCapability[capability] = [];
    byCapability[capability].push(item);
  });
  var html = groups.filter(function(group) {
    return byCapability[group.key] && byCapability[group.key].length;
  }).map(function(group) {
    return '<div class="vision-model-group">' + esc(group.label) + '</div>'
      + byCapability[group.key].map(function(item) {
          var index = visionModelFlat.length;
          visionModelFlat.push(item);
          return '<div class="vision-model-item' + (index === visionModelActiveIndex ? ' active' : '')
            + '" data-model="' + esc(item.id) + '">'
            + '<span class="vision-model-id">' + esc(item.id) + '</span>'
            + visionModelBadgeHTML(item)
            + '</div>';
        }).join('');
  }).join('');
  pop.innerHTML = html;
  revealVisionPop(pop, input);
}

function openVisionModelPop() {
  closeVisionBasePop();
  visionModelPopOpen = true;
  renderVisionModelPop();
}

function closeVisionModelPop() {
  if (!visionModelPopOpen) return;
  visionModelPopOpen = false;
  visionModelActiveIndex = -1;
  renderVisionModelPop();
}

function pickVisionModel(modelId) {
  var input = document.getElementById('vision-model');
  if (input) input.value = modelId || '';
  closeVisionModelPop();
  if (input) input.focus();
}

function visionModelKeydown(event) {
  if (event.key === 'Escape') { closeVisionModelPop(); return; }
  if (!visionModelPopOpen) {
    if (event.key === 'ArrowDown' && visionModelOptions.length) {
      event.preventDefault();
      openVisionModelPop();
    }
    return;
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!visionModelFlat.length) return;
    var delta = event.key === 'ArrowDown' ? 1 : -1;
    visionModelActiveIndex = (visionModelActiveIndex + delta + visionModelFlat.length) % visionModelFlat.length;
    renderVisionModelPop();
  } else if (event.key === 'Enter') {
    if (visionModelActiveIndex >= 0 && visionModelActiveIndex < visionModelFlat.length) {
      event.preventDefault();
      pickVisionModel(visionModelFlat[visionModelActiveIndex].id);
    }
  }
}

function clearVisionModelOptions(message) {
  visionModelOptions = [];
  visionModelActiveIndex = -1;
  renderVisionModelPop();
  if (message) setVisionModelHint(message, '');
}

function resetVisionModelButton() {
  var button = document.getElementById('vision-model-refresh');
  if (!button) return;
  button.disabled = false;
  button.textContent = '获取模型';
}

function renderVisionModelOptions(models) {
  visionModelOptions = (models || []).filter(function(item) {
    return item && typeof item.id === 'string' && item.id.trim();
  }).slice().sort(function(a, b) {
    return visionModelPriority(a) - visionModelPriority(b)
      || a.id.localeCompare(b.id, undefined, {sensitivity: 'base'});
  });
  visionModelActiveIndex = -1;
  renderVisionModelPop();
}

function currentVisionProviderDraft() {
  return {
    id: document.getElementById('vision-provider-id').value.trim(),
    name: document.getElementById('vision-provider-name').value.trim(),
    api_base: document.getElementById('vision-api-base').value.trim(),
    api_key: document.getElementById('vision-api-key').value.trim()
  };
}

function visionDraftHasUsableKey(provider) {
  if (provider.api_key) return true;
  if (!provider.id) return false;
  var saved = (visionConfig.providers || []).find(function(item) {
    return item.id === provider.id;
  });
  return !!(saved && saved.has_api_key);
}

async function fetchVisionModels(options) {
  options = options || {};
  var silent = !!options.silent;
  var provider = currentVisionProviderDraft();
  if (!provider.api_base) {
    setVisionModelHint('请先填写 API 地址；模型名称也可以手动输入', 'is-error');
    if (!silent) showToast('请先填写 API 地址');
    return;
  }
  if (!visionDraftHasUsableKey(provider)) {
    setVisionModelHint('请先填写 API Key；模型名称也可以手动输入', 'is-error');
    if (!silent) showToast('请先填写 API Key');
    return;
  }

  var requestSerial = ++visionModelRequestSerial;
  var button = document.getElementById('vision-model-refresh');
  if (button) {
    button.disabled = true;
    button.textContent = '获取中…';
  }
  setVisionModelHint('正在读取接口可用模型…', 'is-loading');
  try {
    var resp = await fetch('/api/vision-providers/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: provider})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '获取模型失败');
    if (requestSerial !== visionModelRequestSerial) return;
    renderVisionModelOptions(data.models || []);
    setVisionModelHint(
      '已获取 ' + visionModelOptions.length + ' 个模型。未确认型号可保存后发送测试图片验证',
      'is-ready'
    );
    if (!silent) {
      openVisionModelPop();
      showToast('已获取 ' + visionModelOptions.length + ' 个模型');
    }
  } catch (e) {
    if (requestSerial !== visionModelRequestSerial) return;
    clearVisionModelOptions();
    setVisionModelHint((e.message || '无法自动获取模型') + ' 仍可手动填写模型名称', 'is-error');
    if (!silent) showToast('获取模型失败：' + e.message);
  } finally {
    if (requestSerial === visionModelRequestSerial && button) {
      button.disabled = false;
      button.textContent = '获取模型';
    }
  }
}

function maybeAutoFetchVisionModels() {
  var provider = currentVisionProviderDraft();
  visionModelRequestSerial += 1;
  resetVisionModelButton();
  clearVisionModelOptions('地址或密钥已更新，正在准备读取模型…');
  if (provider.api_base && visionDraftHasUsableKey(provider)) {
    fetchVisionModels({silent: true});
  } else {
    setVisionModelHint('填写 API 地址和 Key 后会自动读取模型；接口不支持时仍可手动输入', '');
  }
}

function configuredVisionProviders() {
  return (visionConfig.providers || []).filter(function(provider) {
    return provider.enabled && provider.configured;
  });
}

var importVisionProviderQuery = '';

function selectedImportRecoveryProviderId() {
  var container = document.getElementById('import-recovery-provider');
  return container ? (container.dataset.value || '') : '';
}

function renderImportRecoveryProviderOptions() {
  var list = document.getElementById('import-recovery-provider-list');
  var container = document.getElementById('import-recovery-provider');
  if (!list || !container) return;
  var providers = configuredVisionProviders();
  var current = container.dataset.value || '';
  var check = '<svg class="app-select-check" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>';
  list.innerHTML = providers.map(function(provider) {
    var selected = provider.id === current;
    return '<button class="app-select-option import-vision-option' + (selected ? ' is-selected' : '') + '" type="button" role="option" aria-selected="' + selected + '" data-value="' + esc(provider.id) + '" onclick="selectImportRecoveryProvider(event,\'' + esc(provider.id) + '\')">'
      + visionAvatarHtml(provider, 'vision-avatar-sm')
      + '<span class="import-vision-opt"><span class="import-vision-opt-name">' + esc(provider.name) + ' · ' + esc(provider.model || '未选择模型') + '</span>'
      + '<span class="import-vision-opt-model">' + esc(visionHostLabel(provider.api_base)) + '</span></span>'
      + (selected ? check : '') + '</button>';
  }).join('') || '<div class="document-options-empty">请先在设置中配置</div>';
}

function updateImportRecoveryProviderLabel() {
  var container = document.getElementById('import-recovery-provider');
  var label = document.getElementById('import-recovery-provider-label');
  if (!container || !label) return;
  var providers = configuredVisionProviders();
  var provider = providers.find(function(item) { return item.id === (container.dataset.value || ''); });
  if (!provider) {
    label.textContent = providers.length ? '选择解析接口' : '请先在设置中配置';
    return;
  }
  label.innerHTML = visionAvatarHtml(provider, 'vision-avatar-sm')
    + '<span class="import-vision-name">' + esc(provider.name) + ' · ' + esc(provider.model || '未选择模型') + '</span>';
}

function syncImportRecoveryProvider() {
  var container = document.getElementById('import-recovery-provider');
  if (!container) return;
  var providers = configuredVisionProviders();
  var current = container.dataset.value || '';
  var recoveryJob = Array.isArray(importQueue) ? importQueue.find(function(item) {
    return item && item.status === 'error' && item.type === 'pdf'
      && item.failureStage !== 'index' && item.retryProviderId;
  }) : null;
  var preferred = (recoveryJob && recoveryJob.retryProviderId) || visionConfig.default_provider_id || '';
  var panel = document.getElementById('import-recovery-panel');
  var preferFailedJob = panel && !panel.hidden && container.dataset.userSelected !== 'true';
  if (preferFailedJob && providers.some(function(provider) { return provider.id === preferred; })) {
    current = preferred;
  } else if (!providers.some(function(provider) { return provider.id === current; })) {
    current = providers.some(function(provider) { return provider.id === preferred; }) ? preferred : (providers[0] || {}).id || '';
  }
  container.dataset.value = current;
  var trigger = container.querySelector('.app-select-trigger');
  if (trigger) trigger.disabled = providers.length === 0;
  container.classList.toggle('is-disabled', providers.length === 0);
  renderImportRecoveryProviderOptions();
  updateImportRecoveryProviderLabel();
}

function toggleImportRecoveryProvider(event) {
  toggleAppSelect(event, 'import-recovery-provider');
  renderImportRecoveryProviderOptions();
}

function selectImportRecoveryProvider(event, providerId) {
  if (event) event.stopPropagation();
  var container = document.getElementById('import-recovery-provider');
  if (container) {
    container.dataset.value = providerId;
    container.dataset.userSelected = 'true';
  }
  updateImportRecoveryProviderLabel();
  renderImportRecoveryProviderOptions();
  closeAppSelects();
  if (typeof renderImportQueue === 'function') renderImportQueue();
}

function importVisionProviderSearchText(provider) {
  return [provider.name, provider.model, provider.api_base, visionHostLabel(provider.api_base)]
    .map(function(value) { return String(value || '').toLowerCase(); })
    .join(' ');
}

function renderImportVisionProviderOptions() {
  var menu = document.getElementById('import-vision-provider-options');
  var container = document.getElementById('import-vision-provider');
  if (!menu || !container) return;
  var providers = configuredVisionProviders();
  var current = container.dataset.value || '';
  var query = importVisionProviderQuery.trim().toLowerCase();
  var filtered = query
    ? providers.filter(function(provider) { return importVisionProviderSearchText(provider).indexOf(query) >= 0; })
    : providers;
  var check = '<svg class="app-select-check" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 10 3 3 7-7"/></svg>';
  var search = providers.length > 8
    ? '<div class="import-vision-search-wrap"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg>'
      + '<input class="import-vision-search" id="import-vision-provider-filter" type="search" autocomplete="off" aria-label="搜索 API 或模型" placeholder="搜索 API、模型或 endpoint…" value="' + esc(importVisionProviderQuery) + '" oninput="filterImportVisionProviders(this.value)" onkeydown="importVisionSearchKeydown(event)"></div>'
    : '';
  var options = filtered.map(function(provider) {
    var selected = provider.id === current;
    return '<button class="app-select-option import-vision-option' + (selected ? ' is-selected' : '') + '" type="button" role="option" aria-selected="' + selected + '" data-value="' + esc(provider.id) + '" onclick="selectImportVisionProvider(event,\'' + esc(provider.id) + '\')" onkeydown="importVisionOptionKeydown(event)">'
      + visionAvatarHtml(provider, 'vision-avatar-sm')
      + '<span class="import-vision-opt"><span class="import-vision-opt-name">' + esc(provider.name) + ' · ' + esc(provider.model || '未选择模型') + '</span>'
      + '<span class="import-vision-opt-model">' + esc(visionHostLabel(provider.api_base)) + '</span></span>'
      + (selected ? check : '') + '</button>';
  }).join('');
  if (!providers.length) options = '<div class="document-options-empty">请先在设置中配置</div>';
  else if (!filtered.length) options = '<div class="document-options-empty">没有匹配的 API 或模型</div>';
  menu.innerHTML = search + '<div class="import-vision-option-list" id="import-vision-provider-list" role="listbox" aria-label="已配置的 API 与模型">' + options + '</div>';
  positionImportVisionMenu();
}

function syncImportVisionProviders() {
  var container = document.getElementById('import-vision-provider');
  var options = document.getElementById('import-vision-provider-options');
  if (!container || !options) return;
  var providers = configuredVisionProviders();
  var trigger = container.querySelector('.app-select-trigger');
  if (!providers.length) {
    container.dataset.value = '';
  } else {
    var current = container.dataset.value || '';
    var preferred = visionConfig.default_provider_id || '';
    if (!providers.some(function(provider) { return provider.id === current; })) {
      current = providers.some(function(provider) { return provider.id === preferred; }) ? preferred : providers[0].id;
    }
    container.dataset.value = current;
  }
  var configLink = document.getElementById('vision-parse-config-link');
  if (configLink) configLink.hidden = providers.length > 0;
  if (trigger) trigger.disabled = providers.length === 0;
  container.classList.toggle('is-disabled', providers.length === 0);
  renderImportVisionProviderOptions();
  updateImportVisionProviderLabel();
  syncImportRecoveryProvider();
}

async function toggleImportVisionProvider(event) {
  await toggleAppSelect(event, 'import-vision-provider');
  var container = document.getElementById('import-vision-provider');
  if (!container || !container.classList.contains('is-open')) return;
  importVisionProviderQuery = '';
  renderImportVisionProviderOptions();
  requestAnimationFrame(function() {
    positionImportVisionMenu();
    if (configuredVisionProviders().length > 8) {
      var input = document.getElementById('import-vision-provider-filter');
      if (input) input.focus();
    }
  });
}

function selectImportVisionProvider(event, providerId) {
  if (event) event.stopPropagation();
  var container = document.getElementById('import-vision-provider');
  if (container) container.dataset.value = providerId;
  importVisionProviderQuery = '';
  updateImportVisionProviderLabel();
  closeAppSelects();
  renderImportVisionProviderOptions();
  if (typeof renderImportQueue === 'function') renderImportQueue();
}

function updateImportVisionProviderLabel() {
  var container = document.getElementById('import-vision-provider');
  var label = document.getElementById('import-vision-provider-label');
  if (!container || !label) return;
  var providers = configuredVisionProviders();
  var provider = providers.find(function(item) { return item.id === (container.dataset.value || ''); });
  if (!provider) {
    label.textContent = providers.length ? '选择解析接口' : '请先在设置中配置';
    return;
  }
  label.innerHTML = visionAvatarHtml(provider, 'vision-avatar-sm')
    + '<span class="import-vision-name">' + esc(provider.name) + ' · ' + esc(provider.model || '未选择模型') + '</span>';
}

function filterImportVisionProviders(value) {
  importVisionProviderQuery = String(value || '');
  renderImportVisionProviderOptions();
  var input = document.getElementById('import-vision-provider-filter');
  if (input) {
    input.focus();
    var end = input.value.length;
    input.setSelectionRange(end, end);
  }
}

function importVisionVisibleOptions() {
  return Array.from(document.querySelectorAll('#import-vision-provider-list .import-vision-option'));
}

function focusImportVisionOption(index) {
  var options = importVisionVisibleOptions();
  if (!options.length) return;
  var target = options[(index + options.length) % options.length];
  target.focus();
  target.scrollIntoView({block: 'nearest'});
}

async function importVisionTriggerKeydown(event) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  event.preventDefault();
  event.stopPropagation();
  var container = document.getElementById('import-vision-provider');
  if (!container || !container.classList.contains('is-open')) await toggleImportVisionProvider(event);
  if (configuredVisionProviders().length > 8) return;
  var options = importVisionVisibleOptions();
  var selected = options.findIndex(function(option) { return option.classList.contains('is-selected'); });
  focusImportVisionOption(event.key === 'ArrowUp' ? (selected >= 0 ? selected : options.length) - 1 : selected >= 0 ? selected : 0);
}

function importVisionSearchKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeAppSelects();
    var trigger = document.querySelector('#import-vision-provider .app-select-trigger');
    if (trigger) trigger.focus();
  } else if (event.key === 'ArrowDown') {
    event.preventDefault();
    focusImportVisionOption(0);
  } else if (event.key === 'ArrowUp') {
    event.preventDefault();
    focusImportVisionOption(importVisionVisibleOptions().length - 1);
  }
}

function importVisionOptionKeydown(event) {
  var options = importVisionVisibleOptions();
  var index = options.indexOf(event.currentTarget);
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    focusImportVisionOption(index + (event.key === 'ArrowDown' ? 1 : -1));
  } else if (event.key === 'Home' || event.key === 'End') {
    event.preventDefault();
    focusImportVisionOption(event.key === 'Home' ? 0 : options.length - 1);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeAppSelects();
    var trigger = document.querySelector('#import-vision-provider .app-select-trigger');
    if (trigger) trigger.focus();
  }
}

function positionImportVisionMenu() {
  var container = document.getElementById('import-vision-provider');
  var menu = document.getElementById('import-vision-provider-options');
  var trigger = container ? container.querySelector('.app-select-trigger') : null;
  if (!container || !menu || !trigger || !container.classList.contains('is-open')) return;
  var rect = trigger.getBoundingClientRect();
  var viewportWidth = window.innerWidth;
  var viewportHeight = window.innerHeight;
  var margin = 12;
  var gap = 7;
  var width = Math.min(720, Math.max(rect.width, Math.min(440, viewportWidth - margin * 2)), viewportWidth - margin * 2);
  menu.style.width = Math.max(0, width) + 'px';
  menu.style.left = Math.max(margin, Math.min(rect.left, viewportWidth - margin - width)) + 'px';
  var height = Math.min(menu.scrollHeight, 360, viewportHeight - margin * 2);
  var below = viewportHeight - rect.bottom - gap - margin;
  var above = rect.top - gap - margin;
  var opensUp = below < height && above > below;
  var top = opensUp ? Math.max(margin, rect.top - gap - height) : Math.min(rect.bottom + gap, viewportHeight - margin - height);
  menu.style.top = Math.max(margin, top) + 'px';
  menu.dataset.placement = opensUp ? 'top' : 'bottom';
}

// Signature of the fields that决定连通性；一旦地址或模型变了，上次测连结果作废。
function visionProviderSig(provider) {
  return (provider.api_base || '') + '|' + (provider.model || '');
}

// Only ever返回 'ok' after a real 测试连接 succeeded for the current config.
function visionVerifiedState(provider) {
  var r = visionTestResults[provider.id];
  if (!r || r.sig !== visionProviderSig(provider)) return null;
  return r.ok ? 'ok' : 'failed';
}

function recordVisionTestResult(provider, ok) {
  if (!provider) return;
  visionTestResults[provider.id] = {sig: visionProviderSig(provider), ok: !!ok};
}

// Returns {label, cls} for the card badge. 绿色“已连通”只在真实测连成功后出现，
// 填完三个框但未验证的接口显示中性的“待测试”，避免像旧版那样误报“可用”。
function visionProviderBadge(provider) {
  if (!provider.enabled) return {label: '已停用', cls: ' muted'};
  if (!provider.configured) return {label: '缺少密钥', cls: ' warning'};
  var verified = visionVerifiedState(provider);
  if (verified === 'ok') return {label: '已连通', cls: ''};
  if (verified === 'failed') return {label: '连接失败', cls: ' warning'};
  return {label: '待测试', cls: ' pending'};
}

function renderVisionProviders() {
  var list = document.getElementById('vision-provider-list');
  var count = document.getElementById('vision-provider-count');
  var status = document.getElementById('vision-config-status');
  var autoFallback = document.getElementById('vision-auto-fallback');
  var fallbackSummary = document.getElementById('vision-fallback-summary');
  var readyProviders = configuredVisionProviders();
  var fallbackProvider = readyProviders[0] || null;
  var providers = visionConfig.providers || [];
  if (count) count.textContent = providers.length.toLocaleString() + ' 个接口';
  if (status) {
    var readyCount = readyProviders.length;
    status.className = 'settings-status ' + (readyCount ? 'ready' : 'warning');
    status.textContent = readyCount ? '已配置 ' + readyCount + ' 个接口' : '尚未配置';
  }
  if (list) {
    if (!providers.length) {
      list.innerHTML = '<div class="vision-provider-empty">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 3 7.5 12 12l9-4.5L12 3z"/><path d="M3 12l9 4.5 9-4.5"/><path d="M3 16.5 12 21l9-4.5"/></svg>'
        + '<strong>尚未添加其他解析接口</strong>'
        + '<span>MinerU 会继续作为默认的免费解析服务；点右上角“添加接口”可接入通义千问等视觉模型</span>'
        + '</div>';
    } else {
      var usageByProvider = {};
      (Array.isArray(parserStatistics.providers) ? parserStatistics.providers : []).forEach(function(item) {
        usageByProvider[item.provider_id] = item;
      });
      var rows = providers.map(function(provider) {
        var badge = visionProviderBadge(provider);
        var usage = usageByProvider[provider.id] || {};
        var usageLabel = Number(usage.parsed_book_count || 0).toLocaleString() + ' 本 · ' + Number(usage.parsed_page_count || 0).toLocaleString() + ' 页';
        return '<tr><td data-label="接口"><span class="mineru-account-identity vision-table-identity">'
          + visionAvatarHtml(provider)
          + '<span class="mineru-account-copy"><strong>' + esc(provider.name) + '</strong><small title="' + esc(provider.api_base) + '">' + esc(provider.model || '未选择模型') + ' · ' + esc(visionHostLabel(provider.api_base)) + '</small></span></span></td>'
          + '<td data-label="状态"><span class="mineru-account-status-cell"><span class="vision-provider-state' + badge.cls + '">' + badge.label + '</span>'
          + '<label class="ui-switch mineru-row-switch" title="' + (provider.enabled ? '停用这个接口' : '启用这个接口') + '">'
          + '<input type="checkbox"' + (provider.enabled ? ' checked' : '') + ' onchange="quickToggleVisionProvider(\'' + provider.id + '\', this.checked)">'
          + '<span class="ui-switch-track" aria-hidden="true"></span><span class="visually-hidden">' + (provider.enabled ? '停用' : '启用') + ' ' + esc(provider.name) + '</span></label></span></td>'
          + '<td data-label="本地解析"><span class="mineru-table-usage">' + usageLabel + '</span></td>'
          + '<td data-label="操作"><span class="mineru-row-actions"><button class="mineru-text-action" type="button" onclick="testVisionProvider(\'' + provider.id + '\')">测试</button><button class="mineru-text-action" type="button" onclick="editVisionProvider(\'' + provider.id + '\')">编辑</button><button class="mineru-text-action danger" type="button" onclick="deleteVisionProvider(\'' + provider.id + '\')">删除</button></span></td></tr>';
      }).join('');
      list.innerHTML = '<div class="mineru-account-table-scroll"><table class="mineru-account-table vision-provider-table"><thead><tr><th>接口</th><th>状态</th><th>本地解析</th><th><span class="visually-hidden">操作</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
    }
  }
  if (autoFallback) {
    autoFallback.checked = !!visionConfig.auto_fallback_from_mineru;
    autoFallback.disabled = !fallbackProvider;
  }
  if (fallbackSummary) {
    if (!fallbackProvider) {
      fallbackSummary.textContent = '请先添加并启用一个解析接口，之后即可开启自动切换';
    } else if (visionConfig.auto_fallback_from_mineru) {
      fallbackSummary.textContent = '已开启；MinerU 失败后将自动改用“' + fallbackProvider.name + '”，可能产生调用费用';
    } else {
      fallbackSummary.textContent = '已关闭；开启后将使用“' + fallbackProvider.name + '”，可能产生调用费用';
    }
  }
  syncImportVisionProviders();
  renderImportQueue();
}

async function loadVisionProviders() {
  var status = document.getElementById('vision-config-status');
  if (status) {
    status.className = 'settings-status';
    status.textContent = '读取中…';
  }
  try {
    var resp = await fetch('/api/vision-providers');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    visionConfig = data;
    visionConfigLoaded = true;
    renderVisionProviders();
  } catch (e) {
    visionConfigLoaded = false;
    if (status) {
      status.className = 'settings-status warning';
      status.textContent = '读取失败';
    }
    syncImportVisionProviders();
    showToast('读取其他解析 API 配置失败：' + e.message);
  }
}

var visionNameAutoValue = '';

function updateVisionEditorHead() {
  var title = document.getElementById('vision-editor-title');
  var avatar = document.getElementById('vision-editor-avatar');
  var cancel = document.getElementById('vision-cancel-edit');
  if (!title || !avatar) return;
  var editing = !!document.getElementById('vision-provider-id').value.trim();
  var name = document.getElementById('vision-provider-name').value.trim();
  var base = document.getElementById('vision-api-base').value.trim();
  title.textContent = editing
    ? '编辑接口' + (name ? ' · ' + name : '')
    : (name ? '添加接口 · ' + name : '添加解析接口');
  if (name || base) {
    var brand = visionBrandFromBase(base);
    avatar.classList.add('has-brand');
    if (brand && brand.icon) {
      avatar.classList.add('has-icon');
      avatar.style.background = brand.iconBg || '';
      avatar.innerHTML = '<img src="/static/brands/' + brand.icon + '" alt="">';
    } else {
      avatar.classList.remove('has-icon');
      var info = visionAvatarFor({name: name, api_base: base});
      avatar.style.background = info.color;
      avatar.textContent = info.letter;
    }
  } else {
    avatar.classList.remove('has-brand', 'has-icon');
    avatar.style.background = '';
    avatar.innerHTML = VISION_PLUS_SVG;
  }
  if (cancel) cancel.hidden = !editing;
}

function autoFillVisionName() {
  var nameInput = document.getElementById('vision-provider-name');
  if (!nameInput) return;
  var current = nameInput.value.trim();
  if (!current || current === visionNameAutoValue) {
    var brand = visionBrandFromBase(document.getElementById('vision-api-base').value.trim());
    var suggested = brand ? brand.name : '';
    nameInput.value = suggested;
    visionNameAutoValue = suggested;
  }
  updateVisionEditorHead();
}

function resetVisionProviderForm() {
  visionModelRequestSerial += 1;
  resetVisionModelButton();
  closeVisionModelPop();
  ['vision-provider-id','vision-provider-name','vision-api-base','vision-model','vision-api-key'].forEach(function(id) {
    var input = document.getElementById(id);
    if (input) input.value = '';
  });
  visionNameAutoValue = '';
  var enabled = document.getElementById('vision-provider-enabled');
  if (enabled) enabled.checked = true;
  var hint = document.getElementById('vision-save-hint');
  if (hint) hint.textContent = '';
  clearVisionModelOptions('填写 API 地址和 Key 后会自动读取模型；接口不支持时仍可手动输入');
  updateVisionEditorHead();
  renderVisionProviders();
}

function startAddVisionProvider() {
  setSettingsSection('vision-api-settings', true);
  resetVisionProviderForm();
  var card = document.getElementById('vision-editor-card');
  if (card) card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  var base = document.getElementById('vision-api-base');
  if (base) base.focus();
}

function editVisionProvider(providerId) {
  var provider = (visionConfig.providers || []).find(function(item) { return item.id === providerId; });
  if (!provider) return;
  document.getElementById('vision-provider-id').value = provider.id;
  document.getElementById('vision-provider-name').value = provider.name || '';
  document.getElementById('vision-api-base').value = provider.api_base || '';
  document.getElementById('vision-model').value = provider.model || '';
  document.getElementById('vision-api-key').value = '';
  document.getElementById('vision-provider-enabled').checked = !!provider.enabled;
  document.getElementById('vision-save-hint').textContent = provider.has_api_key ? '已保存密钥；留空不会覆盖' : '尚未保存 API Key';
  visionNameAutoValue = '';
  visionModelRequestSerial += 1;
  resetVisionModelButton();
  closeVisionModelPop();
  clearVisionModelOptions('正在读取这个接口的模型列表…');
  updateVisionEditorHead();
  renderVisionProviders();
  var card = document.getElementById('vision-editor-card');
  if (card) card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  if (provider.api_base && provider.has_api_key) fetchVisionModels({silent: true});
}

async function quickToggleVisionProvider(providerId, enabled) {
  var provider = (visionConfig.providers || []).find(function(item) { return item.id === providerId; });
  if (!provider) return;
  try {
    var resp = await fetch('/api/vision-providers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        action: 'save_provider',
        provider: {
          id: provider.id,
          name: provider.name,
          api_base: provider.api_base,
          model: provider.model,
          api_key: '',
          enabled: enabled
        }
      })
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '切换失败');
    visionConfig = data;
    visionConfigLoaded = true;
    renderVisionProviders();
    showToast(provider.name + (enabled ? ' 已启用' : ' 已停用'));
  } catch (e) {
    renderVisionProviders();
    showToast('切换失败：' + e.message);
  }
}

async function saveVisionProvider() {
  var hint = document.getElementById('vision-save-hint');
  var provider = {
    id: document.getElementById('vision-provider-id').value.trim(),
    name: document.getElementById('vision-provider-name').value.trim(),
    api_base: document.getElementById('vision-api-base').value.trim(),
    model: document.getElementById('vision-model').value.trim(),
    api_key: document.getElementById('vision-api-key').value.trim(),
    enabled: document.getElementById('vision-provider-enabled').checked
  };
  if (!provider.api_base) {
    showToast('请先填写 API 地址');
    document.getElementById('vision-api-base').focus();
    return;
  }
  if (!provider.id && !provider.api_key && !visionDraftHasUsableKey(provider)) {
    showToast('请填写 API Key');
    document.getElementById('vision-api-key').focus();
    return;
  }
  if (!provider.model) {
    showToast('请填写或选择视觉模型');
    document.getElementById('vision-model').focus();
    return;
  }
  if (!provider.name) {
    var brand = visionBrandFromBase(provider.api_base);
    provider.name = brand ? brand.name : visionHostLabel(provider.api_base) || '自定义接口';
    document.getElementById('vision-provider-name').value = provider.name;
  }
  if (hint) hint.textContent = '正在保存…';
  try {
    var resp = await fetch('/api/vision-providers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'save_provider', provider: provider})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    visionConfig = data;
    visionConfigLoaded = true;
    resetVisionProviderForm();
    renderVisionProviders();
    showToast('其他解析 API 已保存');
  } catch (e) {
    if (hint) hint.textContent = '保存失败';
    showToast('保存解析接口失败：' + e.message);
  }
}

async function deleteVisionProvider(providerId) {
  var provider = (visionConfig.providers || []).find(function(item) { return item.id === providerId; });
  if (!provider || !await showAppConfirm(
    '将删除解析接口“' + provider.name + '”',
    {title:'删除解析接口？', confirmText:'删除', tone:'danger'}
  )) return;
  try {
    var resp = await fetch('/api/vision-providers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'delete_provider', provider_id: providerId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '删除失败');
    visionConfig = data;
    resetVisionProviderForm();
    renderVisionProviders();
    showToast('解析接口已删除');
  } catch (e) {
    showToast('删除解析接口失败：' + e.message);
  }
}

async function testVisionProvider(providerId) {
  var provider = (visionConfig.providers || []).find(function(item) { return item.id === providerId; });
  if (!provider) return;
  if (!provider.configured) {
    showToast('请先保存 API Key、地址和模型名称');
    return;
  }
  if (!await showAppConfirm(
    '测试会向“' + provider.name + '”发送一张极小的测试图片，确认模型确实支持视觉输入',
    {title:'测试视觉接口？', confirmText:'发送测试图片'}
  )) return;
  showToast('正在测试 ' + provider.name + '…');
  try {
    var resp = await fetch('/api/vision-providers/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider_id: providerId})
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '测试失败');
    recordVisionTestResult(provider, true);
    renderVisionProviders();
    showToast(provider.name + ' 视觉连接成功 · ' + data.latency_ms + ' ms');
  } catch (e) {
    recordVisionTestResult(provider, false);
    renderVisionProviders();
    showToast(provider.name + ' 连接失败：' + e.message);
  }
}

async function setVisionAutoFallback(enabled) {
  var toggle = document.getElementById('vision-auto-fallback');
  var providers = configuredVisionProviders();
  var provider = providers[0] || null;
  var previous = !!visionConfig.auto_fallback_from_mineru;
  if (enabled && !provider) {
    if (toggle) toggle.checked = false;
    showToast('请先添加并启用一个其他解析 API');
    return;
  }
  if (toggle) toggle.disabled = true;
  try {
    var resp = await fetch('/api/vision-providers', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        action: 'save_policy',
        auto_fallback_from_mineru: !!enabled
      })
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    visionConfig = data;
    renderVisionProviders();
    showToast(enabled ? '已开启；MinerU 失败后将自动改用 ' + provider.name : '已关闭 MinerU 失败后自动切换');
  } catch (e) {
    if (toggle) {
      toggle.checked = previous;
      toggle.disabled = providers.length === 0;
    }
    showToast('保存自动切换设置失败：' + e.message);
  }
}
