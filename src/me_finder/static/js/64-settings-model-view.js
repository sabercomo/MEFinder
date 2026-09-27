/* component: 译本对齐模型行（Vue 3 试点）
   genre: 设置页组件卡片——单选 + 下载/删除 + 进度
   pre-emit critique: 状态文案与原命令式渲染逐字一致；只换渲染方式，不换视觉；
     两行模型的静态信息（名称、体积）在这里声明一次，不再在模板里写两遍。
   检查编号: contrast/slop/chrome/tokens/responsive 沿用 30-settings.css 既有类名，未新增样式。

   状态归属（Vue 试点的验收点）：
   - 唯一写入方是 64-settings-model.js（请求、代次、轮询都在那里），只经下面的
     setComponent / setSelection / setPending / setLoadError 四个入口写 store。
   - 本文件只读 store：模型行由 Vue 组件渲染，标题栏状态由 watchEffect 跟随。
     以前每次状态变化要手动改 5 个元素 × 2 行 + 标题栏，现在只写一次 store。
   - rowView / summaryView 是纯函数，没有 Vue 也能在 node 里测。 */
(function (global) {  // module: 64-settings-model-view.js
  var MODELS = [
    {id: 'minilm-l12-v2', name: 'MiniLM 多语言模型', badge: '默认',
      detail: '384 维 · 约 220 MB · 速度更快', progressLabel: 'MiniLM 模型下载进度'},
    {id: 'multilingual-e5-large', name: 'E5 Large 多语言模型', badge: '',
      detail: '1024 维 · 约 2.24 GB · 更准但更慢', progressLabel: 'E5 Large 模型下载进度'}
  ];
  var IDLE_HINT = '首次使用前需下载，文件只保存在本机';

  var Vue = global.Vue || null;
  var reactive = Vue ? Vue.reactive : function (value) { return value; };
  var store = reactive({
    component: null,
    selectedId: 'minilm-l12-v2',
    saving: false,
    loadError: false,
    pending: {}
  });

  function modelById(id) {
    var models = (store.component && store.component.models) || [];
    return models.find(function (model) { return model.id === id; }) || null;
  }

  function hiddenProgress(width) {
    return {visible: false, indeterminate: false, width: width, valueNow: null, valueText: null};
  }

  // 单个模型行的全部可见状态；文案与 0.5.7 命令式渲染逐字一致。
  function rowView(id) {
    var model = modelById(id);
    var pending = store.pending[id] || '';
    var view;
    if (!model) {
      view = {tone: '', stateText: '读取中…', hint: IDLE_HINT, title: '',
        progress: hiddenProgress('0%'),
        button: {label: '下载安装', disabled: false, danger: false, action: 'download'}};
    } else if (model.state === 'downloading') {
      var transfer = alignmentModelDownloadProgress(model);
      view = {tone: '', stateText: transfer ? '下载中 ' + transfer.percent + '%' : '下载中',
        hint: transfer ? transfer.text : (model.message || '正在下载模型…'),
        progress: {visible: true, indeterminate: !transfer,
          width: transfer ? (transfer.ratio * 100) + '%' : '0%',
          valueNow: transfer ? transfer.percent : null, valueText: transfer ? transfer.text : null},
        button: {label: '正在下载…', disabled: true, danger: false, action: null}};
    } else if (model.state === 'verifying') {
      // 字节已齐、安装回执未落：若仍显示"正在下载 99%"，用户分不清"快好了"与"卡死"
      // （2026-09-13 E5 卡 99% 反馈）。
      view = {tone: '', stateText: '校验中', hint: '模型文件已就绪，正在校验并写入安装记录…',
        progress: {visible: true, indeterminate: false, width: '99%', valueNow: 99, valueText: '校验中'},
        button: {label: '校验中…', disabled: true, danger: false, action: null}};
    } else if (model.installed) {
      view = {tone: 'ready', stateText: '已下载', hint: '模型保存在 MEFinder 组件目录，可离线使用',
        progress: hiddenProgress('100%'),
        button: {label: '删除模型', disabled: false, danger: true, action: 'delete'}};
    } else {
      var failed = model.state === 'failed';
      view = {tone: failed ? 'warning' : '', stateText: failed ? '下载失败' : '未下载',
        hint: model.error ? '上次下载失败：' + model.error : IDLE_HINT,
        progress: hiddenProgress('0%'),
        button: {label: failed ? '重试下载' : '下载安装', disabled: false, danger: false, action: 'download'}};
    }
    if (model) view.title = model.error || '';
    if (pending === 'starting') view.button = {label: '正在启动…', disabled: true, danger: false, action: null};
    if (pending === 'deleting') view.button = {label: '删除中…', disabled: true, danger: view.button.danger, action: null};
    return view;
  }

  // 标题栏：当前选中模型的一句话状态；数据未到时保持「读取中…」。
  function summaryView() {
    if (store.loadError) return {tone: 'warning', text: '读取失败'};
    var models = (store.component && store.component.models) || [];
    var selected = modelById(store.selectedId);
    if (!selected) return {tone: '', text: '读取中…'};
    var transfer = alignmentModelDownloadProgress(selected);
    var installedCount = models.filter(function (model) { return model.installed; }).length;
    var tone = selected.installed ? 'ready' : (selected.state === 'failed' ? 'warning' : '');
    if (selected.state === 'downloading') return {tone: tone, text: '下载中' + (transfer ? ' ' + transfer.percent + '%' : '')};
    if (selected.state === 'verifying') return {tone: tone, text: '校验中'};
    if (selected.installed) return {tone: tone, text: '当前模型已下载'};
    return {tone: tone, text: '当前模型未下载 · 已下载 ' + installedCount + ' / ' + models.length};
  }

  // —— 写入入口：只由 64-settings-model.js 调用 ——
  function setComponent(component) {
    store.component = component;
    store.loadError = false;
    store.pending = {};
  }
  function setSelection(modelId, saving) {
    store.selectedId = modelId;
    store.saving = !!saving;
  }
  function setPending(modelId, phase) {
    var next = Object.assign({}, store.pending);
    if (phase) next[modelId] = phase; else delete next[modelId];
    store.pending = next;
  }
  function setLoadError() { store.loadError = true; }

  var ROW_TEMPLATE = [
    '<section v-for="model in models" :key="model.id"',
    '    class="embedding-model-option local-ocr-component"',
    '    :class="{selected: model.id === store.selectedId}" :data-embedding-model-choice="model.id">',
    '  <div class="local-ocr-component-head">',
    '    <label class="embedding-model-choice">',
    '      <input type="radio" name="alignment-embedding-model" :value="model.id"',
    '        :checked="model.id === store.selectedId" :disabled="store.saving"',
    '        @change="pick(model.id)">',
    '      <span class="embedding-model-check" aria-hidden="true"></span>',
    '      <span class="embedding-model-copy"><strong>{{ model.name }}<template v-if="model.badge"> <em>{{ model.badge }}</em></template></strong><small>{{ model.detail }}</small></span>',
    '    </label>',
    '    <span :id="\'embedding-model-state-\' + model.id" class="settings-status"',
    '      :class="row(model.id).tone">{{ row(model.id).stateText }}</span>',
    '  </div>',
    '  <div class="local-ocr-install-row">',
    '    <div class="local-ocr-install-copy">',
    '      <span :id="\'embedding-model-hint-\' + model.id" role="status" aria-live="polite">{{ row(model.id).hint }}</span>',
    '      <div :id="\'embedding-model-progress-\' + model.id" class="local-ocr-progress" role="progressbar"',
    '        :aria-label="model.progressLabel" :hidden="!row(model.id).progress.visible"',
    '        :class="{indeterminate: row(model.id).progress.indeterminate}"',
    '        aria-valuemin="0" aria-valuemax="100"',
    '        :aria-valuenow="row(model.id).progress.valueNow" :aria-valuetext="row(model.id).progress.valueText">',
    '        <span :style="{width: row(model.id).progress.width}"></span></div>',
    '    </div>',
    '    <div class="settings-actions local-ocr-install-actions">',
    '      <button class="action-btn" :id="\'embedding-model-download-\' + model.id" type="button"',
    '        :class="{danger: row(model.id).button.danger}" :title="row(model.id).title"',
    '        :disabled="row(model.id).button.disabled" @click="act(model.id)">{{ row(model.id).button.label }}</button>',
    '    </div>',
    '  </div>',
    '</section>'
  ].join('\n');

  function mount() {
    if (!Vue || typeof document === 'undefined') return;
    var rows = document.getElementById('embedding-model-options');
    if (rows) {
      Vue.createApp({
        template: ROW_TEMPLATE,
        setup: function () {
          return {
            store: store,
            models: MODELS,
            row: rowView,
            act: function (id) {
              var action = rowView(id).button.action;
              if (action === 'download') global.downloadAlignmentModel(id);
              else if (action === 'delete') global.deleteAlignmentModel(id);
            },
            pick: function (id) {
              global.setAlignmentEmbeddingModel(id);
              // 保存中或偏好未就绪时切换被拒：store 不变，Vue 不会回写单选框，而浏览器已把
              // 同组的原选中项取消了。这里按 store 把整组单选框对齐一遍。
              Vue.nextTick(function () {
                rows.querySelectorAll('input[name="alignment-embedding-model"]').forEach(function (input) {
                  input.checked = input.value === store.selectedId;
                });
              });
            }
          };
        }
      }).mount(rows);
    }
    var status = document.getElementById('alignment-model-status');
    if (status) {
      Vue.watchEffect(function () {
        var summary = summaryView();
        status.className = 'settings-status' + (summary.tone ? ' ' + summary.tone : '');
        status.textContent = summary.text;
      });
    }
  }

  global.MEFinderAlignmentModelView = Object.freeze({
    rowView: rowView,
    summaryView: summaryView,
    setComponent: setComponent,
    setSelection: setSelection,
    setPending: setPending,
    setLoadError: setLoadError
  });
  mount();
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
