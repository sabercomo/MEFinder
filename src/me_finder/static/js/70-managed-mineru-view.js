/* component: 托管 MinerU 运行时卡片（Vue 3 试点第二块）
   genre: 设置页组件卡片——硬件说明 + 两套配置（Pipeline / VLM）× 安装/启动/停止/卸载/取消 + 进度
   pre-emit critique: 状态与文案逐条沿用 0.5.7 的 renderManagedMineru；类名、结构不变，
     两套配置的静态信息只声明一次，不再在模板里写两遍 10 个按钮。
   检查编号: contrast/slop/chrome/tokens/responsive 沿用 30-settings.css 既有类名，未新增样式。

   状态归属：
   - 唯一写入方是 70-vision.js（请求、代次、轮询），只经 setRuntime / setNotice /
     setChecking / setPending 写 store。
   - 本文件只读 store；cardView / profileView 是纯函数，没有 Vue 也能在 node 里测。 */
(function (global) {  // module: 70-managed-mineru-view.js
  var PROFILES = [
    {id: 'pipeline', name: 'Pipeline', note: '兼容性优先 · 支持纯 CPU'},
    {id: 'vlm', name: 'VLM 高精度', note: '需要 Apple Silicon 或兼容 NVIDIA GPU（8GB+ 显存）'}
  ];
  var BUSY_STATES = ['provisioning', 'downloading_models', 'validating', 'starting', 'cleaning'];
  var BUSY_LABELS = {
    provisioning: '安装依赖中', downloading_models: '下载模型中', validating: '验证中',
    starting: '启动中', cleaning: '清理中'
  };

  var Vue = global.Vue || null;
  var reactive = Vue ? Vue.reactive : function (value) { return value; };
  var store = reactive({
    runtime: null,       // 未读到前为 null：保持首屏的「正在检测硬件…」
    localConfig: {},
    notice: '',          // 请求失败等临时提示，覆盖常规提示；下一次 setRuntime 清除
    checking: false,     // 「检查新版本」进行中
    pending: {}          // profile:action -> true，请求进行中禁用对应按钮
  });

  function managedMineruTransferSummary(profile) {
    if (!profile.total_bytes) return '';
    var total = (profile.total_is_estimate ? '约 ' : '') + localOCRByteSize(profile.total_bytes);
    var summary = '已下载 ' + localOCRByteSize(profile.downloaded_bytes) + ' / ' + total;
    if (profile.downloaded_bytes >= profile.total_bytes) return summary + ' · 即将完成';
    if (!profile.download_speed_bps || profile.eta_seconds == null) {
      return summary + (profile.downloaded_bytes ? ' · 网络波动或正在处理分片…' : ' · 正在检测网速…');
    }
    return summary + ' · ' + localOCRByteSize(profile.download_speed_bps) + '/s · ' + localOCREstimatedWait(profile.eta_seconds);
  }

  function managedMineruErrorText(value) {
    var message = String(value || '').replace(/\s+/g, ' ').trim();
    if (/pypi\.org\/simple\/mineru/i.test(message) && /(failed to fetch|tunnel error|connect)/i.test(message)) {
      return '无法连接 PyPI，请检查网络或代理后重试';
    }
    if (/(huggingface_hub|hf_hub_download|xet_get|aws\.cdn\.hf\.co)/i.test(message) && /(connectionerror|network error|request middleware error|timeout|connect|readerror|i\/o error|decoding response body)/i.test(message)) {
      return '模型下载网络中断，请检查网络或代理后重试';
    }
    return message.length > 180 ? message.slice(0, 177) + '…' : message;
  }

  function managedMineruVersionText(runtime) {
    // 如实说明安装目标是从 PyPI 取到的还是清单固定版本，别让用户以为一定是最新
    var target = String(runtime.version || '').trim();
    if (!target) return '';
    var detail = String(runtime.version_detail || '').trim();
    var source = String(runtime.version_source || '') === 'pypi' ? '兼容区间内最新' : '清单固定版本';
    return '安装目标 MinerU ' + target + '（' + source + '）' + (detail ? ' · ' + detail : '');
  }

  function externalConfigured() {
    return !!store.localConfig.enabled && !store.localConfig.managed;
  }

  function button(visible, label, extra) {
    return Object.assign({visible: visible, label: label, disabled: false}, extra || {});
  }

  // 单套配置的全部可见状态。runtime 里没有这套配置时，保持首屏模板的默认呈现。
  function profileView(profileId) {
    var runtime = store.runtime || {};
    var profile = (runtime.profiles || []).find(function (item) { return item.profile === profileId; });
    var pending = function (action) { return !!store.pending[profileId + ':' + action]; };
    if (!profile) {
      return {busy: false, stateTone: 'warning', stateText: '未安装',
        progressHint: {visible: false, text: ''},
        progress: {visible: false, width: '', indeterminate: false},
        install: button(true, '下载安装', {action: 'install', disabled: pending('install')}),
        start: button(false, '启动'), stop: button(false, '停止'),
        uninstall: button(false, '卸载'), cancel: button(false, '取消')};
    }
    var hardware = runtime.hardware || {};
    var service = runtime.service || {};
    var external = externalConfigured();
    var busy = BUSY_STATES.indexOf(profile.state) >= 0;
    var running = !!service.running && service.profile === profileId;
    var detail = running && service.endpoint
      ? '运行于 ' + service.endpoint
      : profile.error ? '安装失败：' + managedMineruErrorText(profile.error) : (profile.message || '');
    var transfer = busy ? managedMineruTransferSummary(profile) : '';
    if (transfer) detail += (detail ? ' · ' : '') + transfer;
    var installAction = profile.update_available ? 'update' : 'install';
    return {
      busy: busy,
      stateTone: profile.installed ? 'ready' : 'warning',
      stateText: BUSY_LABELS[profile.state]
        || (running ? '运行中' : profile.error ? '安装失败' : profile.update_available ? '可更新'
          : profile.installed ? '已安装'
          : profile.supported ? (external ? '未由 MEFinder 安装' : '未安装') : '平台不支持'),
      progressHint: {visible: !!detail, text: detail},
      progress: {visible: busy,
        width: profile.progress == null ? '18%' : Math.round(profile.progress * 100) + '%',
        indeterminate: busy && profile.progress == null},
      install: button(!((profile.installed && !profile.update_available) || busy),
        profile.update_available ? '更新组件' : external ? '改用托管安装' : '下载安装',
        {action: installAction, disabled: !profile.supported
          || (profileId === 'vlm' && !hardware.vlm_supported) || pending(installAction)}),
      start: button(!(!profile.installed || busy || running), '启动', {disabled: pending('start')}),
      stop: button(!(!running || busy), '停止', {disabled: pending('stop')}),
      uninstall: button(!(!profile.installed || busy || running), '卸载', {disabled: pending('uninstall')}),
      cancel: button(busy, '取消', {disabled: pending('cancel')})
    };
  }

  // 卡片级：硬件说明、VLM 是否展示、推荐安装/检查更新按钮、底部提示。
  function cardView() {
    if (!store.runtime) {
      return {loaded: false, active: false, hardware: '正在检测硬件…', vlmHidden: false,
        auto: {label: '安装推荐配置', disabled: !!store.pending['auto:install']},
        check: {label: store.checking ? '检查中…' : '检查新版本', disabled: store.checking},
        hint: store.notice};
    }
    var runtime = store.runtime;
    var hardware = runtime.hardware || {};
    var service = runtime.service || {};
    var profiles = runtime.profiles || [];
    var external = externalConfigured();
    var memory = hardware.vram_mb ? ' · ' + (hardware.vram_mb / 1024).toFixed(0) + 'GB 显存' : '';
    var vlmProfile = profiles.find(function (item) { return item.profile === 'vlm'; });
    var active = profiles.some(function (item) { return BUSY_STATES.indexOf(item.state) >= 0; });
    var failed = profiles.some(function (item) { return !!item.error; });
    var recommended = profiles.find(function (item) {
      return item.profile === (hardware.recommended_profile || 'pipeline');
    });
    var hint = failed ? '安装失败，未改动现有本地部署设置。' : (service.running
      ? ''
      : active ? '安装需要约 20GB 可用空间，请保持应用开启'
      : external ? '已配置自部署服务 ' + store.localConfig.endpoint + '；无需重复下载。下方托管运行时为可选方案'
      : '组件按需下载，不会随主程序更新自动安装');
    var versionLine = failed ? '' : managedMineruVersionText(runtime);
    if (versionLine) hint = (hint ? hint + ' · ' : '') + versionLine;
    return {
      loaded: true,
      active: active,
      hardware: hardware.detection_error
        ? hardware.detection_error + ' · 默认推荐 Pipeline'
        : hardware.name
        ? '当前设备：' + hardware.name + memory + ' · 推荐 ' + (hardware.recommended_profile === 'vlm' ? 'VLM' : 'Pipeline')
        : '未检测到可用的本地推理硬件',
      vlmHidden: !hardware.vlm_supported && !(vlmProfile && vlmProfile.installed),
      auto: {
        label: recommended && recommended.installed ? '已安装' : external ? '改用推荐托管配置' : '安装推荐配置',
        disabled: active || !runtime.supported || !!(recommended && recommended.installed)
          || !!store.pending['auto:install']
      },
      check: {label: store.checking ? '检查中…' : '检查新版本',
        disabled: store.checking || active || !runtime.supported},
      hint: store.notice || hint
    };
  }

  // —— 写入入口：只由 70-vision.js 调用 ——
  function setRuntime(runtime, localConfig) {
    store.runtime = runtime || {};
    store.localConfig = localConfig || {};
    store.notice = '';
    store.checking = false;
  }
  function setNotice(text) { store.notice = text || ''; }
  function setChecking(checking) { store.checking = !!checking; }
  function setPending(profileId, action, on) {
    var next = Object.assign({}, store.pending);
    if (on) next[profileId + ':' + action] = true; else delete next[profileId + ':' + action];
    store.pending = next;
  }

  var ACTION_BUTTONS = [
    {key: 'install', cls: 'action-btn primary'},
    {key: 'start', cls: 'action-btn'},
    {key: 'stop', cls: 'action-btn'},
    {key: 'uninstall', cls: 'action-btn danger'},
    {key: 'cancel', cls: 'action-btn'}
  ];

  var CARD_TEMPLATE = [
    '<div class="managed-mineru-heading"><span><strong id="managed-mineru-title">MEFinder 托管运行时</strong>',
    '  <small id="managed-mineru-hardware">{{ card().hardware }}</small></span>',
    '  <span class="settings-actions">',
    '    <button id="managed-mineru-check-updates" class="action-btn" type="button"',
    '      :disabled="card().check.disabled" @click="check()">{{ card().check.label }}</button>',
    '    <button id="managed-mineru-auto-install" class="action-btn primary" type="button"',
    '      :disabled="card().auto.disabled" @click="run(\'auto\', \'install\')">{{ card().auto.label }}</button>',
    '  </span></div>',
    '<div class="managed-mineru-profile-list">',
    '  <article v-for="profile in profiles" :key="profile.id" class="managed-mineru-profile"',
    '      :data-mineru-profile="profile.id" :hidden="hiddenProfile(profile.id)">',
    '    <div><strong>{{ profile.name }}</strong><small>{{ profile.note }}</small>',
    '      <small :id="\'managed-mineru-\' + profile.id + \'-progress\'" class="managed-mineru-profile-progress"',
    '        role="status" aria-live="polite" :hidden="!view(profile.id).progressHint.visible">{{ view(profile.id).progressHint.text }}</small>',
    '      <div :id="\'managed-mineru-\' + profile.id + \'-progress-bar\'" class="managed-mineru-progress"',
    '        :class="{indeterminate: view(profile.id).progress.indeterminate}" :hidden="!view(profile.id).progress.visible">',
    '        <span :style="{width: view(profile.id).progress.width}"></span></div></div>',
    '    <span :id="\'managed-mineru-\' + profile.id + \'-state\'" class="settings-status"',
    '      :class="view(profile.id).stateTone">{{ view(profile.id).stateText }}</span>',
    '    <div class="settings-actions">',
    '      <button v-for="action in actions" :key="action.key" :class="action.cls" type="button"',
    '        :id="\'managed-mineru-\' + profile.id + \'-\' + action.key"',
    '        :hidden="!view(profile.id)[action.key].visible" :disabled="view(profile.id)[action.key].disabled"',
    '        @click="run(profile.id, view(profile.id)[action.key].action || action.key)">{{ view(profile.id)[action.key].label }}</button>',
    '    </div>',
    '  </article>',
    '</div>',
    '<small id="managed-mineru-hint" class="settings-hint" role="status" aria-live="polite">{{ card().hint }}</small>'
  ].join('\n');

  function mount() {
    if (!Vue || typeof document === 'undefined') return;
    var container = document.getElementById('managed-mineru');
    if (!container) return;
    Vue.createApp({
      template: CARD_TEMPLATE,
      setup: function () {
        return {
          profiles: PROFILES,
          actions: ACTION_BUTTONS,
          card: cardView,
          view: profileView,
          hiddenProfile: function (profileId) { return profileId === 'vlm' && cardView().vlmHidden; },
          run: function (profileId, action) { global.manageMineruComponent(profileId, action); },
          check: function () { global.checkManagedMineruUpdates(); }
        };
      }
    }).mount(container);
  }

  global.MEFinderManagedMineruView = Object.freeze({
    cardView: cardView,
    profileView: profileView,
    transferSummary: managedMineruTransferSummary,
    errorText: managedMineruErrorText,
    setRuntime: setRuntime,
    setNotice: setNotice,
    setChecking: setChecking,
    setPending: setPending
  });
  mount();
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
