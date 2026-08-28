/* ═══ Library ═══ */
function applyLibraryCatalog(data) {
  libraryStore.sources = data.items || [];
  libraryStore.volumes = data.volumes || [];
  libraryStore.volumeBySource = buildVolumeIndex(libraryStore.volumes);
  libraryStore.stats = data.stats || null;
  libraryStore.loaded = true;
  // 搜索下拉与文献库共用同一份摘要，避免两处各拉一次。
  searchStore.sourceFiles = libraryStore.sources;
  searchStore.volumes = libraryStore.volumes;
  searchStore.documentsLoaded = true;
}

async function loadLibrary(force) {
  try {
    applyLibraryCatalog(await fetchLibraryCatalog(force));
    await loadDocumentGroups();
    renderLibraryStats();
    syncLibraryViewButtons();
    syncLibrarySortControls();
    renderGroupScopeSelector();
    renderLibraryList();
  } catch(e) {
    document.getElementById('library-list').innerHTML = '<div class="empty-state" style="min-height:200px"><div class="empty-state-text">' + esc(e.message || '文献库加载失败') + '</div></div>';
  }
}

// 作品组只做「限定 source_file 集合」（不引入 folder/root scope）：拉列表，scope 变化即重绘。
async function loadDocumentGroups() {
  try {
    var response = await fetch('/api/document-groups');
    var data = await response.json();
    libraryStore.documentGroups = (response.ok && !data.error && Array.isArray(data.document_groups))
      ? data.document_groups : [];
  } catch (_) {
    libraryStore.documentGroups = [];
  }
  if (libraryStore.groupScopeId && !libraryStore.documentGroups.some(function(g) { return g.document_group_id === libraryStore.groupScopeId; })) {
    libraryStore.groupScopeId = '';
  }
  if (searchStore.groupId && !libraryStore.documentGroups.some(function(g) { return g.document_group_id === searchStore.groupId; })) {
    searchStore.groupId = '';
    updateSearchDocumentLabel();
  }
}

function documentGroupById(groupId) {
  return libraryStore.documentGroups.find(function(g) { return g.document_group_id === groupId; }) || null;
}

function documentGroupMemberIdSet(groupId) {
  var group = documentGroupById(groupId);
  var ids = new Set();
  if (group) (group.members || []).forEach(function(m) { ids.add(m.source_file_id); });
  return ids;
}

// 当前 scope 下某文献的成员版本名（display_name 由 /api/document-groups 用 B 的 fallback 算好）。
function documentGroupMemberLabel(groupId, sourceId) {
  var group = documentGroupById(groupId);
  if (!group) return '';
  var member = (group.members || []).find(function(m) { return m.source_file_id === sourceId; });
  return member ? (member.display_name || '') : '';
}

function documentSupportsTextAlignment(source) {
  var facet = libraryFileFacet(source);
  return facet === 'pdf' || facet === 'epub';
}

function setLibraryGroupScope(groupId) {
  libraryStore.groupScopeId = groupId || '';
  closeAppSelects();
  clearLibrarySelection();
  renderGroupScopeSelector();
  renderLibraryStats();
  renderLibraryList();
}

// 工具栏轻量作品组入口（无常驻左栏、无 fold）：全部文献 / 各作品组（含版本数）。
function renderGroupScopeSelector() {
  var menu = document.getElementById('library-group-scope-menu');
  var label = document.getElementById('library-group-scope-label');
  var wrap = document.getElementById('library-group-scope');
  if (!menu || !label) return;
  var current = documentGroupById(libraryStore.groupScopeId);
  label.textContent = current ? current.title : '全部文献';
  if (wrap) wrap.classList.toggle('is-scoped', !!current);
  var html = '<button class="app-select-option lib-group-opt' + (libraryStore.groupScopeId ? '' : ' is-selected')
    + '" type="button" role="option" onclick="setLibraryGroupScope(\'\')">'
    + '<span class="lib-group-name">全部文献</span></button>';
  if (libraryStore.documentGroups.length) {
    html += '<div class="lib-group-sep" role="separator"></div>';
    html += libraryStore.documentGroups.map(function(g) {
      var count = (g.members || []).length;
      return '<button class="app-select-option lib-group-opt' + (g.document_group_id === libraryStore.groupScopeId ? ' is-selected' : '')
        + '" type="button" role="option" onclick="setLibraryGroupScope(\'' + esc(g.document_group_id) + '\')">'
        + '<span class="lib-group-name">' + esc(g.title) + '</span>'
        + '<span class="lib-group-count">' + count + ' 个版本</span></button>';
    }).join('');
  }
  html += groupScopeManageOptionsHTML();
  menu.innerHTML = html;
}

// C2：选择器底部追加管理入口。
function groupScopeManageOptionsHTML() {
  return '<div class="lib-group-sep" role="separator"></div>'
    + '<button class="app-select-option lib-group-manage" type="button" onclick="closeAppSelects();openManageDocumentGroups()">管理作品组…</button>';
}

function openManageDocumentGroups() {
  var modal = document.getElementById('group-manage-modal');
  if (!modal) return;
  groupPicker = { groupId: '', query: '', selected: {}, focusPending: false };
  renderDocumentGroupManager();
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
}

function closeGroupManageModal() {
  var modal = document.getElementById('group-manage-modal');
  if (modal) { modal.classList.remove('open'); modal.setAttribute('aria-hidden', 'true'); }
}

function groupManageBackdrop(event) {
  if (event.target && event.target.id === 'group-manage-modal') closeGroupManageModal();
}

function documentGroupAlignmentForPair(group, leftSourceId, rightSourceId) {
  return (group.alignments || []).find(function(item) {
    return item.status === 'completed' && (
      (item.pivot_source_file_id === leftSourceId && item.target_source_file_id === rightSourceId) ||
      (item.pivot_source_file_id === rightSourceId && item.target_source_file_id === leftSourceId)
    );
  });
}

function syncDocumentGroupPairAction(groupId) {
  var group = documentGroupById(groupId);
  var left = document.getElementById('grp-pair-left-' + groupId);
  var right = document.getElementById('grp-pair-right-' + groupId);
  var button = document.getElementById('grp-pair-generate-' + groupId);
  var status = document.getElementById('grp-pair-status-' + groupId);
  if (!group || !left || !right || !button || !status) return;
  var distinct = left.value && right.value && left.value !== right.value;
  var existing = distinct
    ? documentGroupAlignmentForPair(group, left.value, right.value)
    : null;
  button.disabled = !distinct;
  button.textContent = existing ? '重新生成' : '生成对照';
  status.textContent = distinct
    ? (existing ? '这两个版本已有直接对照' : '这两个版本尚未生成直接对照')
    : '请选择两个不同版本';
}

function generateSelectedTextAlignmentAction(groupId, button) {
  var left = document.getElementById('grp-pair-left-' + groupId);
  var right = document.getElementById('grp-pair-right-' + groupId);
  if (!left || !right) return;
  generateTextAlignmentAction(groupId, left.value, right.value, button);
}

// 单一「管理作品组」弹窗承载成员管理，并把默认基准与任意两版的直接对照分开。
function documentGroupSourceLabel(source) {
  var format = sourceFormatLabel(source);
  var parser = source.parser_type === 'native_text' ? '原生文本' : source.parser_label;
  return parser && parser !== format ? format + ' · ' + parser : format;
}

// 内嵌「添加已有文献」选择器：与「生成对照」同一套卡片母题，展开时齐平在组卡片内。
// groupId 记当前展开的组；query/selected 在整个 manager 重绘间保留，避免搜索时丢焦点。
var groupPicker = { groupId: '', query: '', selected: {}, focusPending: false };

function renderDocumentGroupManager() {
  var body = document.getElementById('group-manage-body');
  if (!body) return;
  var selectedCount = libraryStore.deleteSelection.size;
  var pairGroups = [];
  var html = '<div class="grp-create"><label class="grp-create-field">'
    + '<span class="grp-field-label">作品组标题</span>'
    + '<input id="grp-create-input" class="grp-input" type="text" placeholder="例如：法哲学原理" onkeydown="if(event.key===\'Enter\'){event.preventDefault();createDocumentGroupInline();}">'
    + '</label>'
    + '<button id="grp-create-btn" class="action-btn primary" type="button" onclick="createDocumentGroupInline()">新建</button></div>';
  if (selectedCount) {
    html += '<div class="grp-assign-hint">已选 ' + selectedCount + ' 份文献——点某个作品组的「加入所选」把它们归入该作品组</div>';
  }
  if (!libraryStore.documentGroups.length) {
    html += '<div class="grp-empty">还没有作品组。作品组用于把「同一部作品的不同版本 / 原文 / 译本」归到一起，不是文件夹</div>';
  }
  libraryStore.documentGroups.forEach(function(g) {
    var gid = esc(g.document_group_id);
    var pickerOpen = groupPicker.groupId === g.document_group_id;
    html += '<div class="grp-block"><div class="grp-head">'
      + '<input class="grp-input grp-title" value="' + esc(g.title) + '" aria-label="作品组标题" onchange="renameDocumentGroupInline(\'' + gid + '\', this.value)">'
      + '<div class="grp-head-actions">'
      + '<button class="action-btn sm' + (pickerOpen ? ' is-active' : '') + '" type="button" onclick="toggleGroupPicker(\'' + gid + '\')">＋ 添加文献</button>'
      + (selectedCount ? '<button class="action-btn sm" type="button" onclick="assignSelectedToGroupAction(\'' + gid + '\',this)">加入所选（' + selectedCount + '）</button>' : '')
      + '<button class="action-btn sm danger-outline" type="button" onclick="deleteDocumentGroupAction(\'' + gid + '\',this)">删除组</button>'
      + '</div></div>';
    var members = g.members || [];
    if (!members.length) {
      html += '<div class="grp-empty grp-empty--sm">尚无成员。点上方「添加文献」从已导入的文献里勾选加入，或在文献列表勾选后用底部「设置作品组」加入</div>';
    } else {
      html += '<div class="grp-members">' + members.map(function(m) {
        var sid = esc(m.source_file_id);
        var src = libraryStore.sources.find(function(s) { return s.source_file_id === m.source_file_id; });
        var srcTitle = src ? (src.title || src.file_name || m.source_file_id) : m.source_file_id;
        var isBase = m.source_file_id === g.base_source_file_id;
        var language = src ? libLangChipLabel(libraryLanguageCode(src)) : '未识别语言';
        var format = src ? documentGroupSourceLabel(src) : '未知格式';
        return '<div class="grp-member' + (isBase ? ' is-base' : '') + '">'
          + '<span class="grp-member-main"><span class="grp-member-title" title="' + esc(srcTitle) + '">' + esc(srcTitle) + '</span>'
          + '<span class="grp-member-meta"><span>' + esc(language) + '</span><span>' + esc(format) + '</span></span></span>'
          + '<input class="grp-input grp-vlabel" value="' + esc(m.version_label || '') + '" placeholder="' + esc(m.display_name || '') + '" aria-label="版本名称" onchange="setMemberVersionLabelInline(\'' + sid + '\', this.value)">'
          + '<button class="grp-base-btn' + (isBase ? ' is-base' : '') + '" type="button"'
          + (isBase ? ' disabled' : ' onclick="setGroupBaseAction(\'' + gid + '\',\'' + sid + '\')"')
          + '>' + (isBase ? '默认基准' : '设为默认') + '</button>'
          + '<button class="grp-remove-btn" type="button" aria-label="从作品组移除" title="从作品组移除" onclick="removeGroupMemberAction(\'' + sid + '\')">✕</button>'
          + '</div>';
      }).join('') + '</div>';
    }
    var supported = members.map(function(m) {
      var src = libraryStore.sources.find(function(s) { return s.source_file_id === m.source_file_id; });
      return src && documentSupportsTextAlignment(src) ? {member:m, source:src} : null;
    }).filter(Boolean);
    if (supported.length >= 2) {
      var chinese = supported.find(function(item) {
        return libraryLanguageCode(item.source).indexOf('zh') === 0;
      });
      var english = supported.find(function(item) {
        return libraryLanguageCode(item.source) === 'en';
      });
      var leftId = chinese && english
        ? chinese.member.source_file_id
        : (supported.find(function(item) {
          return item.member.source_file_id === g.base_source_file_id;
        }) || supported[0]).member.source_file_id;
      var rightId = chinese && english
        ? english.member.source_file_id
        : supported.find(function(item) {
          return item.member.source_file_id !== leftId;
        }).member.source_file_id;
      var optionHtml = function(selectedId) {
        return supported.map(function(item) {
          var source = item.source;
          var member = item.member;
          var version = member.version_label || member.display_name || source.title || source.file_name;
          var label = version + ' · ' + libLangChipLabel(libraryLanguageCode(source)) + ' · ' + documentGroupSourceLabel(source);
          return '<option value="' + esc(member.source_file_id) + '"' + (member.source_file_id === selectedId ? ' selected' : '') + '>' + esc(label) + '</option>';
        }).join('');
      };
      var existingPair = documentGroupAlignmentForPair(g, leftId, rightId);
      html += '<div class="grp-pair"><div class="grp-pair-copy">'
        + '<strong>生成双栏对照</strong><span id="grp-pair-status-' + gid + '">'
        + (existingPair ? '这两个版本已有直接对照' : '这两个版本尚未生成直接对照')
        + '</span></div><div class="grp-pair-controls">'
        + '<label><span class="visually-hidden">左栏版本</span><select id="grp-pair-left-' + gid + '" class="grp-pair-select" onchange="syncDocumentGroupPairAction(\'' + gid + '\')">' + optionHtml(leftId) + '</select></label>'
        + '<span class="grp-pair-arrow" aria-hidden="true">↔</span>'
        + '<label><span class="visually-hidden">右栏版本</span><select id="grp-pair-right-' + gid + '" class="grp-pair-select" onchange="syncDocumentGroupPairAction(\'' + gid + '\')">' + optionHtml(rightId) + '</select></label>'
        + '<button id="grp-pair-generate-' + gid + '" class="grp-align-btn" type="button" onclick="generateSelectedTextAlignmentAction(\'' + gid + '\',this)">' + (existingPair ? '重新生成' : '生成对照') + '</button>'
        + '</div></div>';
      pairGroups.push(g.document_group_id);
    } else if (members.length) {
      html += '<div class="grp-pair grp-pair--empty">至少需要两个 PDF / EPUB 版本才能生成双栏对照</div>';
    }
    if (pickerOpen) {
      html += '<div class="grp-pick">'
        + '<div class="grp-pick-search">'
        + '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="9" r="6"/><path d="m14 14 3 3"/></svg>'
        + '<input id="grp-pick-input-' + gid + '" class="grp-input" type="text" placeholder="搜索书名、作者、文件名…" value="' + esc(groupPicker.query) + '" oninput="groupPickerInputAction(\'' + gid + '\', this.value)" aria-label="搜索已导入文献">'
        + '</div>'
        + '<div id="grp-pick-list-' + gid + '" class="grp-pick-list"></div>'
        + '<div class="grp-pick-foot"><span id="grp-pick-count-' + gid + '" class="grp-pick-count"></span>'
        + '<button class="grp-base-btn" type="button" onclick="toggleGroupPicker(\'' + gid + '\')">完成</button>'
        + '<button class="grp-align-btn" type="button" onclick="addGroupPickedAction(\'' + gid + '\', this)">加入所选</button>'
        + '</div></div>';
    }
    html += '</div>';
  });
  body.innerHTML = html;
  pairGroups.forEach(syncDocumentGroupPairAction);
  if (groupPicker.groupId) {
    renderGroupPickerList(groupPicker.groupId);
    var pickInput = document.getElementById('grp-pick-input-' + groupPicker.groupId);
    if (pickInput && groupPicker.focusPending) {
      groupPicker.focusPending = false;
      pickInput.focus();
    }
  }
}

async function postGroupOp(path, payload, successMessage) {
  var response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  var data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || '操作失败');
  await loadDocumentGroups();
  renderGroupScopeSelector();
  renderDocumentGroupManager();
  renderLibraryList();
  if (successMessage) showToast(successMessage, 'success');
  return data.result || {};
}

async function createDocumentGroupInline() {
  var button = document.getElementById('grp-create-btn');
  if (button.disabled) return;
  var input = document.getElementById('grp-create-input');
  var title = input ? input.value.trim() : '';
  if (!title) { showToast('请输入作品组标题', 'warning'); return; }
  button.disabled = true;
  button.textContent = '创建中…';
  try {
    await postGroupOp('/api/document-groups/create', {title: title}, '作品组已创建');
    var again = document.getElementById('grp-create-input');
    if (again) again.focus();
  } catch (e) { showToast(e.message || '创建失败', 'danger'); }
  finally { button.disabled = false; button.textContent = '新建'; }
}

async function renameDocumentGroupInline(groupId, value) {
  var title = (value || '').trim();
  if (!title) { renderDocumentGroupManager(); return; }
  try { await postGroupOp('/api/document-groups/rename', {document_group_id: groupId, title: title}, '已重命名'); }
  catch (e) { showToast(e.message || '重命名失败', 'danger'); }
}

async function deleteDocumentGroupAction(groupId, button) {
  if (button.disabled) return;
  var group = documentGroupById(groupId);
  var name = group ? group.title : '';
  button.disabled = true;
  button.textContent = '等待确认…';
  try {
    if (!await showAppConfirm('删除作品组「' + name + '」只解除版本归组关系，不会删除任何文献。', {title: '删除作品组？', tone: 'warning', confirmText: '删除作品组'})) return;
    button.textContent = '删除中…';
    await postGroupOp('/api/document-groups/delete', {document_group_id: groupId}, '作品组已删除（文献仍保留）');
  }
  catch (e) { showToast(e.message || '删除失败', 'danger'); }
  finally { button.disabled = false; button.textContent = '删除组'; }
}

async function setGroupBaseAction(groupId, sourceId) {
  try { await postGroupOp('/api/document-groups/set-base', {document_group_id: groupId, base_source_file_id: sourceId}, sourceId ? '已设为基准版本' : '已取消基准版本'); }
  catch (e) { showToast(e.message || '设置基准失败', 'danger'); }
}

async function removeGroupMemberAction(sourceId) {
  try { await postGroupOp('/api/document-groups/remove-member', {source_file_id: sourceId}, '已从作品组移除（文献仍保留）'); }
  catch (e) { showToast(e.message || '移除失败', 'danger'); }
}

async function setMemberVersionLabelInline(sourceId, value) {
  try { await postGroupOp('/api/document-groups/version-label', {source_file_id: sourceId, version_label: (value || '').trim()}, '版本名称已更新'); }
  catch (e) { showToast(e.message || '更新失败', 'danger'); }
}

async function assignSelectedToGroupAction(groupId, button) {
  if (button.disabled) return;
  var ids = Array.from(libraryStore.deleteSelection);
  if (!ids.length) { showToast('请先在文献列表勾选文献', 'warning'); return; }
  var label = button.textContent;
  button.disabled = true;
  button.textContent = '加入中…';
  try {
    for (var i = 0; i < ids.length; i += 1) {
      var response = await fetch('/api/document-groups/add-member', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({document_group_id: groupId, source_file_id: ids[i]})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '加入失败');
    }
    clearLibrarySelection();
    await loadDocumentGroups();
    renderGroupScopeSelector();
    renderDocumentGroupManager();
    renderLibraryList();
    showToast('已将 ' + ids.length + ' 份文献加入作品组', 'success');
  } catch (e) { showToast(e.message || '加入失败', 'danger'); }
  finally { button.disabled = false; button.textContent = label; }
}

// source_file_id → 所属作品组标题（一个文献至多归一组）。用于选择器里标注「已在《X》」。
function groupMembershipMap() {
  var map = {};
  libraryStore.documentGroups.forEach(function(g) {
    (g.members || []).forEach(function(m) { map[m.source_file_id] = g.title; });
  });
  return map;
}

// 候选 = 尚未在本组、且命中搜索（书名 / 作者 / 文件名）的已导入文献。
function groupPickerCandidates(groupId, query) {
  var group = documentGroupById(groupId);
  var here = {};
  if (group) (group.members || []).forEach(function(m) { here[m.source_file_id] = true; });
  var q = (query || '').trim().toLowerCase();
  return libraryStore.sources.filter(function(src) {
    if (here[src.source_file_id]) return false;
    if (!q) return true;
    var hay = ((src.title || '') + ' ' + (src.author || '') + ' ' + (src.file_name || '')).toLowerCase();
    return hay.indexOf(q) >= 0;
  });
}

function toggleGroupPicker(groupId) {
  if (groupPicker.groupId === groupId) {
    groupPicker.groupId = '';
  } else {
    groupPicker.groupId = groupId;
    groupPicker.query = '';
    groupPicker.selected = {};
    groupPicker.focusPending = true;
  }
  renderDocumentGroupManager();
}

// 只更新列表容器，不整块重绘，搜索时不丢焦点。
function groupPickerInputAction(groupId, value) {
  if (groupPicker.groupId !== groupId) return;
  groupPicker.query = value || '';
  renderGroupPickerList(groupId);
}

function toggleGroupPickCandidate(sourceId, checked) {
  if (checked) groupPicker.selected[sourceId] = true;
  else delete groupPicker.selected[sourceId];
  var countEl = document.getElementById('grp-pick-count-' + groupPicker.groupId);
  if (countEl) countEl.textContent = groupPickSelectedCountLabel();
}

function groupPickSelectedCountLabel() {
  var n = Object.keys(groupPicker.selected).length;
  return n ? '已选 ' + n + ' 份' : '勾选要加入的文献';
}

function renderGroupPickerList(groupId) {
  var list = document.getElementById('grp-pick-list-' + groupId);
  if (!list) return;
  var membership = groupMembershipMap();
  var candidates = groupPickerCandidates(groupId, groupPicker.query);
  if (!candidates.length) {
    list.innerHTML = '<div class="grp-pick-empty">' + (groupPicker.query ? '没有命中的文献' : '所有已导入文献都已在本组') + '</div>';
  } else {
    list.innerHTML = candidates.map(function(src) {
      var sid = esc(src.source_file_id);
      var title = src.title || src.file_name || src.source_file_id;
      var language = libLangChipLabel(libraryLanguageCode(src));
      var format = documentGroupSourceLabel(src);
      var otherGroup = membership[src.source_file_id];
      var checked = groupPicker.selected[src.source_file_id] ? ' checked' : '';
      return '<label class="grp-pick-row">'
        + '<input type="checkbox"' + checked + ' onchange="toggleGroupPickCandidate(\'' + sid + '\', this.checked)">'
        + '<span class="grp-pick-main"><span class="grp-pick-title" title="' + esc(title) + '">' + esc(title) + '</span>'
        + '<span class="grp-pick-meta"><span>' + esc(language) + '</span><span>' + esc(format) + '</span>'
        + (otherGroup ? '<span class="grp-pick-moved">已在《' + esc(otherGroup) + '》，加入即移动</span>' : '')
        + '</span></span></label>';
    }).join('');
  }
  var countEl = document.getElementById('grp-pick-count-' + groupId);
  if (countEl) countEl.textContent = groupPickSelectedCountLabel();
}

async function addGroupPickedAction(groupId, button) {
  if (button.disabled) return;
  var ids = Object.keys(groupPicker.selected);
  if (!ids.length) { showToast('请先勾选要加入的文献', 'warning'); return; }
  button.disabled = true;
  button.textContent = '加入中…';
  try {
    for (var i = 0; i < ids.length; i += 1) {
      var response = await fetch('/api/document-groups/add-member', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({document_group_id: groupId, source_file_id: ids[i]})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '加入失败');
    }
    groupPicker.groupId = '';
    groupPicker.selected = {};
    groupPicker.query = '';
    await loadDocumentGroups();
    renderGroupScopeSelector();
    renderDocumentGroupManager();
    renderLibraryList();
    showToast('已将 ' + ids.length + ' 份文献加入作品组', 'success');
  } catch (e) {
    showToast(e.message || '加入失败', 'danger');
    button.disabled = false;
    button.textContent = '加入所选';
  }
}

// 映射区间、识别证据、PDF 剖面和收录作品只在详情抽屉里用，按 source_id 单份读取。
function ensureLibraryDetail(sourceId) {
  if (!sourceId || libraryStore.detailLoaded[sourceId]) return Promise.resolve();
  if (libraryStore.detailPending[sourceId]) return libraryStore.detailPending[sourceId];
  var request = fetch('/api/library/document?source_id=' + encodeURIComponent(sourceId)).then(function(response) {
    return response.json().then(function(data) {
      if (!response.ok || data.error) throw new Error(data.error || '文献详情读取失败');
      applyLibraryDetail(sourceId, data);
    });
  }).then(function() {
    delete libraryStore.detailPending[sourceId];
  }, function(error) {
    delete libraryStore.detailPending[sourceId];
    throw error;
  });
  libraryStore.detailPending[sourceId] = request;
  return request;
}

function applyLibraryDetail(sourceId, data) {
  var detail = data.item || {};
  // libraryStore.sources 与 searchStore.sourceFiles 指向同一个数组，就地替换让两处同时拿到完整记录。
  var index = libraryStore.sources.findIndex(function(item) { return item.source_file_id === sourceId; });
  if (index >= 0) libraryStore.sources[index] = Object.assign({}, libraryStore.sources[index], detail);
  if (data.volume && data.volume.source_file_id) {
    var volumeIndex = libraryStore.volumes.findIndex(function(item) { return item.source_file_id === sourceId; });
    if (volumeIndex >= 0) libraryStore.volumes[volumeIndex] = data.volume;
    else libraryStore.volumes.push(data.volume);
    libraryStore.volumeBySource.set(sourceId, data.volume);
  }
  var volumeId = data.volume ? data.volume.volume_id : null;
  if (volumeId) {
    libraryStore.works = libraryStore.works.filter(function(work) { return work.volume_id !== volumeId; }).concat(data.works || []);
  }
  libraryStore.detailLoaded[sourceId] = true;
}

function renderLibraryStats() {
  var container = document.getElementById('library-stats');
  if (!container) return;
  var current = {total:0,calibrated:0,page_pending:0,bibliographic:0};
  libraryGroupScopedSources().forEach(function(item) {
    if (item.source_type !== 'pdf') return;
    current.total += 1;
    var group = calibrationStatusGroup(item.status);
    if (group === 'calibrated') current.calibrated += 1;
    else current.page_pending += 1;
    if (bibliographicMissingFields(sourceBibliographicMetadata(item)).length > 0) current.bibliographic += 1;
  });
  // W1：拆成「待处理」行动组（重）+「参考量」组（轻），一眼看出现在该处理什么。
  container.innerHTML = '<div class="stat-group stat-group--pending"><span class="stat-group__label">待处理</span>'
    + statusStatButton('page_pending','页码待处理',current.page_pending,'warning','notice',libraryStore.statusFilter,'applyLibStatusFilter')
    + statusStatButton('bibliographic','书目待补全',current.bibliographic,'neutral','book',libraryStore.statusFilter,'applyLibStatusFilter')
    + '</div><span class="library-controls-spacer"></span>'
    + '<div class="stat-group stat-group--reference">'
    + statusStatButton('pdf_all','PDF 总数',current.total,'info','document',libraryStore.statusFilter,'applyLibStatusFilter')
    + statusStatButton('calibrated','已校准',current.calibrated,'success','check',libraryStore.statusFilter,'applyLibStatusFilter')
    + '</div>';
}

// 主流范式（Notion / Linear / Zotero）：三个筛选收进一个「筛选」按钮 + 弹层分面；
// 只有正在生效的筛选才作为可删 chip 露出来，按钮带数字角标。渲染由 renderLibraryList 触发。
function libDocTypeLabel(v) {
  return v === 'book' ? '著作' : v === 'journal_article' ? '期刊论文'
    : v === 'thesis' ? '学位论文' : v === 'unknown' ? '未识别' : '全部类型';
}

function libraryFileFacet(source) {
  if (source && source.source_type === 'pdf') return 'pdf';
  return sourceFormatLabel(source) === 'EPUB' ? 'epub' : 'word';
}

// 当前生效的（非「全部」）筛选，按「类型 → 语言 → 文件」次序，供角标与 chips 使用。
function libFilterActiveList() {
  var out = [];
  if (libraryStore.documentTypeFilter !== 'all') out.push({kind:'doctype', label:libDocTypeLabel(libraryStore.documentTypeFilter)});
  if (libraryStore.languageFilter !== 'all') out.push({kind:'lang', label:libLangChipLabel(libraryStore.languageFilter)});
  if (libraryStore.typeFilter !== 'all') out.push({kind:'type', label:libraryStore.typeFilter === 'word' ? 'Word' : libraryStore.typeFilter === 'epub' ? 'EPUB' : 'PDF'});
  return out;
}

function renderLibraryFilterBar() {
  var scopeSources = libraryGroupScopedSources();
  var allCount = scopeSources.length;
  var wordCount = scopeSources.filter(function(s){ return libraryFileFacet(s) === 'word'; }).length;
  var epubCount = scopeSources.filter(function(s){ return libraryFileFacet(s) === 'epub'; }).length;
  var pdfCount = scopeSources.filter(function(s){ return libraryFileFacet(s) === 'pdf'; }).length;
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
  var languageOptions = libraryLanguageFacetOptions(scopeSources, libraryStore.defaultLanguage);
  if (libraryStore.languageFilter !== 'all' && !languageOptions.some(function(option) { return option.v === libraryStore.languageFilter; })) libraryStore.languageFilter = 'all';
  var langOpts = [{v:'all', label:'全部语言', n:allCount}].concat(languageOptions);

  var typeOpts = [
    {v:'all', label:'全部', n:allCount},
    {v:'word', label:'Word', n:wordCount},
    {v:'epub', label:'EPUB', n:epubCount},
    {v:'pdf', label:'PDF', n:pdfCount}
  ];

  renderLibraryFacet('filter-opts-doctype', doctypeOpts, libraryStore.documentTypeFilter, 'doctype');
  renderLibraryFacet('filter-opts-lang', langOpts, libraryStore.languageFilter, 'lang');
  renderLibraryFacet('filter-opts-type', typeOpts, libraryStore.typeFilter, 'type');

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
  if (kind === 'doctype') libraryStore.documentTypeFilter = value;
  else if (kind === 'lang') libraryStore.languageFilter = value;
  else if (kind === 'type') {
    libraryStore.typeFilter = value;
    if (['word','epub'].indexOf(libraryStore.typeFilter) >= 0 && libraryStore.statusFilter !== 'all') { libraryStore.statusFilter = 'all'; renderLibraryStats(); }
  }
  closeLibDrawer();
  renderLibraryList();
}

// 移除单个生效筛选（点 chip 的 ✕），把该分面复位到「全部」。
async function removeLibFacet(event, kind) {
  if (event) event.stopPropagation();
  if (!await guardLeaveDetail()) return;
  if (kind === 'doctype') libraryStore.documentTypeFilter = 'all';
  else if (kind === 'lang') libraryStore.languageFilter = 'all';
  else if (kind === 'type') libraryStore.typeFilter = 'all';
  closeLibDrawer();
  renderLibraryList();
}

// 切换排序方向（升/降），合并排序控件里的方向按钮。
function toggleLibrarySortDirection() {
  libraryStore.sortDirection = libraryStore.sortDirection === 'asc' ? 'desc' : 'asc';
  try { localStorage.setItem('meFinderLibrarySortDirection', libraryStore.sortDirection); } catch (_) {}
  syncLibrarySortControls();
  closeAppSelects();
  renderLibraryList();
}

async function applyLibStatusFilter(status) {
  if (!await guardLeaveDetail()) return;
  var requested = status || 'all';
  libraryStore.statusFilter = requested === libraryStore.statusFilter ? 'all' : requested;
  if (libraryStore.statusFilter !== 'all' && ['word','epub'].indexOf(libraryStore.typeFilter) >= 0) {
    libraryStore.typeFilter = 'all';  // 筛选按钮/chips 由 renderLibraryFilterBar 随列表重绘刷新
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
  if (value === libraryStore.defaultLanguage) return;
  libraryStore.defaultLanguage = value;
  try { localStorage.setItem('meFinderLibDefaultLanguage', value); } catch (_) {}
  persistDisplayPreference('lib_default_language', value);  // 随数据备份/迁移（C-01）
  syncLibDefaultLanguageControl();
  renderLibraryList();  // 重绘以刷新语言筛选条的标签与「本国/外文」归属
}

function syncLibDefaultLanguageControl() {
  document.querySelectorAll('#lib-default-lang-control .seg-btn').forEach(function(b) {
    b.classList.toggle('active', b.dataset.deflang === libraryStore.defaultLanguage);
  });
}

// 清空所有筛选与搜索，回到全部（空态「清除全部筛选」、筛选弹层「清除全部」用）。
function clearLibraryFilters() {
  libraryStore.typeFilter = 'all';
  libraryStore.languageFilter = 'all';
  libraryStore.documentTypeFilter = 'all';
  libraryStore.statusFilter = 'all';
  libraryStore.groupScopeId = '';
  var search = document.getElementById('lib-search');
  if (search) search.value = '';
  renderGroupScopeSelector();
  renderLibraryStats();
  renderLibraryList();
}

function filterLibrary() {
  // 输入法与连续输入下，91 份以上的列表每敲一个字重排一次会明显发涩。
  if (libraryStore.filterTimer) clearTimeout(libraryStore.filterTimer);
  libraryStore.filterTimer = setTimeout(function() {
    libraryStore.filterTimer = null;
    renderLibraryList();
  }, 160);
}

function setLibraryView(mode) {
  libraryStore.viewMode = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem('meFinderLibraryView', libraryStore.viewMode);
  persistDisplayPreference('library_view', libraryStore.viewMode);
  syncLibraryViewButtons();
  renderLibraryList();
}

function syncLibraryViewButtons() {
  ['list','grid'].forEach(function(mode) {
    var button = document.getElementById('library-view-' + mode);
    if (!button) return;
    var active = libraryStore.viewMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setLibrarySortOption(event, control, value) {
  event.stopPropagation();
  if (control === 'direction') {
    libraryStore.sortDirection = value === 'asc' ? 'asc' : 'desc';
    localStorage.setItem('meFinderLibrarySortDirection', libraryStore.sortDirection);
  } else {
    libraryStore.sortField = ['imported_at','title','author','modified_at','source_type','status'].indexOf(value) >= 0 ? value : 'imported_at';
    localStorage.setItem('meFinderLibrarySortField', libraryStore.sortField);
  }
  syncLibrarySortControls();
  closeAppSelects();
  renderLibraryList();
}

function syncLibrarySortControls() {
  var labels = {imported_at:'导入时间',title:'书名',author:'作者',modified_at:'最近修改时间',source_type:'来源类型',status:'校准状态',desc:'降序',asc:'升序'};
  var fieldLabel = document.getElementById('library-sort-field-label');
  if (fieldLabel) fieldLabel.textContent = labels[libraryStore.sortField] || labels.imported_at;
  document.querySelectorAll('#library-sort-field-select .app-select-option').forEach(function(option) {
    option.classList.toggle('is-selected', option.dataset.value === libraryStore.sortField);
  });
  // 方向合并成一个可点按钮：文案随升/降切换，箭头方向靠 .is-asc 翻转。
  var dirBtn = document.getElementById('library-sort-dir');
  var dirLabel = document.getElementById('library-sort-dir-label');
  if (dirLabel) dirLabel.textContent = labels[libraryStore.sortDirection] || labels.desc;
  if (dirBtn) {
    dirBtn.classList.toggle('is-asc', libraryStore.sortDirection === 'asc');
    dirBtn.setAttribute('aria-label', libraryStore.sortDirection === 'asc' ? '升序，点击改为降序' : '降序，点击改为升序');
  }
}

function compareLibraryDates(a, b) {
  var av = Date.parse(a || '') || 0;
  var bv = Date.parse(b || '') || 0;
  if (!av && !bv) return 0;
  if (!av) return 1;
  if (!bv) return -1;
  return libraryStore.sortDirection === 'desc' ? bv - av : av - bv;
}

function libraryGroupScopedSources() {
  var sources = libraryStore.sources.slice();
  if (libraryStore.groupScopeId) {
    var groupMemberIds = documentGroupMemberIdSet(libraryStore.groupScopeId);
    sources = sources.filter(function(s) { return groupMemberIds.has(s.source_file_id); });
  }
  return sources;
}

async function generateTextAlignmentAction(groupId, pivotSourceId, targetSourceId, button) {
  var group = documentGroupById(groupId);
  if (!group || !pivotSourceId || !targetSourceId) {
    showToast('请选择两个要对照的版本', 'warning');
    return;
  }
  if (pivotSourceId === targetSourceId) {
    showToast('请选择两个不同版本', 'warning');
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = '对齐中…';
  }
  try {
    var response = await fetch('/api/text-alignments/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        document_group_id: groupId,
        pivot_source_file_id: pivotSourceId,
        target_source_file_id: targetSourceId
      })
    });
    var data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || '自动对齐失败');
    await loadDocumentGroups();
    renderGroupScopeSelector();
    renderDocumentGroupManager();
    var result = data.result || {};
    var rejected = Number(result.rejected_link_count || 0);
    var unmatched = Number(result.unmatched_link_count || 0);
    showToast(
      '对齐完成：' + Number(result.accepted_link_count || 0) + ' 组可定位'
        + (rejected ? '，' + rejected + ' 组低置信度已拒绝' : '')
        + (unmatched ? '，' + unmatched + ' 组为单版附加内容' : ''),
      'success'
    );
  } catch (e) {
    renderDocumentGroupManager();
    showToast(e.message || '自动对齐失败', 'danger');
  }
}

function getFilteredSources() {
  // 作品组 scope：只保留成员，再照常走类型/语言/状态/搜索/排序。
  let sources = libraryGroupScopedSources();
  if (libraryStore.typeFilter !== 'all') {
    sources = sources.filter(function(source) { return libraryFileFacet(source) === libraryStore.typeFilter; });
  }
  if (libraryStore.languageFilter !== 'all') {
    sources = sources.filter(s => libraryLanguageCode(s) === libraryStore.languageFilter);
  }
  if (libraryStore.documentTypeFilter === 'unknown') {
    // 未识别：从未跑过书目识别的 PDF（类型只是默认回落成 book，并非真的判定过）。
    sources = sources.filter(s => s.source_type === 'pdf' && !isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)));
  } else if (libraryStore.documentTypeFilter === 'book') {
    // 著作：仅已确认类型的图书 PDF；不再把 Word 文集和未识别 PDF 混进来。
    sources = sources.filter(s => s.source_type === 'pdf' && isBibliographicTypeConfirmed(sourceBibliographicMetadata(s)) && libraryDocType(s) === 'book');
  } else if (libraryStore.documentTypeFilter !== 'all') {
    sources = sources.filter(s => libraryDocType(s) === libraryStore.documentTypeFilter);
  }
  if (libraryStore.statusFilter === 'pdf_all') {
    sources = sources.filter(s => s.source_type === 'pdf');
  } else if (libraryStore.statusFilter === 'page_pending') {
    sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) !== 'calibrated');
  } else if (libraryStore.statusFilter === 'bibliographic') {
    sources = sources.filter(s => s.source_type === 'pdf' && bibliographicMissingFields(sourceBibliographicMetadata(s)).length > 0);
  } else if (libraryStore.statusFilter !== 'all') {
    sources = sources.filter(s => s.source_type === 'pdf' && calibrationStatusGroup(s.status) === libraryStore.statusFilter);
  }
  const q = (document.getElementById('lib-search').value || '').trim().toLowerCase().replace(/\s+/g, '');
  if (q) {
    sources = sources.filter(s => {
      const haystack = [s.title, s.author, s.translator, s.publisher, s.file_name]
        .map(function(value) { return String(value || '').toLowerCase().replace(/\s+/g, ''); })
        .join('|');
      return haystack.indexOf(q) >= 0;
    });
  }
  sources.sort(function(a, b) {
    var left = librarySortProjection(a);
    var right = librarySortProjection(b);
    var result;
    if (libraryStore.sortField === 'imported_at' || libraryStore.sortField === 'modified_at') {
      result = compareLibraryDates(left[libraryStore.sortField], right[libraryStore.sortField]);
    } else if (libraryStore.sortField === 'status') {
      var order = {manual_mapped:0,auto_mapped_high:1,unmapped:2,needs_review:3,auto_mapping_failed:4,source_missing:5,mapping:6};
      var av = a.source_type === 'pdf' && order[a.status] != null ? order[a.status] : 99;
      var bv = b.source_type === 'pdf' && order[b.status] != null ? order[b.status] : 99;
      result = libraryStore.sortDirection === 'desc' ? bv - av : av - bv;
    } else {
      result = calibrationSortText(left[libraryStore.sortField], right[libraryStore.sortField], libraryStore.sortDirection);
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
  var selectedCount = libraryStore.deleteSelection.size;
  var selectedPdfCount = libraryStore.sources.filter(function(item) {
    return item.source_type === 'pdf' && libraryStore.deleteSelection.has(item.source_file_id);
  }).length;
  var active = selectedCount > 0;
  // Selection alone drives the contextual action bar: no persistent mode toggle.
  if (page) page.classList.toggle('library-selecting', active);
  if (bar) bar.hidden = !active;
  if (count) count.textContent = '已选 ' + selectedCount + ' 项';
  if (removeButton) removeButton.disabled = selectedCount === 0;
  if (exportButton) {
    exportButton.disabled = libraryStore.exportRunning || selectedPdfCount === 0;
    if (!libraryStore.exportRunning) {
      exportButton.textContent = selectedPdfCount
        ? '导出所选 PDF（' + selectedPdfCount + '）'
        : '导出所选 PDF';
    }
  }
  if (selectVisibleButton) {
    var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
    var allSelected = selectable.length > 0 && selectable.every(function(item) {
      return libraryStore.deleteSelection.has(item.source_file_id);
    });
    selectVisibleButton.textContent = allSelected ? '取消全选' : '全选当前';
    selectVisibleButton.disabled = selectable.length === 0;
  }
}

function syncLibraryDeleteSelectionUI() {
  document.querySelectorAll('#library-list .library-entry').forEach(function(entry) {
    var selected = libraryStore.deleteSelection.has(entry.dataset.id);
    entry.classList.toggle('delete-selected', selected);
    entry.setAttribute('aria-selected', selected ? 'true' : 'false');
    var input = entry.querySelector('.library-delete-check');
    if (input) input.checked = selected;
  });
  updateLibraryDeleteControls();
}

function clearLibrarySelection() {
  if (libraryStore.deleteSelection.size === 0) return;
  libraryStore.deleteSelection.clear();
  syncLibraryDeleteSelectionUI();
}

function toggleLibraryDeleteSelection(sourceId, force) {
  var source = libraryStore.sources.find(function(item) { return item.source_file_id === sourceId; });
  if (!isLibraryDeleteSelectable(source)) {
    showToast('当前来源类型暂不支持从文献库移除', 'warning');
    return;
  }
  var selected = typeof force === 'boolean' ? force : !libraryStore.deleteSelection.has(sourceId);
  if (selected) libraryStore.deleteSelection.add(sourceId);
  else libraryStore.deleteSelection.delete(sourceId);
  syncLibraryDeleteSelectionUI();
}

function toggleSelectVisibleLibraryDocuments() {
  var selectable = getFilteredSources().filter(isLibraryDeleteSelectable);
  var allSelected = selectable.length > 0 && selectable.every(function(item) {
    return libraryStore.deleteSelection.has(item.source_file_id);
  });
  selectable.forEach(function(item) {
    if (allSelected) libraryStore.deleteSelection.delete(item.source_file_id);
    else libraryStore.deleteSelection.add(item.source_file_id);
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

function handleLibraryEntryClick(event, sourceId) {
  if (libraryStore.suppressSelectionClick) {
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
  listEl.className = 'library-list-container library-view-' + libraryStore.viewMode;
  libraryStore.deleteSelection.forEach(function(sourceId) {
    if (!libraryStore.sources.some(function(source) { return source.source_file_id === sourceId; })) {
      libraryStore.deleteSelection.delete(sourceId);
    }
  });
  // 筛选按钮角标 + 生效 chips + 弹层三组分面（含实时计数），一处渲染。
  renderLibraryFilterBar();

  libraryStore.renderToken += 1;
  if (sources.length === 0) {
    // 三态空状态：库为空 → 引导导入；有数据但筛选无果 → 清除筛选（L-13）。
    listEl.innerHTML = libraryStore.sources.length === 0
      ? '<div class="empty-state" style="min-height:220px"><div class="empty-state-text">文献库还是空的</div><div class="empty-state-hint">导入 PDF、DOCX 或 EPUB 后即可检索、核对页码与上下文</div><button class="action-btn primary" style="margin-top:14px" onclick="navigateTo(\'import\')">去导入文献</button></div>'
      : '<div class="empty-state" style="min-height:220px"><div class="empty-state-text">当前筛选没有匹配文献</div><div class="empty-state-hint">换个筛选条件，或清除全部筛选</div><button class="action-btn" style="margin-top:14px" onclick="clearLibraryFilters()">清除全部筛选</button></div>';
    updateLibraryDeleteControls();
    return;
  }
  // 首批同步渲染，其余按帧追加，避免大文献库一次性构建整张列表阻塞首屏。
  listEl.innerHTML = sources.slice(0, LIBRARY_RENDER_BATCH).map(libraryEntryHTML).join('');
  syncLibraryDeleteSelectionUI();
  if (sources.length > LIBRARY_RENDER_BATCH) {
    appendLibraryEntries(sources, LIBRARY_RENDER_BATCH, libraryStore.renderToken);
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
    if (token !== libraryStore.renderToken) return;
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
  var title = src.title || (src.file_name || src.source_file_id);
  var author = src.author || '作者信息待完善';
  var thesisIcon = src.document_type === 'thesis'
    ? '<svg class="doc-thesis-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-label="学位论文"><title>学位论文</title><path d="M22 10 12 5 2 10l10 5 10-5Z"/><path d="M6 12v5c0 1 2.7 2.5 6 2.5s6-1.5 6-2.5v-5"/><path d="M22 10v5"/></svg>'
    : '';
  var bib = sourceBibliographicMetadata(src);
  var missingMetadataText = isPdf ? bibliographicMissingText(bib) : '';
  var size = formatFileSize(src.size_bytes);
  var isSelected = src.source_file_id === libraryStore.selectedId;
  var isDeleteSelectable = isLibraryDeleteSelectable(src);
  var isDeleteSelected = libraryStore.deleteSelection.has(src.source_file_id);
  var typeCls = isPdf ? (src.parser_label === 'MinerU' ? 'mineru' : 'pdf') : 'word';
  var typeLabel = isPdf ? (src.parser_label || 'PDF') : sourceFormatLabel(src);
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
  if (libraryStore.viewMode === 'grid') {
    var imported = formatCalDate(src.imported_at || src.last_modified);
    var secondary = !isPdf ? ((vol && vol.corpus_title) || '') : '';
    return '<article class="library-card library-entry' + (isSelected ? ' selected' : '') + (isDeleteSelected ? ' delete-selected' : '') + '" tabindex="0" role="option" data-id="' + esc(src.source_file_id) + '" data-delete-selectable="' + (isDeleteSelectable ? '1' : '0') + '" aria-selected="' + (isDeleteSelected ? 'true' : 'false') + '" onclick="handleLibraryEntryClick(event,\'' + esc(src.source_file_id) + '\')">'
      + '<div class="library-card-top"><div class="library-card-badges"><span class="type-badge ' + typeCls + '">' + typeLabel + '</span>' + statusChip + (wordStructure ? '<span class="library-card-status">' + esc(wordStructure) + '</span>' : '') + (secondary ? '<span class="library-card-status">' + esc(secondary) + '</span>' : '') + '</div>' + selectionControl + '</div>'
      + '<div class="library-card-title">' + thesisIcon + esc(title) + '</div><div class="library-card-author">' + esc(author) + '</div>'
      + (missingMetadataText ? bibliographicMissingBadge(bib) : '')
      + '<div class="library-card-meta">' + esc(countMeta + ' · ' + size) + '</div>'
      + '<div class="library-card-mapping">' + esc(isPdf ? (src.mapping_summary || '尚未建立引用页码映射') : ((vol && vol.version_info) || typeLabel + ' 文献')) + '</div>'
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
  info += drawerInfoRow('文件类型', sourceFormatLabel(src) + ' 文档');
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
    items += '<button class="bib-menu-item" type="button" role="menuitem" onclick="bibCloseMenus();exportLibraryDocumentEpub(\'' + sid + '\')">导出 EPUB</button>';
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
    include_source_pdf: settingsStore.currentDocumentExportMode === 'with_pdf'
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

async function exportLibraryDocumentEpub(sourceId) {
  if (!sourceId) return;
  try {
    var outputDirectory = await chooseDesktopExportDirectory();
    if (outputDirectory === null) return;
    showToast('正在导出 EPUB…');
    var data = await requestLibraryDocumentEpubExport(sourceId, outputDirectory);
    showToast('已导出 ' + Number(data.page_count || 0).toLocaleString()
      + ' 页到：' + data.path + '（' + formatFileSize(data.size_bytes) + '）');
  } catch (error) {
    showToast('导出 EPUB 失败：' + (error && error.message ? error.message : '未知错误'), 'danger');
  }
}

async function requestLibraryDocumentEpubExport(sourceId, outputDirectory) {
  var payload = {source_id: sourceId};
  if (outputDirectory) payload.output_dir = outputDirectory;
  var response = await fetch('/api/document/export-epub', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  var data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || '导出失败');
  return data;
}

async function exportSelectedLibraryDocuments() {
  if (libraryStore.exportRunning) return;
  var items = libraryStore.sources.filter(function(item) {
    return item.source_type === 'pdf' && libraryStore.deleteSelection.has(item.source_file_id);
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

  libraryStore.exportRunning = true;
  var exportButton = document.getElementById('library-export-selected-btn');
  var exported = [];
  var failures = [];
  var skippedWordCount = libraryStore.deleteSelection.size - items.length;
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
    libraryStore.exportRunning = false;
    updateLibraryDeleteControls();
  }

  var skippedText = skippedWordCount ? '；已跳过 ' + skippedWordCount + ' 份 Word / EPUB' : '';
  if (failures.length) {
    showToast('批量导出完成：成功 ' + exported.length + ' 本，失败 ' + failures.length + ' 本'
      + skippedText + '。首个失败：' + failures[0].title + '：' + failures[0].message, 'warning');
    return;
  }
  outputDirectory = outputDirectory || exported[0].path.replace(/[\\/][^\\/]+$/, '');
  showToast('已导出 ' + exported.length + ' 个文档包'
    + (settingsStore.currentDocumentExportMode === 'with_pdf' ? '（包含原 PDF）' : '') + skippedText
    + '，每本一个文档包。保存到：' + outputDirectory, 'success');
}

async function selectLibDoc(sourceId) {
  // 切到别的文献前拦一道未保存修改；同一文献的重选（识别/保存后刷新）不打扰。
  var switchingDoc = sourceId !== libraryStore.selectedId;
  if (switchingDoc && !await guardLeaveDetail()) return;
  if (switchingDoc) bibEditMode[sourceId] = false;  // 新文献默认查看态
  libraryStore.selectedId = sourceId;
  document.querySelectorAll('#library-list .library-entry').forEach(function(row) {
    row.classList.toggle('selected', row.dataset.id === sourceId);
  });
  if (!libraryStore.sources.some(function(s) { return s.source_file_id === sourceId; })) return;
  try {
    await ensureLibraryDetail(sourceId);
  } catch (error) {
    showToast(error && error.message ? error.message : '文献详情读取失败', 'danger');
    return;
  }
  // 详情是异步补齐的，期间用户可能已经关掉抽屉或换了一份文献。
  if (libraryStore.selectedId !== sourceId) return;
  var src = libraryStore.sources.find(function(s) { return s.source_file_id === sourceId; });
  if (!src) return;
  var vol = volumeForSource(sourceId);
  var works = vol ? libraryStore.works.filter(function(w) { return w.volume_id === vol.volume_id; }) : [];
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
    + '<span class="detail-pill">' + sourceFormatLabel(src) + '</span>'
    + (vol && vol.primary_structure ? '<span class="detail-pill">' + structureLabel(vol.primary_structure) + '</span>' : '')
    + drawerStatusPill(src)
    + '</div>'
    + bibliographicHTML;

  var extra = document.getElementById('library-drawer-extra');
  if (extra) extra.innerHTML = drawerWorksHTML(works) + drawerFileInfoHTML(src, vol) + drawerMainActionsHTML(src);

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
  if (open && libraryStore.selectedId && calSelectedSourceId !== libraryStore.selectedId) {
    calSelectedSourceId = libraryStore.selectedId;
    await loadCalibrationDoc(libraryStore.selectedId);
  }
}

function updateLibraryEntry(sourceId) {
  renderLibraryStats();
  renderLibraryList();
  if (libraryStore.selectedId === sourceId) {
    var src = libraryStore.sources.find(function(s) { return s.source_file_id === sourceId; });
    if (src && src.source_type === 'pdf') renderDrawerCalibrationSummary(src);
  }
}
