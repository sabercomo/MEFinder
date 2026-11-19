(function (global) {  // module: 32-materials.js
  'use strict';

  const materials = global.MEFinderMaterials;
  const {node, button, load, post, openDialog, displayPath, tagName, parentPath, tagTree, tagsByExcerpt} = materials;
  const host = document.getElementById('page-materials');
  const side = document.getElementById('materials-tags');
  const list = document.getElementById('materials-excerpts');
  const detail = document.getElementById('materials-detail');
  const filter = document.getElementById('materials-filter');
  const exportButton = document.getElementById('materials-export');
  const expanded = new Set();
  let snapshot = null;
  let active = '';
  let selected = null;
  let visible = [];
  let location = null;
  let locating = false;
  let request = 0;
  let loadRequest = 0;

  const notice = (message, type = 'success') => showToast(message, type);
  const report = message => notice(message, 'danger');

  function pageLabel(excerpt) {
    if (!excerpt.citation_start) return '页码未核定';
    const range = excerpt.citation_end && excerpt.citation_start !== excerpt.citation_end
      ? excerpt.citation_start + '–' + excerpt.citation_end : excerpt.citation_start;
    return '第 ' + range + ' 页';
  }

  function citation(excerpt) {
    return [excerpt.author, '《' + excerpt.title + '》', pageLabel(excerpt)].filter(Boolean).join(' · ');
  }

  function reveal(path) {
    while ((path = parentPath(path))) expanded.add(path);
  }

  function createTag(parent) {
    openDialog({parent, onSaved: ({path}) => {
      choose(path);
      notice('已创建标签');
    }, onError: report});
  }

  function choose(path) {
    active = path;
    selected = null;
    location = null;
    locating = false;
    request++;
    filter.value = '';
    reveal(path);
    render();
  }

  function tagLine(path, total, children) {
    const line = node('div', 'materials-tag-line');
    line.classList.toggle('active', active === path);
    if (children) {
      const toggle = button(expanded.has(path) ? '▾' : '▸', 'materials-tree-control');
      toggle.setAttribute('aria-expanded', String(expanded.has(path)));
      toggle.setAttribute('aria-label', (expanded.has(path) ? '收起 ' : '展开 ') + displayPath(path));
      toggle.dataset.togglePath = path;
      toggle.addEventListener('click', () => {
        if (expanded.has(path)) expanded.delete(path);
        else expanded.add(path);
        render();
        Array.from(side.querySelectorAll('[data-toggle-path]')).find(el => el.dataset.togglePath === path).focus();
      });
      line.appendChild(toggle);
    } else if (path) {
      line.appendChild(node('span', 'materials-tree-indent'));
    }
    const name = button(path ? tagName(path) : '全部语料', 'materials-tree-tag');
    name.dataset.tagPath = path;
    name.title = path ? displayPath(path) : '全部语料';
    name.setAttribute('aria-pressed', String(active === path));
    name.appendChild(node('span', 'materials-count', total));
    name.addEventListener('click', () => {
      choose(path);
      Array.from(side.querySelectorAll('[data-tag-path]')).find(el => el.dataset.tagPath === path).focus();
    });
    line.appendChild(name);
    if (path && path === active && path.split('/').length < 3) {
      const add = button('＋', 'materials-tree-control');
      add.setAttribute('aria-label', '在 ' + displayPath(path) + ' 下新建子标签');
      add.addEventListener('click', () => createTag(path));
      line.appendChild(add);
    }
    return line;
  }

  function render() {
    if (!snapshot) return;
    const scroll = side.scrollTop;
    const byExcerpt = tagsByExcerpt(snapshot);
    const counts = new Map();
    const tags = snapshot.material_tags.map(row => row.path);
    const tree = tagTree(tags);
    const items = snapshot.material_excerpts.slice().sort((a, b) => b.excerpt_id - a.excerpt_id);
    const itemsWithTags = items.map(excerpt => {
      const direct = byExcerpt.get(excerpt.excerpt_id);
      const includes = new Set();
      direct.forEach(path => {
        while (path) {
          includes.add(path);
          path = parentPath(path);
        }
      });
      includes.forEach(path => counts.set(path, (counts.get(path) || 0) + 1));
      return {...excerpt, tags: direct, includes};
    });
    function branch(path) {
      const result = node('ul', 'materials-tag-branch');
      for (const child of tree.get(path) || []) {
        const item = node('li');
        item.appendChild(tagLine(child, counts.get(child) || 0, tree.has(child)));
        if (expanded.has(child)) item.appendChild(branch(child));
        result.appendChild(item);
      }
      return result;
    }
    side.replaceChildren(tagLine('', items.length, false), branch(''));
    side.scrollTop = scroll;
    const query = filter.value.trim().toLowerCase();
    visible = itemsWithTags.filter(excerpt => (!active || excerpt.includes.has(active))
      && [excerpt.title, excerpt.text, excerpt.author, excerpt.note].some(text => text.toLowerCase().includes(query)));
    document.getElementById('materials-active-tag').textContent = active ? displayPath(active) : '全部语料';
    document.getElementById('materials-count').textContent = visible.length + ' 条' + (tree.has(active) && active ? ' · 含子标签' : '');
    exportButton.disabled = !visible.length;
    list.replaceChildren();
    for (const excerpt of visible) {
      const row = node('article', 'materials-excerpt-row');
      row.classList.toggle('selected', excerpt.excerpt_id === selected);
      const rowTitle = button(excerpt.title, 'materials-excerpt-title');
      rowTitle.addEventListener('click', () => selectExcerpt(excerpt.excerpt_id));
      const heading = node('div', 'materials-excerpt-heading');
      heading.append(rowTitle, node('span', 'materials-page-label', pageLabel(excerpt)));
      const text = node('p', 'materials-text-preview', excerpt.text);
      text.addEventListener('click', () => selectExcerpt(excerpt.excerpt_id));
      const meta = node('div', 'materials-excerpt-meta');
      if (excerpt.author) meta.appendChild(node('span', '', excerpt.author));
      if (excerpt.note) meta.appendChild(node('span', 'materials-note-preview', excerpt.note));
      meta.appendChild(chips(excerpt.tags));
      row.append(heading, text, meta);
      list.appendChild(row);
    }
    if (!visible.length) {
      list.appendChild(node('p', 'materials-empty', query ? '没有符合筛选条件的语料'
        : active ? '此标签下暂无语料' : '在检索结果或阅读器中划选原文，再点「加标签」'));
    }
    renderDetail(visible.find(excerpt => excerpt.excerpt_id === selected));
  }

  function chips(paths) {
    const group = node('div', 'materials-chips');
    paths.forEach(path => {
      const chip = button(displayPath(path), 'materials-chip');
      chip.title = displayPath(path);
      chip.addEventListener('click', () => openTag(path));
      group.appendChild(chip);
    });
    return group;
  }

  function renderDetail(excerpt) {
    detail.replaceChildren();
    detail.hidden = !excerpt;
    if (!excerpt) return;
    detail.append(node('h3', '', excerpt.title), node('p', 'materials-secondary', citation(excerpt)));
    if (locating) detail.appendChild(node('p', 'materials-secondary', '正在核对出处…'));
    const warning = location?.status === 'source_deleted' ? '原书已删除'
      : location?.status === 'text_changed' ? '原文已变，无法确认当前位置'
        : location?.error;
    if (warning) detail.appendChild(node('p', 'materials-error', warning + ' · 保留收集时的出处'));
    detail.append(node('blockquote', 'materials-quote', excerpt.text), node('h4', '', '标签'), chips(excerpt.tags));
    detail.append(node('h4', '', '备注'),
      node('p', 'materials-note', excerpt.note || '暂无备注'));
    const actions = node('div', 'materials-form-actions');
    const copy = button('复制出处');
    copy.addEventListener('click', () => navigator.clipboard.writeText(citation(excerpt))
      .then(() => notice('已复制出处'), failure => report(failure.message)));
    const edit = button('编辑标签');
    edit.addEventListener('click', () => openDialog({excerpt, onSaved: () => notice('已保存标签'), onError: report}));
    const note = button('编辑备注');
    note.addEventListener('click', () => editNote(excerpt));
    const open = button('打开原文', 'action-btn primary');
    open.disabled = locating || !location?.reader;
    open.addEventListener('click', async () => {
      try {
        const resolved = await post({action: 'locate', excerpt_id: excerpt.excerpt_id});
        if (selected !== excerpt.excerpt_id) return;
        location = resolved;
        Object.assign(snapshot.material_excerpts.find(row => row.excerpt_id === selected), resolved.excerpt);
        render();
        if (resolved.reader) await global.MEFinderReader.open({...resolved.reader, returnLabel: '素材库'});
      } catch (failure) { report(failure.message); }
    });
    actions.append(copy, edit, note, open);
    detail.appendChild(actions);
  }

  function editNote(excerpt) {
    const dialog = node('dialog', 'materials-dialog');
    dialog.setAttribute('aria-labelledby', 'material-note-title');
    const form = node('form', 'materials-form');
    const title = node('h2', '', '编辑备注');
    title.id = 'material-note-title';
    const label = node('label', '', '备注');
    const input = node('textarea', 'materials-input');
    input.id = 'material-note';
    input.rows = 4;
    input.maxLength = 10000;
    input.value = excerpt.note;
    label.htmlFor = input.id;
    const actions = node('div', 'materials-form-actions');
    const cancel = button('取消');
    const save = button('保存备注', 'action-btn primary');
    save.type = 'submit';
    const error = node('p', 'materials-error');
    error.hidden = true;
    error.setAttribute('role', 'status');
    actions.append(cancel, save);
    form.append(title, label, input, error, actions);
    dialog.appendChild(form);
    document.body.appendChild(dialog);
    cancel.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => dialog.remove(), {once: true});
    dialog.addEventListener('cancel', event => { if (save.disabled) event.preventDefault(); });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      save.disabled = cancel.disabled = true;
      try {
        await post({action: 'edit', excerpt_id: excerpt.excerpt_id, note: input.value});
        dialog.close();
        global.dispatchEvent(new Event('mefinder-materials-change'));
        notice('备注已保存');
      } catch (failure) {
        error.hidden = false;
        error.textContent = failure.message;
        input.focus();
      } finally {
        save.disabled = cancel.disabled = false;
      }
    });
    dialog.showModal();
    input.focus();
  }

  async function selectExcerpt(id) {
    const sequence = ++request;
    selected = id;
    location = null;
    locating = true;
    render();
    try {
      const result = await post({action: 'locate', excerpt_id: id});
      if (sequence !== request) return;
      location = result;
      Object.assign(snapshot.material_excerpts.find(row => row.excerpt_id === id), result.excerpt);
    } catch (failure) {
      if (sequence !== request) return;
      location = {error: failure.message};
    } finally {
      if (sequence === request) {
        locating = false;
        render();
        if (matchMedia('(max-width: 940px)').matches) {
          detail.scrollIntoView({block: 'nearest'});
        }
      }
    }
  }

  async function refresh() {
    const sequence = ++loadRequest;
    const status = document.getElementById('materials-status');
    status.hidden = false;
    status.textContent = '正在读取素材库…';
    try {
      const found = await load();
      if (sequence !== loadRequest) return;
      snapshot = found;
      if (active && !found.material_tags.some(row => row.path === active)) active = '';
      status.hidden = true;
      render();
    } catch (failure) {
      if (sequence !== loadRequest) return;
      status.textContent = failure.message;
    }
  }

  function openTag(path) {
    choose(path);
    if (!host.classList.contains('active')) navigateTo('materials');
  }

  async function exportMarkdown() {
    const payload = {
      action: 'export', heading: active ? displayPath(active) : '素材库',
      excerpt_ids: visible.map(excerpt => excerpt.excerpt_id)
    };
    try {
      const directory = await chooseDesktopExportDirectory();
      if (directory === null) return;
      if (directory !== undefined) payload.output_dir = directory;
      const result = await post(payload);
      if (result.path) {
        notice('已导出到 ' + result.path);
        return;
      }
      const url = URL.createObjectURL(new Blob([result.markdown], {type: 'text/markdown;charset=utf-8'}));
      const link = node('a');
      link.href = url;
      link.download = 'MEFinder-素材库.md';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      report(failure.message);
    }
  }

  document.getElementById('materials-nav').addEventListener('click', () => navigateTo('materials'));
  document.getElementById('materials-new-tag').addEventListener('click', () => createTag(''));
  filter.addEventListener('input', render);
  exportButton.addEventListener('click', exportMarkdown);
  global.addEventListener('mefinder-materials-change', () => {
    if (host.classList.contains('active')) refresh();
  });
  global.MEFinder.materials = Object.freeze({refresh, openTag, chips});
}(window));
