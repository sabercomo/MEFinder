/* Optional alignment backend settings; reuses the component card controls. */
var MEFinderBertalignSettings = (function () {
  'use strict';
  var timer = null;
  var loading = false;
  var revision = 0;
  async function request(url, payload) {
    var options = {cache: 'no-store'};
    if (payload) { options.method = 'POST'; options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(payload); }
    var response = await fetch(url, options);
    var data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作失败');
    return data;
  }
  function showBackend(backend) {
    document.getElementById('default-alignment-settings').hidden = backend === 'bertalign';
    document.getElementById('default-alignment-status').hidden = backend === 'bertalign';
  }
  function render(data) {
    var busy = !!data.operation;
    document.getElementById('bertalign-state').textContent = busy ? '处理中…' : (data.installed ? '已安装' : '未安装');
    document.getElementById('bertalign-hint').textContent = data.error || (!data.supported ? '当前平台的组件清单不可用，请检查应用更新' : data.message) || (data.has_models ? '已验证本地 LaBSE，可离线生成对齐' : '安装时联网下载，文献内容保留在本机');
    var action = document.getElementById('bertalign-action');
    action.disabled = busy || !data.supported;
    action.textContent = data.installed ? (data.update_available ? '更新' : '验证组件') : '安装组件及模型';
    action.onclick = function () { manage(data.installed ? (data.update_available ? 'update' : 'validate') : 'install'); };
    var remove = document.getElementById('bertalign-uninstall');
    remove.hidden = !data.installed || busy;
    remove.onclick = function () { manage('uninstall'); };
    var cancel = document.getElementById('bertalign-cancel');
    cancel.hidden = !busy;
    cancel.onclick = function () { manage('cancel'); };
    clearTimeout(timer);
    if (busy) timer = setTimeout(load, 1000);
  }
  async function load() {
    if (loading) return;
    loading = true;
    var readRevision = ++revision;
    try {
      var results = await Promise.all([request('/api/text-alignment/runtime?backend=bertalign'), request('/api/preferences')]);
      if (readRevision !== revision) return;
      document.getElementById('alignment-backend').value = results[1].alignment_backend || 'default';
      showBackend(results[1].alignment_backend);
      render(results[0]);
    } catch (error) {
      if (readRevision !== revision) return;
      document.getElementById('bertalign-state').textContent = '读取失败';
      document.getElementById('bertalign-hint').textContent = error.message;
      var retry = document.getElementById('bertalign-action');
      retry.disabled = false; retry.textContent = '重新读取'; retry.onclick = load;
    } finally { loading = false; }
  }
  async function manage(action) {
    if (action === 'uninstall' && !await showAppConfirm(
      '将删除 Bertalign 运行时和 LaBSE 模型。文献、已有对齐结果与人工校正都会保留。',
      {title: '卸载 Bertalign', confirmText: '卸载', tone: 'danger'})) return;
    revision += 1;
    try { render(await request('/api/text-alignment/runtime', {backend: 'bertalign', action: action})); }
    catch (error) { document.getElementById('bertalign-hint').textContent = error.message; }
  }
  async function select(backend) {
    revision += 1;
    var input = document.getElementById('alignment-backend');
    input.disabled = true;
    try {
      await request('/api/preferences', {alignment_backend: backend});
      showBackend(backend);
      localStorage.setItem('mefinder-alignment-backend', backend);
      window.dispatchEvent(new Event('library_changed'));
      document.getElementById('bertalign-hint').textContent = '已切换算法，译本对照和阅读器将读取对应结果';
    } catch (error) { document.getElementById('bertalign-hint').textContent = error.message; await load(); }
    finally { input.disabled = false; }
  }
  return {load: load, select: select};
}());
