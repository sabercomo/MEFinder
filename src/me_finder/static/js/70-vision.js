/* ═══ MinerU API settings ═══ */
async function loadMineruConfig() {
  var status = document.getElementById('mineru-config-status');
  if (!status) return;
  status.className = 'settings-status';
  status.textContent = '读取中…';
  try {
    var resp = await fetch('/api/mineru-config');
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '读取失败');
    document.getElementById('mineru-api-base').value = data.api_base || 'https://mineru.net';
    document.getElementById('mineru-expires-at').value = data.expires_at || '';
    document.getElementById('mineru-token').value = '';
    if (data.configured) {
      var expiryStatus = data.expiry_status || 'ok';
      var variant = (expiryStatus === 'expired' || expiryStatus === 'invalid') ? 'warning'
        : (expiryStatus === 'expires_today' || expiryStatus === 'unset') ? 'warning' : 'ready';
      status.className = 'settings-status ' + variant;
      status.textContent = '已配置' + (data.expiry_label ? ' · ' + data.expiry_label : '');
    } else {
      status.className = 'settings-status warning';
      status.textContent = data.has_legacy_access_keys
        ? '旧 AK/SK 无法鉴权，请填写 API Token'
        : '尚未配置 API Token';
    }
    mineruConfigLoaded = true;
  } catch (e) {
    status.className = 'settings-status warning';
    status.textContent = '读取失败';
    showToast('读取 MinerU 配置失败：' + e.message);
  }
}

async function exportBackup() {
  var hint = document.getElementById('backup-export-hint');
  try {
    if (hint) hint.textContent = '正在导出…';
    var resp = await fetch('/api/backup/export', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导出失败');
    if (hint) hint.textContent = '已导出到：' + data.path;
    showToast('备份已导出（' + formatFileSize(data.size_bytes) + '）');
  } catch (e) {
    if (hint) hint.textContent = '生成一个包含页码映射、书目信息和偏好的小体积 zip';
    showToast('导出备份失败：' + e.message);
  }
}

async function importBackup() {
  var input = document.getElementById('backup-import-path');
  var path = (input.value || '').trim();
  if (!path) { showToast('请先填写备份文件路径'); return; }
  if (!await showAppConfirm(
    '导入将覆盖当前的页码映射与书目信息，并重建索引',
    {title:'导入并覆盖当前数据？', confirmText:'确认导入', tone:'danger'}
  )) return;
  try {
    var resp = await fetch('/api/backup/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})});
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
    showToast('已恢复备份，正在重建索引…');
    pollBackupRestore(data.job_id);
  } catch (e) {
    showToast('导入备份失败：' + e.message);
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

async function saveMineruConfig() {
  var hint = document.getElementById('mineru-save-hint');
  var payload = {
    token: document.getElementById('mineru-token').value.trim(),
    api_base: document.getElementById('mineru-api-base').value.trim(),
    expires_at: document.getElementById('mineru-expires-at').value
  };
  hint.textContent = '正在保存…';
  try {
    var resp = await fetch('/api/mineru-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '保存失败');
    if (!data.configured) {
      hint.textContent = '尚未填写有效 Token';
      showToast('请粘贴 MinerU API 管理页面创建的 Token');
      mineruConfigLoaded = false;
      await loadMineruConfig();
      return;
    }
    hint.textContent = '已保存到本机';
    showToast('MinerU API 配置已保存');
    mineruConfigLoaded = false;
    await loadMineruConfig();
  } catch (e) {
    hint.textContent = '保存失败';
    showToast('保存 MinerU 配置失败：' + e.message);
  }
}

async function testMineruConnection() {
  var hint = document.getElementById('mineru-save-hint');
  var btn = document.getElementById('mineru-test-btn');
  var token = document.getElementById('mineru-token').value.trim();
  if (token) {
    showToast('测试使用已保存的 Token，请先点“保存 API 配置”再测试');
    return;
  }
  if (btn) btn.disabled = true;
  if (hint) hint.textContent = '正在测试连接…';
  showToast('正在测试 MinerU 连接…');
  try {
    var resp = await fetch('/api/mineru-config/test', {method: 'POST'});
    var data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || '测试失败');
    if (hint) hint.textContent = '连接正常 · ' + data.latency_ms + ' ms';
    showToast('MinerU 连接成功 · ' + data.latency_ms + ' ms');
  } catch (e) {
    if (hint) hint.textContent = '连接失败';
    showToast('MinerU 连接失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ═══ Optional OpenAI-compatible vision providers ═══ */
var VISION_BRAND_RULES = [
  {re: /deepseek/i, name: '深度求索 DeepSeek', color: '#4D6BFE', icon: 'deepseek-color.svg', base: 'https://api.deepseek.com'},
  {re: /dashscope|aliyuncs/i, name: '通义千问', color: '#615CED', icon: 'qwen-color.svg', base: 'https://dashscope.aliyuncs.com/compatible-mode/v1'},
  {re: /moonshot/i, name: '月之暗面 Kimi', color: '#1E1F24', icon: 'kimi-color.svg', iconBg: '#101319', base: 'https://api.moonshot.cn/v1'},
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
      || String(item.owned_by || '').toLowerCase().indexOf(query) >= 0;
  });
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
  var owners = [];
  var byOwner = {};
  items.forEach(function(item) {
    var owner = String(item.owned_by || '其他');
    if (!byOwner[owner]) { byOwner[owner] = []; owners.push(owner); }
    byOwner[owner].push(item);
  });
  var html = owners.map(function(owner) {
    return '<div class="vision-model-group">' + esc(owner) + '</div>'
      + byOwner[owner].map(function(item) {
          var index = visionModelFlat.length;
          visionModelFlat.push(item);
          return '<div class="vision-model-item' + (index === visionModelActiveIndex ? ' active' : '')
            + '" data-model="' + esc(item.id) + '">'
            + '<span class="vision-model-id">' + esc(item.id) + '</span>'
            + (item.likely_vision ? '<span class="vision-model-badge">可能支持图片</span>' : '')
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
      '已获取 ' + visionModelOptions.length + ' 个模型。点击输入框选择；“可能支持图片”仅为名称提示',
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

function renderVisionProviders() {
  var list = document.getElementById('vision-provider-list');
  var status = document.getElementById('vision-config-status');
  var autoFallback = document.getElementById('vision-auto-fallback');
  var fallbackSummary = document.getElementById('vision-fallback-summary');
  var readyProviders = configuredVisionProviders();
  var fallbackProvider = readyProviders[0] || null;
  if (status) {
    var readyCount = readyProviders.length;
    status.className = 'settings-status ' + (readyCount ? 'ready' : 'warning');
    status.textContent = readyCount ? '已配置 ' + readyCount + ' 个接口' : '尚未配置';
  }
  if (list) {
    if (!(visionConfig.providers || []).length) {
      list.innerHTML = '<div class="vision-provider-empty">'
        + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 3 7.5 12 12l9-4.5L12 3z"/><path d="M3 12l9 4.5 9-4.5"/><path d="M3 16.5 12 21l9-4.5"/></svg>'
        + '<strong>尚未添加其他解析接口</strong>'
        + '<span>MinerU 会继续作为默认的免费解析服务；点右上角“添加接口”可接入通义千问、DeepSeek 等视觉模型</span>'
        + '</div>';
    } else {
      var editingId = (document.getElementById('vision-provider-id') || {}).value || '';
      list.innerHTML = visionConfig.providers.map(function(provider) {
        var state = provider.configured && provider.enabled ? '可用' : provider.enabled ? '缺少密钥' : '已停用';
        var stateClass = provider.configured && provider.enabled ? '' : provider.enabled ? ' warning' : ' muted';
        return '<div class="vision-provider-card' + (editingId === provider.id ? ' selected' : '')
          + '" role="button" tabindex="0" title="点击编辑这个接口"'
          + ' onclick="editVisionProvider(\'' + provider.id + '\')"'
          + ' onkeydown="if(event.key===\'Enter\')editVisionProvider(\'' + provider.id + '\')">'
          + visionAvatarHtml(provider)
          + '<div class="vision-provider-card-main">'
          + '<div class="vision-provider-card-name">' + esc(provider.name)
          + '<span class="vision-provider-state' + stateClass + '">' + state + '</span></div>'
          + '<div class="vision-provider-card-model" title="' + esc(provider.api_base) + '">' + esc(provider.model || '未选择模型') + ' · ' + esc(visionHostLabel(provider.api_base)) + '</div>'
          + '</div>'
          + '<div class="vision-provider-card-actions" onclick="event.stopPropagation()" onkeydown="event.stopPropagation()">'
          + '<label class="ui-switch" title="' + (provider.enabled ? '停用这个接口' : '启用这个接口') + '">'
          + '<input type="checkbox"' + (provider.enabled ? ' checked' : '') + ' onchange="quickToggleVisionProvider(\'' + provider.id + '\', this.checked)">'
          + '<span class="ui-switch-track" aria-hidden="true"></span></label>'
          + '<button class="icon-btn" type="button" title="发送测试图片，验证连通" aria-label="测试连接" onclick="testVisionProvider(\'' + provider.id + '\')">' + VISION_BOLT_SVG + '</button>'
          + '<button class="icon-btn danger" type="button" title="删除接口" aria-label="删除接口" onclick="deleteVisionProvider(\'' + provider.id + '\')">' + VISION_TRASH_SVG + '</button>'
          + '</div></div>';
      }).join('');
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
    showToast(provider.name + ' 视觉连接成功 · ' + data.latency_ms + ' ms');
  } catch (e) {
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
