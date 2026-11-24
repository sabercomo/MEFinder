/* PaddleOCR settings: same visual controls as MinerU, separate requests and state. */
(function (global) { // module: 70-paddleocr.js
  const section = document.getElementById('paddleocr-settings');
  const option = document.getElementById('paddleocr-parse-option');
  if (!section || !option) return;

  const field = name => document.getElementById('paddleocr-' + name);
  const endpoint = field('endpoint');
  const enabled = field('enabled');
  const hint = field('hint');
  const connectionHint = field('connection-hint');
  const requests = global.MEFinderTaskState.createLatest();
  const labels = {
    provisioning: '安装依赖中', downloading_models: '下载模型中',
    installed: '已安装', not_installed: '未安装', failed: '安装失败',
  };
  let timer;
  let draft = false;
  let busy = false;
  let available = false;

  function syncImportOption() {
    const selected = settingsStore.currentPdfParseMode === 'paddleocr-local';
    option.hidden = !available && !selected;
    option.querySelector('input').disabled = !available;
    field('parse-note').textContent = available ? '使用已安装的 CPU 组件或已启用的内网服务解析 PDF'
      : '请先在设置中启用 PaddleOCR，或手动选择其他解析方式';
  }

  function render(data) {
    busy = data.busy;
    available = data.enabled;
    field('status').textContent = data.enabled ? '已启用' : '未启用';
    field('status').classList.toggle('ready', !!data.enabled);
    field('platform').textContent = data.supported ? '推荐 PP-StructureV3 · 支持纯 CPU' : '本机不支持托管安装，可连接自部署服务';
    field('state').textContent = data.supported ? labels[data.state] : '平台不支持';
    field('state').classList.toggle('ready', !!data.installed);
    field('state').classList.toggle('warning', !data.installed);
    field('progress').hidden = !data.busy;
    field('cancel').hidden = !data.busy;
    field('install').hidden = !!(data.busy || data.installed);
    field('install').disabled = !data.supported;
    field('recommended').disabled = !data.supported || data.busy || data.installed;
    field('uninstall').hidden = !data.installed || data.busy;
    hint.textContent = data.message || '组件按需下载，安装后仅在手动选择时使用';
    if (!draft) {
      endpoint.value = data.endpoint;
      enabled.checked = !!data.enabled;
    }
    syncImportOption();
  }

  async function request(payload) {
    const token = requests.begin();
    clearTimeout(timer);
    try {
      const response = await global.MEFinderApi.fetch('/api/paddleocr-local', payload ? {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
      } : {});
      const result = await response.json();
      if (!requests.isCurrent(token)) return;
      if (!response.ok || result.error) throw new Error(result.error || '读取 PaddleOCR 状态失败');
      if (payload?.action === 'test') {
        connectionHint.textContent = '连接成功，服务返回了有效的版面解析结果。';
        if (busy) timer = setTimeout(load, 900);
        return;
      }
      if (payload?.action === 'save') {
        draft = false;
        connectionHint.textContent = '已保存设置。';
      }
      render(result);
      if (payload?.action === 'check' && !result.busy) hint.textContent = '此版本支持 PaddleOCR ' + result.version;
      if (result.busy) timer = setTimeout(load, 900);
    } catch (error) {
      if (!requests.isCurrent(token)) return;
      (['save', 'test'].includes(payload?.action) ? connectionHint : hint).textContent = error.message;
      if (!payload) field('status').textContent = '读取失败';
      if (busy) timer = setTimeout(load, 900);
    }
  }

  function load() { return request(); }
  for (const [button, action] of Object.entries({
    'install': 'install', 'recommended': 'install', 'check': 'check',
    'cancel': 'cancel', 'uninstall': 'uninstall',
  })) {
    field(button).addEventListener('click', async () => {
      if (action === 'install' && !await showAppConfirm(
        '将下载 CPU 运行环境和识别模型。建议预留约 5 GB 磁盘空间。',
        {title: '安装 PaddleOCR', confirmText: '下载安装'}
      )) return;
      if (action === 'uninstall' && !await showAppConfirm(
        '将移除 CPU 组件和下载缓存，已导入的文献不受影响。',
        {title: '卸载 PaddleOCR', confirmText: '卸载', danger: true, focusCancel: true}
      )) return;
      request({action});
    });
  }
  section.addEventListener('input', () => {
    draft = true;
    connectionHint.textContent = '有未保存的修改';
  });
  field('save').addEventListener('click', () => request({action: 'save', endpoint: endpoint.value, enabled: enabled.checked}));
  field('test').addEventListener('click', () => {
    connectionHint.textContent = '正在检测连接…';
    request({action: 'test', endpoint: endpoint.value});
  });
  global.MEFinder.paddleOCR = Object.freeze({load, syncImportOption});
}(window));
