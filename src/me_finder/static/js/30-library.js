/* ═══ Library ═══ */
function applyLibraryCatalog(data) {
  libSources = data.items || [];
  libVolumes = data.volumes || [];
  libVolumeBySource = buildVolumeIndex(libVolumes);
  libFolders = data.folders || [];
  libDocumentGroups = data.document_groups || [];
  if (libScopeType === 'folder' && !libFolders.some(function(item) { return item.folder_id === libScopeId; })) {
    libScopeType = 'all';
    libScopeId = '';
  }
  if (libScopeType === 'document_group' && !libDocumentGroups.some(function(item) { return item.document_group_id === libScopeId; })) {
    libScopeType = 'all';
    libScopeId = '';
  }
  libStats = data.stats || null;
  libLoaded = true;
  // 搜索下拉与文献库共用同一份摘要，避免两处各拉一次。
  searchSourceFiles = libSources;
  searchVolumes = libVolumes;
  searchFolders = libFolders;
  searchDocumentGroups = libDocumentGroups;
  searchDocumentsLoaded = true;
}

async function loadLibrary(force) {
  try {
    applyLibraryCatalog(await fetchLibraryCatalog(force));
    renderLibraryScopes();
    renderLibraryStats();
    syncLibraryViewButtons();
    syncLibrarySortControls();
    renderLibraryList();
    applyLibraryRailState();
  } catch(e) {
    document.getElementById('library-list').innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">' + esc(e.message || '文献库加载失败') + '</div></div>';
  }
}

function scopedLibrarySources() {
  return libSources.filter(function(source) {
    return libraryScopeMatches(source, libScopeType, libScopeId);
  });
}

function persistLibraryScope() {
  try {
    localStorage.setItem('meFinderLibraryScopeV1', JSON.stringify({type:libScopeType, id:libScopeId}));
  } catch (_) {}
}

async function setLibraryScope(scopeType, scopeId) {
  if (!await guardLeaveDetail()) return;
  libScopeType = ['root','folder','document_group'].indexOf(scopeType) >= 0 ? scopeType : 'all';
  libScopeId = libScopeType === 'folder' || libScopeType === 'document_group' ? String(scopeId || '') : '';
  persistLibraryScope();
  closeLibDrawer();
  clearLibrarySelection();
  renderLibraryScopes();
  renderLibraryStats();
  renderLibraryList();
}

// 左栏折叠（方案 A）：常驻但可收起，状态记在本地，切页/重载后保持。
function applyLibraryRailState() {
  var body = document.querySelector('#page-library .library-body');
  var btn = document.getElementById('library-fold-btn');
  // 默认折叠：没存过偏好时收起左栏；用户手动展开后记住其选择（存 '0'）。
  var raw = null;
  try { raw = localStorage.getItem('meFinderLibraryRailCollapsed'); } catch (_) {}
  var collapsed = raw === null ? true : raw === '1';
  if (body) body.classList.toggle('rail-collapsed', collapsed);
  if (btn) {
    btn.classList.toggle('is-collapsed', collapsed);
    btn.setAttribute('aria-pressed', collapsed ? 'true' : 'false');
    btn.setAttribute('aria-label', collapsed ? '展开文件夹栏' : '折叠文件夹栏');
  }
}

function toggleLibraryRail() {
  var body = document.querySelector('#page-library .library-body');
  if (!body) return;
  body.classList.add('rail-animated');  // 仅用户手动切换才播过渡；首帧不播
  var collapsed = !body.classList.contains('rail-collapsed');
  try { localStorage.setItem('meFinderLibraryRailCollapsed', collapsed ? '1' : '0'); } catch (_) {}
  applyLibraryRailState();
}

function libraryScopeCount(type, id) {
  return libSources.filter(function(source) {
    return libraryScopeMatches(source, type, id);
  }).length;
}

function libraryScopeButton(type, id, label, count, editable) {
  var active = libScopeType === type && String(libScopeId || '') === String(id || '');
  var idText = esc(String(id || ''));
  var actions = editable
    ? '<span class="library-scope-actions"><button type="button" title="重命名" onclick="openLibraryOrganizationModal(\'' + (type === 'folder' ? 'rename_folder' : 'rename_group') + '\',\'' + idText + '\')">•••</button><button type="button" title="删除" onclick="' + (type === 'folder' ? 'deleteLibraryFolder' : 'deleteLibraryDocumentGroup') + '(\'' + idText + '\')">×</button></span>'
    : '';
  return '<div class="library-scope-item' + (active ? ' is-active' : '') + '"><button class="library-scope-target" type="button" onclick="setLibraryScope(\'' + type + '\',\'' + idText + '\')"><span class="library-scope-name">' + esc(label) + '</span><span class="library-scope-count">' + count + '</span></button>' + actions + '</div>';
}

function renderLibraryScopes() {
  var container = document.getElementById('library-scopes');
  if (!container) return;
  var folders = libFolders.slice().sort(function(a, b) { return calPinyinCollator.compare(a.name || '', b.name || ''); });
  var groups = libDocumentGroups.slice().sort(function(a, b) { return calPinyinCollator.compare(a.title || '', b.title || ''); });
  container.innerHTML = '<div class="library-scope-section">'
    + '<div class="library-scope-heading">浏览</div>'
    + libraryScopeButton('all', '', '全部文件', libSources.length, false)
    + libraryScopeButton('root', '', '根目录', libraryScopeCount('root', ''), false)
    + '</div><div class="library-scope-section"><div class="library-scope-heading"><span>文件夹</span><button type="button" title="新建文件夹" onclick="openLibraryOrganizationModal(\'create_folder\')">＋</button></div>'
    + folders.map(function(folder) { return libraryScopeButton('folder', folder.folder_id, folder.name, libraryScopeCount('folder', folder.folder_id), true); }).join('')
    + (folders.length ? '' : '<button type="button" class="library-scope-new" onclick="openLibraryOrganizationModal(\'create_folder\')"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>新建文件夹</button>')
    + '</div><div class="library-scope-section"><div class="library-scope-heading"><span>作品组</span><button type="button" title="创建作品组" onclick="openLibraryOrganizationModal(\'create_group\')">＋</button></div>'
    + groups.map(function(group) { return libraryScopeButton('document_group', group.document_group_id, group.title, libraryScopeCount('document_group', group.document_group_id), true); }).join('')
    + (groups.length ? '' : '<button type="button" class="library-scope-new" onclick="openLibraryOrganizationModal(\'create_group\')"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M10 4v12M4 10h12"/></svg>新建作品组</button>') + '</div>';
}

function openLibraryOrganizationModal(action, targetId) {
  var modal = document.getElementById('library-organization-modal');
  var title = document.getElementById('library-organization-title');
  var input = document.getElementById('library-organization-input');
  var select = document.getElementById('library-organization-select');
  var hint = document.getElementById('library-organization-hint');
  if (!modal || !title || !input || !select || !hint) return;
  var selectedIds = Array.from(libDeleteSelection);
  if ((action === 'move_sources' || action === 'assign_group') && !selectedIds.length) return;
  libraryOrganizationAction = {action:action, targetId:String(targetId || ''), sourceIds:selectedIds};
  input.hidden = true;
  select.hidden = true;
  hint.textContent = '';
  if (action === 'create_folder') {
    title.textContent = '新建文件夹';
    input.value = '';
    input.placeholder = '文件夹名称';
    input.hidden = false;
  } else if (action === 'rename_folder') {
    var folder = libFolders.find(function(item) { return item.folder_id === targetId; });
    title.textContent = '重命名文件夹';
    input.value = folder ? folder.name : '';
    input.hidden = false;
  } else if (action === 'create_group') {
    title.textContent = '创建作品组';
    input.value = '';
    input.placeholder = '作品标题';
    input.hidden = false;
  } else if (action === 'rename_group') {
    var group = libDocumentGroups.find(function(item) { return item.document_group_id === targetId; });
    title.textContent = '重命名作品组';
    input.value = group ? group.title : '';
    input.hidden = false;
  } else if (action === 'move_sources') {
    title.textContent = '移动 ' + selectedIds.length + ' 份文献';
    select.innerHTML = '<option value="">根目录</option>' + libFolders.map(function(item) {
      return '<option value="' + esc(item.folder_id) + '">' + esc(item.name) + '</option>';
    }).join('');
    select.hidden = false;
    hint.textContent = '只改变文件所在位置，不影响索引、页码或作品组。';
  } else if (action === 'assign_group') {
    title.textContent = '设置 ' + selectedIds.length + ' 份文献的作品组';
    select.innerHTML = '<option value="">不属于任何作品组</option>' + libDocumentGroups.map(function(item) {
      return '<option value="' + esc(item.document_group_id) + '">' + esc(item.title) + '</option>';
    }).join('');
    select.hidden = false;
    hint.textContent = '同一作品组的不同版本仍可位于不同文件夹。';
  } else if (action === 'version_label') {
    var source = libSources.find(function(item) { return item.source_file_id === targetId; });
    if (!source || !source.document_group_id) return;
    title.textContent = '编辑版本名称';
    input.value = String((source.version_metadata && source.version_metadata.version_label) || '');
    input.placeholder = documentGroupMemberDisplayName(Object.assign({}, source, {version_metadata:{}}));
    input.hidden = false;
    hint.textContent = '可留空；留空时将按译者、语言、版次和出版年份自动生成。';
  } else {
    return;
  }
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  setTimeout(function() { (input.hidden ? select : input).focus(); }, 0);
}

function closeLibraryOrganizationModal() {
  var modal = document.getElementById('library-organization-modal');
  if (modal) {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }
  libraryOrganizationAction = null;
}

function libraryOrganizationBackdropClick(event) {
  if (event.target && event.target.id === 'library-organization-modal') closeLibraryOrganizationModal();
}

async function requestLibraryOrganization(path, payload, successMessage) {
  var response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  var data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || '资料组织操作失败');
  closeLibraryOrganizationModal();
  clearLibrarySelection();
  await loadLibrary(true);
  showToast(successMessage, 'success');
  return data.result || {};
}

async function submitLibraryOrganizationModal() {
  if (!libraryOrganizationAction) return;
  var action = libraryOrganizationAction;
  var input = document.getElementById('library-organization-input');
  var select = document.getElementById('library-organization-select');
  try {
    if (action.action === 'create_folder') {
      await requestLibraryOrganization('/api/folders/create', {name:input.value}, '文件夹已创建');
    } else if (action.action === 'rename_folder') {
      await requestLibraryOrganization('/api/folders/rename', {folder_id:action.targetId, name:input.value}, '文件夹已重命名');
    } else if (action.action === 'create_group') {
      await requestLibraryOrganization('/api/document-groups/create', {title:input.value}, '作品组已创建');
    } else if (action.action === 'rename_group') {
      await requestLibraryOrganization('/api/document-groups/rename', {document_group_id:action.targetId, title:input.value}, '作品组已重命名');
    } else if (action.action === 'move_sources') {
      await requestLibraryOrganization('/api/documents/move', {source_file_ids:action.sourceIds, folder_id:select.value || null}, '文献位置已更新');
    } else if (action.action === 'assign_group') {
      await requestLibraryOrganization('/api/document-groups/assign', {source_file_ids:action.sourceIds, document_group_id:select.value || null}, '作品组成员已更新');
    } else if (action.action === 'version_label') {
      await requestLibraryOrganization('/api/document-groups/version-label', {source_file_id:action.targetId, version_label:input.value}, '版本名称已更新');
    }
  } catch (error) {
    showToast(error.message || '资料组织操作失败', 'danger');
  }
}

async function deleteLibraryFolder(folderId) {
  var folder = libFolders.find(function(item) { return item.folder_id === folderId; });
  if (!folder || !await showAppConfirm(
    '删除文件夹“' + folder.name + '”后，其中的文献会移回根目录；不会删除实际文献。',
    {title:'删除文件夹？', tone:'warning', confirmText:'删除文件夹'}
  )) return;
  try {
    if (libScopeType === 'folder' && libScopeId === folderId) {
      libScopeType = 'root';
      libScopeId = '';
      persistLibraryScope();
    }
    await requestLibraryOrganization('/api/folders/delete', {folder_id:folderId}, '文件夹已删除，文献已移回根目录');
  } catch (error) {
    showToast(error.message || '删除文件夹失败', 'danger');
  }
}

async function deleteLibraryDocumentGroup(groupId) {
  var group = libDocumentGroups.find(function(item) { return item.document_group_id === groupId; });
  if (!group || !await showAppConfirm(
    '删除作品组“' + group.title + '”只会解除版本关系，不会删除其中的实际文献。',
    {title:'删除作品组？', tone:'warning', confirmText:'删除作品组'}
  )) return;
  try {
    if (libScopeType === 'document_group' && libScopeId === groupId) {
      libScopeType = 'all';
      libScopeId = '';
      persistLibraryScope();
    }
    await requestLibraryOrganization('/api/document-groups/delete', {document_group_id:groupId}, '作品组已删除，文献仍保留');
  } catch (error) {
    showToast(error.message || '删除作品组失败', 'danger');
  }
}

// 映射区间、识别证据、PDF 剖面和收录作品只在详情抽屉里用，按 source_id 单份读取。
function ensureLibraryDetail(sourceId) {
  if (!sourceId || libDetailLoaded[sourceId]) return Promise.resolve();
  if (libDetailPending[sourceId]) return libDetailPending[sourceId];
  var request = fetch('/api/library/document?source_id=' + encodeURIComponent(sourceId)).then(function(response) {
    return response.json().then(function(data) {
      if (!response.ok || data.error) throw new Error(data.error || '文献详情读取失败');
      applyLibraryDetail(sourceId, data);
    });
  }).then(function() {
    delete libDetailPending[sourceId];
  }, function(error) {
    delete libDetailPending[sourceId];
    throw error;
  });
  libDetailPending[sourceId] = request;
  return request;
}

function applyLibraryDetail(sourceId, data) {
  var detail = data.item || {};
  // libSources 与 searchSourceFiles 指向同一个数组，就地替换让两处同时拿到完整记录。
  var index = libSources.findIndex(function(item) { return item.source_file_id === sourceId; });
  if (index >= 0) libSources[index] = Object.assign({}, libSources[index], detail);
  if (data.volume && data.volume.source_file_id) {
    var volumeIndex = libVolumes.findIndex(function(item) { return item.source_file_id === sourceId; });
    if (volumeIndex >= 0) libVolumes[volumeIndex] = data.volume;
    else libVolumes.push(data.volume);
    libVolumeBySource.set(sourceId, data.volume);
  }
  var volumeId = data.volume ? data.volume.volume_id : null;
  if (volumeId) {
    libWorks = libWorks.filter(function(work) { return work.volume_id !== volumeId; }).concat(data.works || []);
  }
  libDetailLoaded[sourceId] = true;
}

function renderLibraryStats() {
  var container = document.getElementById('library-stats');
  if (!container) return;
  var current = {total:0,calibrated:0,page_pending:0,bibliographic:0};
  scopedLibrarySources().forEach(function(item) {
    if (item.source_type !== 'pdf') return;
    current.total += 1;
    var group = calibrationStatusGroup(item.status);
    if (group === 'calibrated') current.calibrated += 1;
    else current.page_pending += 1;
    if (bibliographicMissingFields(sourceBibliographicMetadata(item)).length > 0) current.bibliographic += 1;
  });
  // W1：拆成「待处理」行动组（重）+「参考量」组（轻），一眼看出现在该处理什么。
  container.innerHTML = '<div class="stat-group stat-group--pending"><span class="stat-group__label">待处理</span>'
    + statusStatButton('page_pending','页码待处理',current.page_pending,'warning','notice',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('bibliographic','书目待补全',current.bibliographic,'neutral','book',libStatusFilter,'applyLibStatusFilter')
    + '</div><span class="library-controls-spacer"></span>'
    + '<div class="stat-group stat-group--reference">'
    + statusStatButton('pdf_all','PDF 总数',current.total,'info','document',libStatusFilter,'applyLibStatusFilter')
    + statusStatButton('calibrated','已校准',current.calibrated,'success','check',libStatusFilter,'applyLibStatusFilter')
    + '</div>';
  // 计数为 0 的统计降噪：0 是「无事」，不该用告警色抢注意力。
  container.querySelectorAll('.status-stat').forEach(function(btn) {
    var count = btn.querySelector('.status-stat__count');
    btn.classList.toggle('is-zero', !!count && count.textContent.trim() === '0');
  });
}

// 主流范式（Notion / Linear / Zotero）：三个筛选收进一个「筛选」按钮 + 弹层分面；
// 只有正在生效的筛选才作为可删 chip 露出来，按钮带数字角标。渲染由 renderLibraryList 触发。
function libDocTypeLabel(v) {
  return v === 'book' ? '著作' : v === 'journal_article' ? '期刊论文'
    : v === 'thesis' ? '学位论文' : v === 'unknown' ? '未识别' : '全部类型';
}

// 当前生效的（非「全部」）筛选，按「类型 → 语言 → 文件」次序，供角标与 chips 使用。
function libFilterActiveList() {
  var out = [];
  if (libDocTypeFilter !== 'all') out.push({kind:'doctype', label:libDocTypeLabel(libDocTypeFilter)});
  if (libLangFilter !== 'all') out.push({kind:'lang', label:libLangChipLabel(libLangFilter)});
  if (libTypeFilter !== 'all') out.push({kind:'type', label:libTypeFilter === 'word' ? 'Word' : 'PDF'});
  return out;
}

function renderLibraryFilterBar() {
  var scopeSources = scopedLibrarySources();
  var allCount = scopeSources.length;
  var wordCount = scopeSources.filter(function(s){ return s.source_type === 'word'; }).length;
  var pdfCount = scopeSources.filter(function(s){ return s.source_type === 'pdf'; }).length;
  var journalCount = scopeSources.filter(function(s){ return libraryDocType(s) === 'journal_article'; }).length;
  var thesisCount = scopeSources.filter(function(s){ return libraryDocType(s) === 'thesis'; }).length;
  // 著作正向计数：已确认类型的图书 PDF；未识别单列一档（L-15）。
  var bookCount = scopeSources.filter(function(s){ return s.source_type === 'pdf' && isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)) && libraryDocType(s) === 'book'; }).length;
  var unknownCount = scopeSources.filter(function(s){ return s.source_type === 'pdf' && !isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)); }).length;

  var doctypeOpts = [
    {v:'all', label:'全部类型', n:allCount},
    {v:'book', label:'著作', n:bookCount},
    {v:'journal_article', label:'期刊论文', n:journalCount},
    {v:'thesis', label:'学位论文', n:thesisCount}
  ];
  if (unknownCount > 0) doctypeOpts.push({v:'unknown', label:'未识别', n:unknownCount});

  // 语言细类只显示库内实际存在的语言；设置项仍只控制中文/外文两大类的先后。
  var languageOptions = libraryLanguageFacetOptions(scopeSources, libDefaultLanguage);
  if (libLangFilter !== 'all' && !languageOptions.some(function(option) { return option.v === libLangFilter; })) libLangFilter = 'all';
  var langOpts = [{v:'all', label:'全部语言', n:allCount}].concat(languageOptions);

  var typeOpts = [
    {v:'all', label:'全部', n:allCount},
    {v:'word', label:'Word', n:wordCount},
    {v:'pdf', label:'PDF', n:pdfCount}
  ];

  renderLibraryFacet('filter-opts-doctype', doctypeOpts, libDocTypeFilter, 'doctype');
  renderLibraryFacet('filter-opts-lang', langOpts, libLangFilter, 'lang');
  renderLibraryFacet('filter-opts-type', typeOpts, libTypeFilter, 'type');

  var active = libFilterActiveList();
  var badge = document.getElementById('library-filter-badge');
  if (badge) { badge.textContent = String(active.length); badge.hidden = active.length === 0; }
  var container = document.getElementById('library-filter');
  if (container) container.classList.toggle('has-active', active.length > 0);
  var chips = document.getElementById('library-filter-chips');
  if (chips) {
    chips.innerHTML = active.map(function(a){
      return '<button class="library-filter-chip" type="button" title="移除筛选：' + esc(a.label) + '" aria-label="移除筛选：' + esc(a.label) + '" onclick="removeLibFacet(event,\'' + a.kind + '\')">'
        + '<span>' + esc(a.label) + '</span>'
        + '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15"/></svg></button>';
    }).join('');
  }
}

function renderLibraryFacet(containerId, options, active, kind) {
  var el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = options.map(function(o){
    return '<button class="filter-opt' + (o.v === active ? ' is-on' : '') + '" type="button" role="option" aria-selected="' + (o.v === active) + '" data-value="' + o.v + '" onclick="setLibFacet(event,\'' + kind + '\',\'' + o.v + '\')">'
      + '<span>' + esc(o.label) + '</span><span class="filter-opt-n">' + o.n + '</span></button>';
  }).join('');
}

// 选中某个分面：立即生效并重绘（弹层保持打开，可连续多选，Notion 式）。
async function setLibFacet(event, kind, value) {
  if (event) event.stopPropagation();
  if (!await guardLeaveDetail()) return;
  if (kind === 'doctype') libDocTypeFilter = value;
  else if (kind === 'lang') libLangFilter = value;
  else if (kind === 'type') {
    libTypeFilter = value;
    if (libTypeFilter === 'word' && libStatusFilter !== 'all') { libStatusFilter = 'all'; renderLibraryStats(); }
  }
  closeLibDrawer();
  renderLibraryList();
}

// 移除单个生效筛选（点 chip 的 ✕），把该分面复位到「全部」。
async function removeLibFacet(event, kind) {
  if (event) event.stopPropagation();
  if (!await guardLeaveDetail()) return;
  if (kind === 'doctype') libDocTypeFilter = 'all';
  else if (kind === 'lang') libLangFilter = 'all';
  else if (kind === 'type') libTypeFilter = 'all';
  closeLibDrawer();
  renderLibraryList();
}

// 切换排序方向（升/降），合并排序控件里的方向按钮。
function toggleLibrarySortDirection() {
  libSortDirection = libSortDirection === 'asc' ? 'desc' : 'asc';
  try { localStorage.setItem('meFinderLibrarySortDirection', libSortDirection); } catch (_) {}
  syncLibrarySortControls();
  closeAppSelects();
  renderLibraryList();
}

async function applyLibStatusFilter(status) {
  if (!await guardLeaveDetail()) return;
  var requested = status || 'all';
  libStatusFilter = requested === libStatusFilter ? 'all' : requested;
  if (libStatusFilter !== 'all' && libTypeFilter === 'word') {
    libTypeFilter = 'all';  // 筛选按钮/chips 由 renderLibraryFilterBar 随列表重绘刷新
  }
  closeLibDrawer();
  renderLibraryStats();
  renderLibraryList();
}

function loadLibDefaultLanguage() {
  var raw = null;
  try { raw = localStorage.getItem('meFinderLibDefaultLanguage'); } catch (_) {}
  return raw === 'foreign' ? 'foreign' : 'chinese';
}

function setLibDefaultLanguage(btn) {
  var value = btn && btn.dataset ? btn.dataset.deflang : btn;
  value = value === 'foreign' ? 'foreign' : 'chinese';
  if (value === libDefaultLanguage) return;
  libDefaultLanguage = value;
  try { localStorage.setItem('meFinderLibDefaultLanguage', value); } catch (_) {}
  persistDisplayPreference('lib_default_language', value);  // 随数据备份/迁移（C-01）
  syncLibDefaultLanguageControl();
  renderLibraryList();  // 重绘以刷新语言筛选条的标签与「本国/外文」归属
}

function syncLibDefaultLanguageControl() {
  document.querySelectorAll('#lib-default-lang-control .seg-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.deflang === libDefaultLanguage);
  });
}

// 清空所有筛选与搜索，回到全部（空态「清除全部筛选」、筛选弹层「清除全部」用）。
function clearLibraryFilters() {
  libTypeFilter = 'all';
  libLangFilter = 'all';
  libDocTypeFilter = 'all';
  libStatusFilter = 'all';
  var search = document.getElementById('lib-search');
  if (search) search.value = '';
  renderLibraryStats();
  renderLibraryList();
}

function filterLibrary() {
  // 输入法与连续输入下，91 份以上的列表每敲一个字重排一次会明显发涩。
  if (libFilterTimer) clearTimeout(libFilterTimer);
  libFilterTimer = setTimeout(function() {
    libFilterTimer = null;
    renderLibraryList();
  }, 160);
}

function setLibraryView(mode) {
  libViewMode = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem('meFinderLibraryView', libViewMode);
  persistDisplayPreference('library_view', libViewMode);
  syncLibraryViewButtons();
  renderLibraryList();
}

function syncLibraryViewButtons() {
  ['list','grid'].forEach(function(mode) {
    var button = document.getElementById('library-view-' + mode);
    if (!button) return;
    var active = libViewMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setLibrarySortOption(event, control, value) {
  event.stopPropagation();
  if (control === 'direction') {
    libSortDirection = value === 'asc' ? 'asc' : 'desc';
    localStorage.setItem('meFinderLibrarySortDirection', libSortDirection);
  } else {
    libSortField = ['imported_at','title','author','modified_at','source_type','status'].indexOf(value) >= 0 ? value : 'imported_at';
    localStorage.setItem('meFinderLibrarySortField', libSortField);
  }
  syncLibrarySortControls();
  closeAppSelects();
  renderLibraryList();
}

function syncLibrarySortControls() {
  var labels = {imported_at:'导入时间',title:'书名',author:'作者',modified_at:'最近修改时间',source_type:'来源类型',status:'校准状态',desc:'降序',asc:'升序'};
  var fieldLabel = document.getElementById('library-sort-field-label');
  if (fieldLabel) fieldLabel.textContent = labels[libSortField] || labels.imported_at;
  document.querySelectorAll('#library-sort-field-select .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === libSortField);
  });
  // 方向合并成一个可点按钮：文案随升/降切换，箭头方向靠 .is-asc 翻转。
  var dirBtn = document.getElementById('library-sort-dir');
  var dirLabel = document.getElementById('library-sort-dir-label');
  if (dirLabel) dirLabel.textContent = labels[libSortDirection] || labels.desc;
  if (dirBtn) {
    dirBtn.classList.toggle('is-asc', libSortDirection === 'asc');
    dirBtn.setAttribute('aria-label', libSortDirection === 'asc' ? '升序，点击改为降序' : '降序，点击改为升序');
  }
}

function compareLibraryDates(a, b) {
  var av = Date.parse(a || '') || 0;
  var bv = Date.parse(b || '') || 0;
  if (!av && !bv) return 0;
  if (!av) return 1;
  if (!bv) return -1;
  return libSortDirection === 'desc' ? bv - av : av - bv;
}

function getFilteredSources() {
  let sources = scopedLibrarySources();
  if (libTypeFilter !== 'all') {
    sources = sources.filter(s => s.source_type === libTypeFilter);
  }
  if (libLangFilter !== 'all') {
    sources = sources.filter(s => libraryLanguageCode(s) === libLangFilter);
  }
  if (libDocTypeFilter === 'unknown') {
    // 未识别：从未跑过书目识别的 PDF（类型只是默认回落成 book，并非真的判定过）。
    sources = sources.filter(s => s.source_type === 'pdf' && !isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)));
  } else if (libDocTypeFilter === 'book') {
    // 著作：仅已确认类型的图书 PDF；不再把 Word 文集和未识别 PDF 混进来。
    sources = sources.filter(s => s.source_type === 'pdf' && isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)) && libraryDocType(s) === 'book');
  } else if (libDocTypeFilter !== 'all') {
    sources = sources.filter(s => libraryDocType(s) === libDocTypeFilter);
  }
  if (libStatusFilter === 'pdf_all') {
    sources = sources.filter(s => s.source_type === 'pdf');
  } else if (libStatusFilter === 'page_pending') {
    sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) !== 'calibrated');
  } else if (libStatusFilter === 'bibliographic') {
    sources = sources.filter(s => s.source_type === 'pdf' && bibliographicMissingFields(sourceBibliographicMetadata(s)).length > 0);
  } else if (libStatusFilter !== 'all') {
    sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) === libStatusFilter);
  }
  const q = (document.getElementById('lib-search').value || '').trim().toLowerCase().replace(/\s+/g, '');
  if (q) {
    sources = sources.filter(s => {
      var bib = sourceBibliographicMetadata(s);
      const haystack = [s.title, s.author, s.translator, s.publisher, s.file_name,
        documentGroupMemberDisplayName(s), bib.language_code, bib.edition, bib.publish_year]
        .map(function(value) { return String(value || '').toLowerCase().replace(/\s+/g, ''); })
        .join('|');
      return haystack.indexOf(q) >= 0;
    });
  }
  sources.sort(function(a, b) {
    var left = librarySortProjection(a);
    var right = librarySortProjection(b);
    var result;
    if (libSortField === 'imported_at' || libSortField === 'modified_at') {
      result = compareLibraryDates(left[libSortField], right[libSortField]);
    } else if (libSortField === 'status') {
      var order = {manual_mapped:0,auto_mapped_high:1,unmapped:2,needs_review:3,auto_mapping_failed:4,source_missing:5,mapping:6};
      var av = a.source_type === 'pdf' && order[a.status] != null ? order[a.status] : 99;
      var bv = b.source_type === 'pdf' && order[b.status] != null ? order[b.status] : 99;
      result = libSortDirection === 'desc' ? bv - av : av - bv;
    } else {
      result = calibrationSortText(left[libSortField], right[libSortField], libSortDirection);
    }
    return result || calibrationSortText(left.title, right.title, 'asc');
  });
  return sources;
}


function updateLibraryDeleteControls() {
  var bar = document.getElementById('library-selection-bar');
  var page = document.getElementById('page-library');
  var count = document.getElementById('library-selection-count');
  var removeButton = document.getElementById('library-remove-selected-btn');
  var exportButton = document.getElementById('library-export-selected-btn');
  var selectVisibleButton = document.getElementById('library-select-visible-btn');
  var selectedCount = libDeleteSelection.size;
  var selectedPdfCount = libSources.filter(function(item) {
    return item.source_type === 'pdf' && libDeleteSelection.has(item.source_file_id);
  }).length;
  var active = selectedCount > 0;
  // Selection alone drives the contextual action bar: no persistent mode toggle.
  if (page) page.classList.toggle('library-selecting', active);
  if (bar) bar.hidden = !active;
  if (count) count.textContent = '已选 ' + selectedCount + ' 项';
  if (removeButton) removeButton.disabled = selectedCount === 0;
  if (exportButton) {
    exportButton.disabled = libraryExportRunning || selectedPdfCount === 0;
    if (!libraryExportRunning) {
      exportButton.textContent = selectedPdfCount
        ? '导出所选 PDF（' + selectedPdfCount + '）'
        : '导出所选 PDF';
    }
  }
  if (selectVisibleButton) {
    var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
    var allSelected = selectable.length > 0 && selectable.every(function(item) {
      return libDeleteSelection.has(item.source_file_id);
    });
    selectVisibleButton.textContent = allSelected ? '取消全选' : '全选当前';
    selectVisibleButton.disabled = selectable.length === 0;
  }
  syncLibrarySelectAll();
}

function syncLibraryDeleteSelectionUI() {
  document.querySelectorAll('#library-list .library-entry').forEach(function(entry) {
    var selected = libDeleteSelection.has(entry.dataset.id);
    entry.classList.toggle('delete-selected', selected);
    entry.setAttribute('aria-selected', selected ? 'true' : 'false');
    var input = entry.querySelector('.library-delete-check');
    if (input) input.checked = selected;
  });
  updateLibraryDeleteControls();
}

function clearLibrarySelection() {
  if (libDeleteSelection.size === 0) return;
  libDeleteSelection.clear();
  syncLibraryDeleteSelectionUI();
}

function toggleLibraryDeleteSelection(sourceId, force) {
  var source = libSources.find(function(item) { return item.source_file_id === sourceId; });
  if (!isLibraryDeleteSelectable(source)) {
    showToast('当前来源类型暂不支持从文献库移除', 'warning');
    return;
  }
  var selected = typeof force === 'boolean' ? force : !libDeleteSelection.has(sourceId);
  if (selected) libDeleteSelection.add(sourceId);
  else libDeleteSelection.delete(sourceId);
  syncLibraryDeleteSelectionUI();
}

function toggleSelectVisibleLibraryDocuments() {
  var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
  var allSelected = selectable.length > 0 && selectable.every(function(item) {
    return libDeleteSelection.has(item.source_file_id);
  });
  selectable.forEach(function(item) {
    if (allSelected) libDeleteSelection.delete(item.source_file_id);
    else libDeleteSelection.add(item.source_file_id);
  });
  syncLibraryDeleteSelectionUI();
}

// 列表键盘导航（L-11）：↑↓ 移动焦点，Enter 打开详情，空格切换勾选，Home/End 跳首尾。
function handleLibraryListKeydown(event) {
  var target = event.target && event.target.closest ? event.target.closest('.library-entry') : null;
  if (!target) return;
  var entries = Array.prototype.slice.call(document.querySelectorAll('#library-list .library-entry'));
  var idx = entries.indexOf(target);
  if (event.key === 'ArrowDown') { event.preventDefault(); if (entries[idx + 1]) entries[idx + 1].focus(); }
  else if (event.key === 'ArrowUp') { event.preventDefault(); if (entries[idx - 1]) entries[idx - 1].focus(); }
  else if (event.key === 'Home') { event.preventDefault(); if (entries[0]) entries[0].focus(); }
  else if (event.key === 'End') { event.preventDefault(); if (entries.length) entries[entries.length - 1].focus(); }
  else if (event.key === 'Enter') { event.preventDefault(); selectLibDoc(target.dataset.id); }
  else if (event.key === ' ' || event.key === 'Spacebar') { event.preventDefault(); toggleLibraryDeleteSelection(target.dataset.id); }
}

function setupLibraryKeyboardNav() {
  var list = document.getElementById('library-list');
  if (!list || list.dataset.keyboardReady === '1') return;
  list.dataset.keyboardReady = '1';
  list.addEventListener('keydown', handleLibraryListKeydown);
}

// 常驻全选（L-09）：工具栏三态复选框——空 / 半选 / 全选当前筛选结果，
// 既是全选入口，也是「这里可多选」的可发现锚点。
function syncLibrarySelectAll() {
  var box = document.getElementById('lib-select-all');
  if (!box) return;
  var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
  var selectedVisible = selectable.filter(function(item) { return libDeleteSelection.has(item.source_file_id); });
  var state = selectable.length === 0 ? 'empty'
    : selectedVisible.length === 0 ? 'empty'
    : selectedVisible.length === selectable.length ? 'all' : 'some';
  box.classList.toggle('is-all', state === 'all');
  box.classList.toggle('is-some', state === 'some');
  box.setAttribute('aria-checked', state === 'all' ? 'true' : state === 'some' ? 'mixed' : 'false');
  box.disabled = selectable.length === 0;
}

function handleLibraryEntryClick(event, sourceId) {
  if (suppressLibrarySelectionClick) {
    event.preventDefault();
    event.stopPropagation();
    return;
  }
  // Clicking the row/card body always opens details; the checkbox is the only
  // thing that toggles selection, so browsing never accidentally selects.
  selectLibDoc(sourceId);
}

function renderLibraryList() {
  const sources = getFilteredSources();
  const listEl = document.getElementById('library-list');
  listEl.className = 'library-list-container library-view-' + libViewMode;
  libDeleteSelection.forEach(function(sourceId) {
    if (!libSources.some(function(source) { return source.source_file_id === sourceId; })) {
      libDeleteSelection.delete(sourceId);
    }
  });
  // 筛选按钮角标 + 生效 chips + 弹层三组分面（含实时计数），一处渲染。
  renderLibraryFilterBar();
  renderLibraryScopes();

  libraryRenderToken += 1;
  if (sources.length === 0) {
    // 三态空状态：库为空 → 引导导入；有数据但筛选无果 → 清除筛选（L-13）。
    listEl.innerHTML = libSources.length === 0
      ? '<div class="empty-state" style="min-height:220px"><div class="empty-state-text">文献库还是空的</div><div class="empty-state-hint">导入 PDF 或 DOCX 后即可检索、校准页码、补全书目</div><button class="action-btn primary" style="margin-top:14px" onclick="navigateTo(\'import\')">去导入文献</button></div>'
      : '<div class="empty-state" style="min-height:220px"><div class="empty-state-text">当前范围没有匹配文献</div><div class="empty-state-hint">可切换文件夹，或清除搜索与筛选条件</div><button class="action-btn" style="margin-top:14px" onclick="clearLibraryFilters()">清除搜索与筛选</button></div>';
    updateLibraryDeleteControls();
    return;
  }
  // 首批同步渲染，其余按帧追加，避免大文献库一次性构建整张列表阻塞首屏。
  listEl.innerHTML = sources.slice(0, LIBRARY_RENDER_BATCH).map(libraryEntryHTML).join('');
  syncLibraryDeleteSelectionUI();
  if (sources.length > LIBRARY_RENDER_BATCH) {
    appendLibraryEntries(sources, LIBRARY_RENDER_BATCH, libraryRenderToken);
  }
}

function scheduleLibraryChunk(callback) {
  // 窗口最小化或隐藏时不会触发 requestAnimationFrame，剩余批次要靠定时器补齐，
  // 否则再显示出来就只剩首批 50 条。
  if (document.hidden || typeof requestAnimationFrame !== 'function') setTimeout(callback, 0);
  else requestAnimationFrame(callback);
}

function appendLibraryEntries(sources, start, token) {
  scheduleLibraryChunk(function() {
    if (token !== libraryRenderToken) return;
    var listEl = document.getElementById('library-list');
    if (!listEl) return;
    var end = Math.min(start + LIBRARY_RENDER_BATCH, sources.length);
    listEl.insertAdjacentHTML('beforeend', sources.slice(start, end).map(libraryEntryHTML).join(''));
    syncLibraryDeleteSelectionUI();
    if (end < sources.length) appendLibraryEntries(sources, end, token);
  });
}

function libraryEntryHTML(src) {
  var vol = volumeForSource(src.source_file_id);
  var isPdf = src.source_type === 'pdf';
  var documentTitle = src.title || (src.file_name || src.source_file_id);
  var inDocumentGroupScope = libScopeType === 'document_group'
    && String(src.document_group_id || '') === String(libScopeId || '');
  var title = inDocumentGroupScope ? documentGroupMemberDisplayName(src) : documentTitle;
  var author = inDocumentGroupScope ? documentTitle : (src.author || '作者信息待完善');
  var thesisIcon = src.document_type === 'thesis'
    ? '<svg class="doc-thesis-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-label="学位论文"><title>学位论文</title><path d="M22 10 12 5 2 10l10 5 10-5Z"/><path d="M6 12v5c0 1 2.7 2.5 6 2.5s6-1.5 6-2.5v-5"/><path d="M22 10v5"/></svg>'
    : '';
  var bib = sourceBibliographicMetadata(src);
  var missingMetadataText = isPdf ? bibliographicMissingText(bib) : '';
  var size = formatFileSize(src.size_bytes);
  var isSelected = src.source_file_id === libSelectedId;
  var isDeleteSelectable = isLibraryDeleteSelectable(src);
  var isDeleteSelected = libDeleteSelection.has(src.source_file_id);
  var typeCls = isPdf ? (src.parser_label === 'MinerU' ? 'mineru' : 'pdf') : 'word';
  var typeLabel = isPdf ? (src.parser_label || 'PDF') : 'Word';
  var itemStatus = isPdf ? (calTransientStatus[src.source_file_id] || src.status) : '';
  var statusGroup = isPdf ? calibrationStatusGroup(itemStatus) : '';
  var statusChip = isPdf
    ? '<span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(statusGroup) + ' ' + statusGroup + '">' + statusChipIcon(statusGroup) + esc(calibrationStatusLabel(itemStatus)) + '</span>'
    : '';
  // 列表模式空间窄：校准状态与「缺书目」都收成仅图标（含 title 悬停提示），
  // 让标题成为主列不被文字徽章挤掉。这也是 Zotero 等主流列表视图的做法。
  var statusIconOnly = isPdf
    ? '<span class="cal-status-icon status-chip--' + statusSemanticVariant(statusGroup) + ' ' + statusGroup + '" title="' + esc(calibrationStatusLabel(itemStatus)) + '" aria-label="' + esc(calibrationStatusLabel(itemStatus)) + '">' + statusChipIcon(statusGroup) + '</span>'
    : '';
  var missingIcon = missingMetadataText
    ? '<span class="library-row-missing-icon" title="' + esc(missingMetadataText) + '" aria-label="' + esc(missingMetadataText) + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><path d="M12 17.5h.01"/></svg></span>'
    : '';
  var wordStructure = !isPdf && vol && vol.primary_structure ? structureLabel(vol.primary_structure) : '';
  var countMeta = isPdf
    ? (src.page_count ? src.page_count + ' 页' : '页数未知')
    : ((src.works_count || 1) + ' 篇');
  // PDF and Word entries both carry a checkbox; CSS reveals it on hover or
  // while a selection is active. Word removal also clears the managed corpus
  // copy so a later full rebuild cannot silently add it back.
  var selectionControl = isDeleteSelectable
    ? '<input class="library-delete-check" type="checkbox" aria-label="选择 ' + esc(title) + '" ' + (isDeleteSelected ? 'checked ' : '') + 'onclick="event.stopPropagation();toggleLibraryDeleteSelection(\'' + esc(src.source_file_id) + '\',this.checked)">'
    : '';
  if (libViewMode === 'grid') {
    var imported = formatCalDate(src.imported_at || src.last_modified);
    var secondary = !isPdf ? ((vol && vol.corpus_title) || '') : '';
    return '<article class="library-card library-entry' + (isSelected ? ' selected' : '') + (isDeleteSelected ? ' delete-selected' : '') + '" tabindex="0" role="option" data-id="' + esc(src.source_file_id) + '" data-delete-selectable="' + (isDeleteSelectable ? '1' : '0') + '" aria-selected="' + (isDeleteSelected ? 'true' : 'false') + '" onclick="handleLibraryEntryClick(event,\'' + esc(src.source_file_id) + '\')">'
      + '<div class="library-card-top"><div class="library-card-badges"><span class="type-badge ' + typeCls + '">' + typeLabel + '</span>' + statusChip + (wordStructure ? '<span class="library-card-status">' + esc(wordStructure) + '</span>' : '') + (secondary ? '<span class="library-card-status">' + esc(secondary) + '</span>' : '') + '</div>' + selectionControl + '</div>'
      + '<div class="library-card-title">' + thesisIcon + esc(title) + '</div><div class="library-card-author">' + esc(author) + '</div>'
      + (missingMetadataText ? bibliographicMissingBadge(bib) : '')
      + '<div class="library-card-meta">' + esc(countMeta + ' · ' + size) + '</div>'
      + '<div class="library-card-mapping">' + esc(isPdf ? (src.mapping_summary || '尚未建立引用页码映射') : ((vol && vol.version_info) || 'Word 文献')) + '</div>'
      + '<div class="library-card-footer"><span class="library-card-action">查看详情</span><span class="library-card-date">' + esc(imported === '未知' ? '日期未知' : imported + ' 导入') + '</span></div></article>';
  }
  return '<div class="library-row library-entry' + (isSelected ? ' selected' : '') + (isDeleteSelected ? ' delete-selected' : '') + '" tabindex="0" role="option" data-id="' + esc(src.source_file_id) + '" data-delete-selectable="' + (isDeleteSelectable ? '1' : '0') + '" aria-selected="' + (isDeleteSelected ? 'true' : 'false') + '" onclick="handleLibraryEntryClick(event,\'' + esc(src.source_file_id) + '\')">'
    + selectionControl
    + '<span class="type-badge ' + typeCls + '">' + typeLabel + '</span>'
    + '<span class="library-row-title">' + thesisIcon + esc(title) + '</span>'
    + '<span class="library-row-author">' + esc(author) + '</span>'
    + '<span class="library-row-info">'
    + statusIconOnly
    + missingIcon
    + (wordStructure ? '<span class="library-card-status">' + esc(wordStructure) + '</span>' : '')
    + '<span class="works-count">' + esc(countMeta) + '</span>'
    + '<span class="library-row-size">' + size + '</span>'
    + '</span>'
    + '</div>';
}

// 详情抽屉按插槽渲染，不再堆进一个巨型 innerHTML：
//   #library-drawer-content = 上一条/下一条 + 标题 + 徽章 + 书目区（读/写态）
//   #library-drawer-calibration（模板静态卡片，介于两个插槽之间）= 页码校准
//   #library-drawer-extra = 收录文献 + 文件信息 + 主操作栏
// 区块顺序落为：书目 → 页码校准 → 收录文献 → 文件信息 → 主操作。
function drawerNavHTML(sourceId) {
  var list = getFilteredSources();
  var idx = list.findIndex(function(s) { return s.source_file_id === sourceId; });
  if (idx < 0 || list.length <= 1) return '';
  var prevId = idx > 0 ? list[idx - 1].source_file_id : '';
  var nextId = idx < list.length - 1 ? list[idx + 1].source_file_id : '';
  function btn(id, label, arrow) {
    return '<button class="drawer-nav-btn" type="button" aria-label="' + label + '"'
      + (id ? ' onclick="selectLibDoc(\'' + esc(id) + '\')"' : ' disabled') + '>' + arrow + '</button>';
  }
  return '<div class="drawer-nav">' + btn(prevId, '上一条文献', '‹')
    + '<span class="drawer-nav-pos" aria-live="polite">' + (idx + 1) + ' / ' + list.length + '</span>'
    + btn(nextId, '下一条文献', '›') + '</div>';
}

function drawerStatusPill(src) {
  if (src.source_type !== 'pdf') return '';
  var status = calTransientStatus[src.source_file_id] || src.status;
  var group = calibrationStatusGroup(status);
  return '<span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(group) + ' ' + group + '">'
    + statusChipIcon(group) + esc(calibrationStatusLabel(status)) + '</span>';
}

// 收录文献：不再内层滚动（L-14），随抽屉整体滚动，避免滚轮被内层吞掉。
function drawerWorksHTML(works) {
  if (!works.length) return '';
  return '<div class="drawer-section-title">收录文献 (' + works.length + ')</div>'
    + '<div class="drawer-works-list">'
    + works.map(function(w) {
      var meta = [];
      if (w.author_label) meta.push(w.author_label);
      if (w.date_label) meta.push(w.date_label);
      if (w.toc_page_start) meta.push('p.' + w.toc_page_start + (w.toc_page_end ? '–' + w.toc_page_end : ''));
      return '<div class="drawer-work-item"><div class="drawer-work-title">' + esc(w.title) + '</div>'
        + (meta.length ? '<div class="drawer-work-meta">' + esc(meta.join(' · ')) + '</div>' : '') + '</div>';
    }).join('') + '</div>';
}

function drawerFileInfoHTML(src, vol) {
  var info = '';
  info += drawerInfoRow('文件类型', src.source_type === 'pdf' ? 'PDF 文档' : 'Word 文档');
  info += drawerInfoRow('文件名', src.file_name);
  info += drawerInfoRow('大小', formatFileSize(src.size_bytes));
  if (src.source_type === 'pdf' && src.pdf_profile) {
    info += drawerInfoRow('PDF 页数', src.pdf_profile.pdf_page_count + ' 页');
    info += drawerInfoRow('PDF 类型', pdfTypeLabel(src.pdf_profile.detected_pdf_type));
    info += drawerInfoRow('页码状态', mappingStatusLabel(src.pdf_profile.mapping_status));
    if (src.pdf_profile.auto_page_mapping) {
      var autoMap = src.pdf_profile.auto_page_mapping;
      info += drawerInfoRow('自动页码映射', autoMap.method === 'manual_override'
        ? '保留人工映射'
        : '应用 ' + (autoMap.applied_segment_count || 0) + ' 个自动段，候选 ' + (autoMap.candidate_count || 0) + ' 个');
      if (autoMap.applied_segments && autoMap.applied_segments.length) info += drawerInfoRow('自动映射区间', autoMap.applied_segments.map(autoMappingSegmentText).join('；'));
      if (autoMap.exception_pages && autoMap.exception_pages.length) info += drawerInfoRow('异常页面', autoMap.exception_pages.length + ' 页');
    }
  }
  if (src.last_modified) info += drawerInfoRow('修改日期', src.last_modified.split('T')[0]);
  if (vol && vol.version_info) info += drawerInfoRow('版本', vol.version_info);
  return '<div class="drawer-collapse" id="drawer-file-info">'
    + '<button class="cal-collapse-head" type="button" aria-expanded="false" onclick="toggleDrawerSection(event,\'drawer-file-info\')">'
    + '<span class="drawer-section-title">文件信息</span>'
    + '<span class="cal-collapse-summary">' + esc(formatFileSize(src.size_bytes) + (src.source_type === 'pdf' && src.pdf_profile && src.pdf_profile.pdf_page_count ? ' · ' + src.pdf_profile.pdf_page_count + ' 页' : '')) + '</span>'
    + '<svg class="cal-collapse-chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>'
    + '</button>'
    + '<div class="drawer-collapse-body" style="display:none"><div class="drawer-info">' + info + '</div></div>'
    + '</div>';
}

function drawerVersionMetadataHTML(src) {
  if (!src.document_group_id) return '';
  var explicit = String((src.version_metadata && src.version_metadata.version_label) || '').trim();
  return '<div class="drawer-info"><div class="drawer-info-row">'
    + '<span class="drawer-info-label">作品组版本</span>'
    + '<span class="drawer-info-value">' + esc(documentGroupMemberDisplayName(src))
    + (explicit ? '' : ' <small>（自动生成）</small>') + '</span>'
    + '<button class="action-btn sm" type="button" onclick="openLibraryOrganizationModal(\'version_label\',\'' + esc(src.source_file_id) + '\')">编辑</button>'
    + '</div></div>';
}

// 主操作栏收敛为「打开原文」+ ⋯（重新解析 / 导出 / 页码动作 / 移除）。
// 「自动检测页码 / 编辑区间」不再在这里重复——页码校准卡片是唯一入口（L-04）。
function drawerMainActionsHTML(src) {
  var sid = esc(src.source_file_id);
  var moreSvg = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>';
  var items = '';
  if (src.source_type === 'pdf') {
    var ocrLabel = src.parser_type === 'mineru_structured' ? '重新 OCR' : 'MinerU 在线解析';
    var ocrRunning = calTransientStatus[src.source_file_id] === 'mapping';
    items += '<button class="bib-menu-item" type="button" role="menuitem"' + (ocrRunning ? ' disabled' : '') + ' onclick="bibCloseMenus();submitMineruReparse(\'' + sid + '\')">' + (ocrRunning ? '正在解析…' : ocrLabel) + '</button>';
    var am = src.pdf_profile && src.pdf_profile.auto_page_mapping;
    if (am && am.applied_segments && am.applied_segments.length) items += '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibCloseMenus();acceptAutoMapping(\'' + sid + '\')">接受自动映射</button>';
    if (am && am.exception_pages && am.exception_pages.length) items += '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibCloseMenus();showAutoMappingExceptions(\'' + sid + '\')">检查异常</button>';
    items += '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibCloseMenus();exportLibraryDocument(\'' + sid + '\')">导出 MEFinder 文档包</button>';
    items += '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibCloseMenus();exportLibraryDocumentMarkdown(\'' + sid + '\')">导出 Markdown</button>';
    items += '<div class="bib-menu-sep"></div>';
  }
  items += '<button class="bib-menu-item bib-menu-item-danger" type="button" role="menuitem" onclick="bibCloseMenus();openRemoveDocumentModal(\'' + sid + '\')">从文献库移除</button>';
  return '<div class="drawer-actions">'
    + (src.source_file_id ? '<button class="action-btn primary" onclick="openSource(\'' + sid + '\', null)">打开原文</button>' : '')
    + '<span class="drawer-actions-spacer"></span>'
    + '<span class="bib-menu-wrap"><button class="action-btn bib-caret-only" type="button" aria-label="更多操作" aria-haspopup="true" aria-expanded="false" aria-controls="drawer-more-menu" onclick="bibToggleMenu(event,\'drawer-more-menu\')">' + moreSvg + '</button>'
    + '<span class="bib-menu bib-menu-end drawer-actions-menu" id="drawer-more-menu" role="menu">' + items + '</span></span>'
    + '</div>';
}

async function exportLibraryDocument(sourceId) {
  if (!sourceId) return;
  try {
    var outputDirectory = await chooseDesktopExportDirectory();
    if (outputDirectory === null) return;
    showToast('正在导出 MEFinder 文档包…');
    var data = await requestLibraryDocumentExport(sourceId, outputDirectory);
    showToast('已导出 ' + Number(data.page_count || 0).toLocaleString()
      + ' 页' + (data.includes_source_pdf ? '，包含原 PDF' : '')
      + ' 到：' + data.path + '（' + formatFileSize(data.size_bytes) + '）');
  } catch (error) {
    showToast('导出 MEFinder 文档失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
  }
}

async function requestLibraryDocumentExport(sourceId, outputDirectory) {
  var payload = {
    source_id: sourceId,
    include_source_pdf: currentDocumentExportMode === 'with_pdf'
  };
  if (outputDirectory) payload.output_dir = outputDirectory;
  var response = await fetch('/api/document/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  var data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || '导出失败');
  return data;
}

async function exportLibraryDocumentMarkdown(sourceId) {
  if (!sourceId) return;
  try {
    var outputDirectory = await chooseDesktopExportDirectory();
    if (outputDirectory === null) return;
    showToast('正在导出 Markdown…');
    var data = await requestLibraryDocumentMarkdownExport(sourceId, outputDirectory);
    showToast('已导出 ' + Number(data.page_count || 0).toLocaleString()
      + ' 页到：' + data.path + '（' + formatFileSize(data.size_bytes) + '）');
  } catch (error) {
    showToast('导出 Markdown 失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
  }
}

async function requestLibraryDocumentMarkdownExport(sourceId, outputDirectory) {
  var payload = {source_id: sourceId};
  if (outputDirectory) payload.output_dir = outputDirectory;
  var response = await fetch('/api/document/export-markdown', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  var data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || '导出失败');
  return data;
}

async function exportSelectedLibraryDocuments() {
  if (libraryExportRunning) return;
  var items = libSources.filter(function(item) {
    return item.source_type === 'pdf' && libDeleteSelection.has(item.source_file_id);
  });
  if (!items.length) {
    showToast('所选文献中没有可导出的 PDF', 'warning');
    return;
  }

  var outputDirectory;
  try {
    outputDirectory = await chooseDesktopExportDirectory();
  } catch (error) {
    showToast('选择导出文件夹失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
    return;
  }
  if (outputDirectory === null) return;

  libraryExportRunning = true;
  var exportButton = document.getElementById('library-export-selected-btn');
  var exported = [];
  var failures = [];
  var skippedWordCount = libDeleteSelection.size - items.length;
  updateLibraryDeleteControls();
  try {
    for (var index = 0; index < items.length; index += 1) {
      exportButton.textContent = '正在导出 ' + (index + 1) + ' / ' + items.length;
      try {
        exported.push(await requestLibraryDocumentExport(
          items[index].source_file_id,
          outputDirectory
        ));
      } catch (error) {
        failures.push({
          title: items[index].title || items[index].file_name || items[index].source_file_id,
          message: error && error.message ? error.message : '未知错误'
        });
      }
    }
  } finally {
    libraryExportRunning = false;
    updateLibraryDeleteControls();
  }

  var skippedText = skippedWordCount ? '；已跳过 ' + skippedWordCount + ' 份 Word' : '';
  if (failures.length) {
    showToast('批量导出完成：成功 ' + exported.length + ' 本，失败 ' + failures.length + ' 本'
      + skippedText + '。首个失败：' + failures[0].title + '：' + failures[0].message, 'warning');
    return;
  }
  outputDirectory = outputDirectory || exported[0].path.replace(/[\\/][^\\/]+$/, '');
  showToast('已导出 ' + exported.length + ' 个文档包'
    + (currentDocumentExportMode === 'with_pdf' ? '（包含原 PDF）' : '') + skippedText
    + '，每本一个文档包。保存到：' + outputDirectory, 'success');
}

async function selectLibDoc(sourceId) {
  // 切到别的文献前拦一道未保存修改；同一文献的重选（识别/保存后刷新）不打扰。
  var switchingDoc = sourceId !== libSelectedId;
  if (switchingDoc && !await guardLeaveDetail()) return;
  if (switchingDoc) bibEditMode[sourceId] = false;  // 新文献默认查看态
  libSelectedId = sourceId;
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) {
    row.classList.toggle('selected', row.dataset.id === sourceId);
  });
  if (!libSources.some(function(s) { return s.source_file_id === sourceId; })) return;
  try {
    await ensureLibraryDetail(sourceId);
  } catch (error) {
    showToast(error && error.message ? error.message : '文献详情读取失败', 'danger');
    return;
  }
  // 详情是异步补齐的，期间用户可能已经关掉抽屉或换了一份文献。
  if (libSelectedId !== sourceId) return;
  var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
  if (!src) return;
  var vol = volumeForSource(sourceId);
  var works = vol ? libWorks.filter(function(w) { return w.volume_id === vol.volume_id; }) : [];
  var title = vol ? vol.display_title : (src.file_name || sourceId);
  var corpusTitle = vol ? (vol.corpus_title || '') : '';

  var bibliographicHTML = '';
  if (src.source_type === 'pdf') {
    // 选中即以当前元数据初始化字段缓存；切类型只在缓存里保留隐藏字段，不会丢。
    bibFieldCache[sourceId] = bibFieldCacheFromMeta(sourceBibliographicMetadata(src));
    bibliographicHTML = renderBibliographicSection(src);
  }

  var content = document.getElementById('library-drawer-content');
  content.innerHTML = drawerNavHTML(sourceId)
    + '<div class="drawer-title" tabindex="-1">' + esc(title) + '</div>'
    + (corpusTitle ? '<div class="drawer-subtitle">' + esc(corpusTitle) + '</div>' : '')
    + '<div class="detail-pills" style="margin-top:12px">'
    + '<span class="detail-pill">' + (src.source_type === 'pdf' ? 'PDF' : 'Word') + '</span>'
    + (vol && vol.primary_structure ? '<span class="detail-pill">' + structureLabel(vol.primary_structure) + '</span>' : '')
    + drawerStatusPill(src)
    + '</div>'
    + bibliographicHTML;

  var extra = document.getElementById('library-drawer-extra');
  if (extra) extra.innerHTML = drawerVersionMetadataHTML(src) + drawerWorksHTML(works) + drawerFileInfoHTML(src, vol) + drawerMainActionsHTML(src);

  document.getElementById('library-drawer').classList.add('open');
  var body = document.querySelector('#page-library .library-body');
  if (body) body.classList.add('detail-open');
  renderDrawerCalibration(src);
  // 新开详情时把焦点移到标题，便于读屏播报；同一文献刷新不夺焦点。
  if (switchingDoc) {
    var titleEl = content.querySelector('.drawer-title');
    if (titleEl) titleEl.focus();
  }
}

function renderDrawerCalibrationSummary(src) {
  var summary = document.getElementById('cal-collapse-summary');
  if (!summary) return;
  var status = calTransientStatus[src.source_file_id] || src.status;
  var group = calibrationStatusGroup(status);
  summary.innerHTML = '<span class="cal-status-badge status-chip status-chip--' + statusSemanticVariant(group) + ' ' + group + '">' + statusChipIcon(group) + esc(calibrationStatusLabel(status)) + '</span>'
    + '<span class="cal-collapse-mapping">' + esc(src.mapping_summary || '尚未建立引用页码映射') + '</span>';
}

function renderDrawerCalibration(src) {
  var host = document.getElementById('library-drawer-calibration');
  if (!host) return;
  var isPdf = src.source_type === 'pdf';
  host.style.display = isPdf ? 'block' : 'none';
  host.classList.remove('expanded');
  document.getElementById('cal-section-body').style.display = 'none';
  var toggle = document.getElementById('cal-collapse-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
  if (!isPdf) {
    calSelectedSourceId = null;
    return;
  }
  renderDrawerCalibrationSummary(src);
}

async function toggleDrawerCalibration(forceOpen) {
  var host = document.getElementById('library-drawer-calibration');
  var body = document.getElementById('cal-section-body');
  if (!host || !body) return;
  var open = forceOpen === true ? true : body.style.display === 'none';
  body.style.display = open ? 'block' : 'none';
  host.classList.toggle('expanded', open);
  var toggle = document.getElementById('cal-collapse-toggle');
  if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open && libSelectedId && calSelectedSourceId !== libSelectedId) {
    calSelectedSourceId = libSelectedId;
    await loadCalibrationDoc(libSelectedId);
  }
}

function updateLibraryEntry(sourceId) {
  renderLibraryStats();
  renderLibraryList();
  if (libSelectedId === sourceId) {
    var src = libSources.find(function(s) { return s.source_file_id === sourceId; });
    if (src && src.source_type === 'pdf') renderDrawerCalibrationSummary(src);
  }
}
