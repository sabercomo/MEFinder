/* ═══ Load index metadata ═══ */
async function loadMeta() {
  try {
    const resp = await fetch('/api/index-meta');
    const meta = await resp.json();
    document.getElementById('index-count').textContent =
      '索引 ' + (meta.eligible_paragraph_count || 0).toLocaleString() + ' 条';
  } catch (e) {
    document.getElementById('index-count').textContent = '索引状态未知';
  }
}

/* ═══ Init ═══ */
document.addEventListener('click', function(event) {
  if (!event.target.closest('.app-select')) closeAppSelects();
});
// 书目字段任一输入即视为有未保存修改（程序化回填另行显式置脏）。
document.addEventListener('input', function(event) {
  if (event.target && event.target.closest && event.target.closest('#bibliographic-editor')) bibEditorDirty = true;
});
(function initVisionEditor() {
  var base = document.getElementById('vision-api-base');
  var key = document.getElementById('vision-api-key');
  var name = document.getElementById('vision-provider-name');
  var model = document.getElementById('vision-model');
  var pop = document.getElementById('vision-model-pop');
  var basePop = document.getElementById('vision-base-pop');
  if (base) {
    base.addEventListener('change', maybeAutoFetchVisionModels);
    base.addEventListener('input', function() {
      autoFillVisionName();
      visionBaseActiveIndex = -1;
      visionBaseShowAll = false;
      openVisionBasePop();
    });
    base.addEventListener('focus', openVisionBasePop);
    base.addEventListener('keydown', visionBaseKeydown);
  }
  if (basePop) basePop.addEventListener('mousedown', function(event) {
    event.preventDefault();
    var item = event.target.closest('.vision-base-item');
    if (item) pickVisionBase(item.getAttribute('data-base'));
  });
  if (key) key.addEventListener('change', maybeAutoFetchVisionModels);
  if (name) name.addEventListener('input', updateVisionEditorHead);
  if (model) {
    model.addEventListener('focus', openVisionModelPop);
    model.addEventListener('input', function() {
      visionModelActiveIndex = -1;
      openVisionModelPop();
    });
    model.addEventListener('keydown', visionModelKeydown);
  }
  if (pop) pop.addEventListener('mousedown', function(event) {
    event.preventDefault();
    var item = event.target.closest('.vision-model-item');
    if (item) pickVisionModel(item.getAttribute('data-model'));
  });
  document.addEventListener('click', function(event) {
    var combo = event.target.closest('.vision-model-combo');
    if (!combo || !combo.querySelector('#vision-model')) closeVisionModelPop();
    if (!combo || !combo.querySelector('#vision-api-base')) closeVisionBasePop();
  });
})();
configureDesktopPlatformOptions();
setupScanDirectoryControls();
setupLibraryDragSelection();
setupScanResultDragSelection();
renderScanDirectories();
loadMeta();
loadPreferences();
loadResumableImports();
syncLibraryViewButtons();
// 文献库只在用户展开文献下拉或进入文献库页时才读取：启动时不预取整库。
renderSearchDocumentOptions();
updateSearchDocumentLabel();
initDropZone();
const requestedInitialPage = new URLSearchParams(window.location.search).get('page');
const initialPage = requestedInitialPage === 'calibration' ? 'library' : requestedInitialPage;
if (['search','library','import','settings'].indexOf(initialPage) >= 0) navigateTo(initialPage);
if (currentPage === 'search') document.getElementById('query').focus();
