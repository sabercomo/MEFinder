(function (global) {  // module: 32-materials.js
  'use strict';

  const materials = global.MEFinderMaterials;
  const {node, button, load, post, openDialog, displayPath, tagName, parentPath, tagTree, tagsByExcerpt, normalizePath, editRequest} = materials;
  const {appendPassage} = global.MEFinder;
  const host = document.getElementById('page-materials');
  const side = document.getElementById('materials-tags');
  const list = document.getElementById('materials-excerpts');
  const detail = document.getElementById('materials-detail');
  const filter = document.getElementById('materials-filter');
  const typeFilter = document.getElementById('materials-type');
  const typeTrigger = typeFilter.querySelector('.app-select-trigger');
  const imageImport = document.getElementById('materials-image-import');
  const layout = document.getElementById('materials-layout');
  const addImage = document.getElementById('materials-add-image');
  let selectedImage = '';
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

  function selectMaterialOption(picker, value, name) {
    picker.dataset.value = value;
    for (const option of picker.querySelectorAll('.app-select-option')) {
      const selected = option.dataset.value === value;
      option.classList.toggle('is-selected', selected);
      option.setAttribute('aria-selected', String(selected));
    }
    const trigger = picker.querySelector('.app-select-trigger');
    const title = picker.querySelector('.is-selected').textContent;
    trigger.querySelector('.app-select-value').textContent = title;
    trigger.setAttribute('aria-label', name + '：' + title);
  }

  function bindMaterialSelect(picker, onSelect) {
    const trigger = picker.querySelector('.app-select-trigger');
    const isOpen = () => picker.classList.contains('is-open');
    const close = () => {
      closeAppSelects();
      trigger.focus({preventScroll: true});
    };
    trigger.addEventListener('click', event => {
      if (isOpen()) close();
      else toggleAppSelect(event, picker.id);
    });
    trigger.addEventListener('keydown', event => {
      if (!trigger.disabled && !isOpen() && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
        event.preventDefault();
        toggleAppSelect(event, picker.id);
      }
    });
    picker.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || !isOpen()) return;
      event.preventDefault();
      event.stopPropagation();
      close();
    });
    picker.addEventListener('mousedown', event => {
      // Keep the focused option in WebKit until the click is handled.
      if (isOpen() && event.target.closest('button')) event.preventDefault();
    });
    picker.addEventListener('focusout', event => {
      if (isOpen() && !picker.contains(event.relatedTarget)) closeAppSelects();
    });
    picker.addEventListener('click', event => {
      const option = event.target.closest('.app-select-option');
      if (!option) return;
      close();
      if (option.dataset.value !== picker.dataset.value) onSelect(option.dataset.value);
    });
  }

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
    selectedImage = '';
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
    const imageTags = new Map(snapshot.material_images.map(image => [image.digest, []]));
    for (const {digest, path} of snapshot.material_image_tags) imageTags.get(digest).push(path);
    items.push(...snapshot.material_images);
    items.sort((a, b) => b.created_at.localeCompare(a.created_at));
    const itemsWithTags = items.map(excerpt => {
      const direct = excerpt.digest ? imageTags.get(excerpt.digest) : byExcerpt.get(excerpt.excerpt_id);
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
      && (typeFilter.dataset.value === 'all' || typeFilter.dataset.value === (excerpt.digest ? 'image' : 'text'))
      && [excerpt.title, excerpt.text, excerpt.author, excerpt.note].some(text => text?.toLowerCase().includes(query)));
    document.getElementById('materials-active-tag').textContent = active ? displayPath(active) : '全部语料';
    document.getElementById('materials-count').textContent = visible.length + ' 条' + (tree.has(active) && active ? ' · 含子标签' : '');
    exportButton.disabled = !visible.some(excerpt => !excerpt.digest);
    list.replaceChildren();
    for (const excerpt of visible) {
      const row = node('article', 'materials-excerpt-row');
      if (excerpt.digest) {
        row.classList.toggle('selected', excerpt.digest === selectedImage);
        const open = button(excerpt.title, 'materials-excerpt-title');
        open.prepend(makeImage(excerpt, 'materials-thumbnail'));
        open.addEventListener('click', () => {
          selectedImage = excerpt.digest;
          selected = null;
          request++;
          render();
          detail.scrollIntoView({block: 'nearest'});
        });
        row.append(open, node('p', 'materials-note-preview', excerpt.note), chips(excerpt.tags));
        list.appendChild(row);
        continue;
      }
      row.classList.toggle('selected', excerpt.excerpt_id === selected);
      const rowTitle = button(excerpt.title, 'materials-excerpt-title');
      rowTitle.addEventListener('click', () => selectExcerpt(excerpt.excerpt_id));
      const heading = node('div', 'materials-excerpt-heading');
      heading.append(rowTitle, node('span', 'materials-page-label', pageLabel(excerpt)));
      const text = node('p', 'materials-text-preview');
      appendPassage(text, excerpt.text);
      let pressAt;
      text.addEventListener('mousedown', ({clientX, clientY}) => { pressAt = [clientX, clientY]; });
      text.addEventListener('click', ({clientX, clientY}) => {
        const selection = global.getSelection();
        const dragged = pressAt && (pressAt[0] !== clientX || pressAt[1] !== clientY);
        if (dragged && !selection.isCollapsed && selection.containsNode(text, true)) return;
        selectExcerpt(excerpt.excerpt_id);
      });
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
    const image = visible.find(excerpt => excerpt.digest && excerpt.digest === selectedImage);
    if (image) renderImageDetail(image);
    else renderDetail(visible.find(excerpt => excerpt.excerpt_id === selected));
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
    const quote = node('blockquote', 'materials-quote');
    appendPassage(quote, excerpt.text);
    detail.append(quote, node('h4', '', '标签'), chips(excerpt.tags));
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
    const field = excerpt.digest ? '说明' : '备注';
    const title = node('h2', '', '编辑' + field);
    title.id = 'material-note-title';
    const label = node('label', '', field);
    const input = node('textarea', 'materials-input');
    input.id = 'material-note';
    input.rows = 4;
    input.maxLength = excerpt.digest ? 200 : 10000;
    input.value = excerpt.note;
    label.htmlFor = input.id;
    const actions = node('div', 'materials-form-actions');
    const cancel = button('取消');
    const save = button('保存' + field, 'action-btn primary');
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
        await post({...editRequest(excerpt), note: input.value});
        dialog.close();
        global.dispatchEvent(new Event('mefinder-materials-change'));
        notice(field + '已保存');
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
    selectedImage = '';
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

  function makeImage(image, className) {
    const element = node('img', className);
    element.alt = image.title;
    element.loading = 'lazy';
    element.src = '/api/materials?image=' + encodeURIComponent(image.digest);
    return element;
  }

  // Adapted from Magpie picture(src) (MIT, © 2026 yetone).
  function imageDetailPreview(image) {
    const viewport = node('div', 'materials-image-viewport');
    const toggle = button('', 'materials-image-toggle');
    toggle.disabled = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', '按原尺寸查看图片');
    const picture = makeImage(image, 'materials-image-detail');
    const showFailure = () => {
      const status = node('p', 'materials-secondary', '图片加载失败');
      status.setAttribute('role', 'status');
      viewport.replaceChildren(status);
    };
    picture.addEventListener('load', () => { toggle.disabled = false; });
    picture.addEventListener('error', showFailure, {once: true});
    toggle.addEventListener('click', () => {
      const expanded = viewport.classList.toggle('is-expanded');
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.setAttribute('aria-label', expanded ? '缩回图片预览' : '按原尺寸查看图片');
      if (!expanded) viewport.scrollTo(0, 0);
    });
    toggle.append(picture);
    viewport.append(toggle);
    return viewport;
  }

  function renderImageDetail(image) {
    detail.hidden = false;
    const origin = JSON.parse(image.origin);
    detail.replaceChildren(node('h3', '', image.title), imageDetailPreview(image));
    if (origin.source_id) {
      const open = button('打开原文');
      open.addEventListener('click', () => openSource(origin.source_id, origin.page));
      detail.append(node('p', 'materials-secondary', origin.citation_page
        ? '第 ' + origin.citation_page + ' 页' : 'PDF 第 ' + origin.page + ' 页 · 未校准'), open);
      const source = node('details');
      source.append(node('summary', '', '出处详情'),
        node('p', 'materials-secondary', 'PDF 第 ' + origin.page + ' 页'));
      detail.append(source);
    }
    detail.append(node('h4', '', '说明'), node('p', 'materials-note', image.note || '暂无说明'),
      node('h4', '', '标签'), chips(image.tags));
    const actions = node('div', 'materials-form-actions');
    const note = button('编辑说明');
    note.addEventListener('click', () => editNote(image));
    const tag = button('编辑标签');
    tag.addEventListener('click', () => openDialog({excerpt: image, onSaved: () => notice('已保存标签'), onError: report}));
    actions.append(note, tag);
    detail.append(actions);
  }

  function showImageImport(files) {
    layout.hidden = addImage.hidden = true;
    imageImport.hidden = false;
    const title = document.getElementById('materials-page-title');
    title.textContent = '添加图片';
    host.classList.add('image-workbench-open');

    const form = node('form', 'image-workbench');
    const header = node('header', 'image-workbench-header');
    const heading = node('h2', '', '添加图片');
    heading.tabIndex = -1;
    const back = button('取消');
    header.append(heading, back);

    function icon(pathData) {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS(svg.namespaceURI, 'path');
      path.setAttribute('d', pathData);
      svg.append(path);
      return svg;
    }

    const tabs = node('div', 'image-workbench-tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', '导入方式');
    const localTab = button('本地图片', 'image-workbench-tab');
    localTab.prepend(icon('M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1ZM3 17l5-5 4 4 3-3 6 6M16 9h.01'));
    const pdfTab = button('从 PDF 截取', 'image-workbench-tab');
    const documentIcon = 'M6 3h8l4 4v14H6zM14 3v5h4m-9 4h6m-6 4h6';
    pdfTab.prepend(icon(documentIcon));
    const modes = {local: localTab, pdf: pdfTab};
    for (const [key, tab] of Object.entries(modes)) {
      tab.id = 'image-workbench-tab-' + key;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', 'image-workbench-source');
      tab.addEventListener('click', () => setMode(key));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        setMode(event.key === 'Home' ? 'local' : event.key === 'End' ? 'pdf' : key === 'local' ? 'pdf' : 'local');
        modes[mode].focus();
      });
    }
    tabs.append(localTab, pdfTab);

    const source = node('section', 'image-workbench-source');
    source.id = 'image-workbench-source';
    source.setAttribute('role', 'tabpanel');
    const stage = node('div', 'image-workbench-stage');
    const input = node('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg';
    input.hidden = true;
    const empty = node('div', 'image-workbench-empty');
    const glyph = node('div', 'image-workbench-glyph');
    const filename = node('p', 'image-workbench-filename');
    const prompt = node('p', 'image-workbench-prompt');
    const formats = node('p', 'materials-secondary');
    const choose = button('选择图片');
    empty.append(glyph, prompt, formats, choose);
    const preview = node('img', 'image-workbench-image');
    preview.alt = '待保存的图片';
    preview.hidden = true;
    const selectionActions = node('div', 'image-workbench-image-actions');
    const recrop = button('裁剪');
    const replace = button('更换');
    const remove = button('移除');
    selectionActions.append(filename, recrop, replace, remove);
    stage.append(empty, preview, input);
    source.append(stage, selectionActions);

    const inspector = node('section', 'image-workbench-inspector');
    inspector.setAttribute('aria-label', '图片属性');
    const note = node('textarea', 'materials-input');
    note.rows = 4;
    note.maxLength = 200;
    note.placeholder = '描述图片内容，便于后续检索';
    const noteLabel = node('label');
    noteLabel.append(node('span', 'image-workbench-field-title', '图片说明'),
      node('span', 'image-workbench-optional', '（可选）'), note);

    const tagGroup = node('fieldset', 'image-workbench-tag-group');
    tagGroup.append(node('legend', 'image-workbench-field-title', '标签'));
    const tagSearch = node('input', 'materials-input');
    tagSearch.type = 'search';
    tagSearch.placeholder = '搜索或新建标签';
    tagSearch.setAttribute('aria-label', '搜索或新建标签');
    const tagOptions = node('div', 'image-workbench-tags');
    tagGroup.append(tagSearch, tagOptions);

    inspector.append(noteLabel, tagGroup);

    const footer = node('footer', 'image-workbench-footer');
    const status = node('p', 'image-workbench-status materials-secondary');
    status.setAttribute('role', 'status');
    const shortcut = node('p', 'image-workbench-shortcut materials-secondary', '支持粘贴图片');
    shortcut.append(node('kbd', '', /Mac/.test(navigator.platform) ? '⌘ V' : 'Ctrl V'));
    const footerInfo = node('div', 'image-workbench-footer-info');
    footerInfo.append(shortcut, status);
    const save = node('button', 'action-btn primary image-workbench-save', '保存到素材库');
    save.type = 'submit';
    footer.append(footerInfo, save);
    const body = node('div', 'image-workbench-body');
    body.append(source, inspector);
    form.append(header, tabs, body, footer);
    imageImport.replaceChildren(form);

    const pictures = {local: null, pdf: null};
    const known = new Set(['未整理', ...(snapshot?.material_tags || []).map(row => row.path)]);
    const selectedTags = new Set(active && active !== '未整理' ? [active] : []);
    let mode = 'local';
    let revision = 0;
    let reading = false;
    let saving = false;
    let statusText = '';
    let error = false;
    let tagLoadMessage = snapshot ? '' : '正在读取标签…';

    function setStatus(text = '', isError = false) {
      statusText = text;
      error = isError;
      renderStatus();
    }

    function renderStatus() {
      body.setAttribute('aria-busy', String(saving));
      status.textContent = statusText || (reading && mode === 'local' ? '正在读取图片…'
        : pictures[mode] ? '' : '选择图片后即可保存');
      status.classList.toggle('materials-error', error);
      save.disabled = saving || !pictures[mode] || (reading && mode === 'local');
      save.textContent = saving ? '正在保存…' : '保存到素材库';
    }

    function renderSource() {
      const picture = pictures[mode];
      const isPDF = mode === 'pdf';
      source.setAttribute('aria-labelledby', modes[mode].id);
      for (const [key, tab] of Object.entries(modes)) {
        tab.setAttribute('aria-selected', String(mode === key));
        tab.tabIndex = mode === key ? 0 : -1;
      }
      stage.dataset.mode = mode;
      stage.classList.toggle('has-image', Boolean(picture));
      empty.hidden = Boolean(picture);
      preview.hidden = selectionActions.hidden = !picture;
      recrop.hidden = !isPDF;
      if (picture) {
        preview.src = picture.url;
        filename.textContent = picture.name;
        filename.title = picture.name;
      } else {
        preview.removeAttribute('src');
        prompt.textContent = isPDF ? '从库内 PDF 选择页面' : '拖放图片到此处';
        formats.textContent = isPDF ? '可截取整页，也可框选局部' : 'PNG、JPG、JPEG · 最大 25 MB';
        choose.textContent = isPDF ? '选择 PDF' : '选择图片';
        glyph.replaceChildren(icon(isPDF ? documentIcon : 'M12 16V4m-5 5 5-5 5 5M4 16v4h16v-4'));
      }
      renderStatus();
    }

    function setMode(next) {
      mode = next;
      statusText = '';
      error = false;
      renderSource();
    }

    function renderTagOptions(focusPath = '') {
      const scrollTop = tagOptions.scrollTop;
      tagOptions.replaceChildren();
      const value = tagSearch.value.trim();
      const query = value.toLocaleLowerCase();
      const paths = [...new Set([...known, ...selectedTags])].filter(path => path !== '未整理'
        && (selectedTags.has(path) || path.toLocaleLowerCase().includes(query)
          || displayPath(path).toLocaleLowerCase().includes(query)))
        .sort((a, b) => a.localeCompare(b, 'zh-CN'));
      let focusedOption;
      for (const path of paths) {
        const checked = selectedTags.has(path);
        const option = button(displayPath(path), 'image-workbench-tag');
        option.setAttribute('aria-pressed', String(checked));
        if (checked) option.prepend(icon('m5 12 4 4 10-10'));
        option.addEventListener('click', () => {
          if (checked) selectedTags.delete(path);
          else selectedTags.add(path);
          renderTagOptions(path);
        });
        if (path === focusPath) focusedOption = option;
        tagOptions.append(option);
      }
      if (value) {
        try {
          const path = normalizePath(value, '');
          if (!known.has(path)) {
            const create = button('新建「' + displayPath(path) + '」', 'image-workbench-tag image-workbench-create-tag');
            create.addEventListener('click', () => {
              known.add(path);
              selectedTags.add(path);
              tagSearch.value = '';
              renderTagOptions();
              tagSearch.focus();
            });
            tagOptions.append(create);
          }
        } catch (failure) {
          tagOptions.append(node('p', 'materials-error', failure.message));
        }
      }
      const emptyMessage = value ? '没有匹配的标签' : tagLoadMessage;
      if (!tagOptions.childElementCount && emptyMessage) {
        tagOptions.append(node('p', 'materials-secondary', emptyMessage));
      }
      tagOptions.scrollTop = scrollTop;
      if (focusPath) (focusedOption || tagSearch).focus({preventScroll: true});
    }

    tagSearch.addEventListener('input', () => renderTagOptions());
    tagGroup.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'Escape' && tagSearch.value) {
        event.preventDefault();
        tagSearch.value = '';
        renderTagOptions();
        tagSearch.focus();
      } else if (event.target === tagSearch && event.key === 'Enter') {
        event.preventDefault();
        if (tagSearch.value.trim()) tagOptions.querySelector('button:not([aria-pressed="true"])')?.click();
      } else if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
        const items = [tagSearch, ...tagOptions.querySelectorAll('button')];
        const index = items.indexOf(document.activeElement);
        const adjacent = index + (event.key === 'ArrowDown' ? 1 : -1);
        if (index >= 0 && items[adjacent]) {
          event.preventDefault();
          items[adjacent].focus();
        }
      }
    });

    function updatePicture(key, picture) {
      if (pictures[key]?.file) URL.revokeObjectURL(pictures[key].url);
      pictures[key] = picture;
      if (mode === key) renderSource();
    }

    async function acceptFiles(incoming) {
      const file = incoming[0];
      if (!file || saving) return;
      const count = incoming.length;
      const attempt = ++revision;
      reading = false;
      setMode('local');
      if (!['image/png', 'image/jpeg'].includes(file.type)) {
        setStatus('请选择 PNG 或 JPEG 图片', true);
        return;
      }
      if (file.size > 25 * 1024 * 1024) {
        setStatus('单张图片不能超过 25 MB', true);
        return;
      }
      const url = URL.createObjectURL(file);
      const candidate = new Image();
      candidate.src = url;
      reading = true;
      renderStatus();
      try {
        await candidate.decode();
        if (revision !== attempt) return;
        updatePicture('local', {file, name: file.name, url});
        if (mode === 'local' && count > 1) setStatus('一次保存一张，已选第一张');
      } catch {
        if (revision === attempt && mode === 'local') setStatus('无法读取图片，请重新选择', true);
      } finally {
        if (pictures.local?.url !== url) URL.revokeObjectURL(url);
        if (revision === attempt) {
          reading = false;
          renderStatus();
        }
      }
    }

    function openPDF(initial = null) {
      showPDFCrop((payload, url, name) => {
        setMode('pdf');
        updatePicture('pdf', {payload, url, name});
      }, initial);
    }

    choose.addEventListener('click', () => mode === 'pdf' ? openPDF() : input.click());
    replace.addEventListener('click', () => mode === 'pdf' ? openPDF() : input.click());
    recrop.addEventListener('click', () => openPDF(pictures.pdf.payload));
    remove.addEventListener('click', () => {
      if (mode === 'local') {
        revision++;
        reading = false;
        input.value = '';
      }
      setStatus();
      updatePicture(mode, null);
      choose.focus();
    });
    input.addEventListener('change', () => {
      acceptFiles(input.files);
      input.value = '';
    });
    stage.addEventListener('dragover', event => {
      if (!event.dataTransfer.types.includes('Files') || saving) return;
      event.preventDefault();
      stage.classList.add('is-dragging');
    });
    stage.addEventListener('dragleave', event => {
      if (!stage.contains(event.relatedTarget)) stage.classList.remove('is-dragging');
    });
    stage.addEventListener('drop', event => {
      if (!event.dataTransfer.files.length || saving) return;
      event.preventDefault();
      stage.classList.remove('is-dragging');
      acceptFiles(event.dataTransfer.files);
    });
    const paste = event => acceptFiles(event.detail);
    imageImport.addEventListener('material-image-files', paste);

    function close() {
      revision++;
      if (pictures.local?.file) URL.revokeObjectURL(pictures.local.url);
      imageImport.removeEventListener('material-image-files', paste);
      imageImport.replaceChildren();
      imageImport.hidden = true;
      layout.hidden = addImage.hidden = false;
      host.classList.remove('image-workbench-open');
      title.textContent = '素材库';
      addImage.focus();
    }

    back.addEventListener('click', async () => {
      if ((pictures.local || pictures.pdf || note.value.trim()) &&
          !await showAppConfirm('放弃本次添加的图片和说明？', {
            title: '放弃添加图片？', confirmText: '放弃', tone: 'danger'
          })) return;
      close();
    });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      const picture = pictures[mode];
      if (!picture || save.disabled) return;
      saving = true;
      header.inert = tabs.inert = body.inert = true;
      setStatus('正在保存到素材库…');
      try {
        const payload = picture.file ? {
          title: picture.name,
          data: await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = () => reject(new Error('无法读取图片'));
            reader.readAsDataURL(picture.file);
          })
        } : picture.payload;
        const paths = selectedTags.size ? [...selectedTags] : ['未整理'];
        const result = await post({action: 'collect_image', ...payload, note: note.value, tags: paths});
        if (result.created) {
          filter.value = '';
          selectMaterialOption(typeFilter, 'all', '素材类型');
          if (active && !result.tags.some(path => path === active || path.startsWith(active + '/'))) {
            active = result.tags[0];
            reveal(active);
          }
          selectedImage = result.digest;
          selected = null;
          request++;
        }
        close();
        global.dispatchEvent(new Event('mefinder-materials-change'));
        notice(result.created ? '图片已保存 · ' + result.tags.map(displayPath).join('、') : '这张图片已在素材库中');
      } catch (failure) {
        saving = false;
        header.inert = tabs.inert = body.inert = false;
        setStatus('保存失败 · ' + failure.message, true);
        save.focus();
      }
    });

    renderTagOptions();
    if (!snapshot) {
      load().then(result => {
        if (!form.isConnected) return;
        for (const {path} of result.material_tags) known.add(path);
        tagLoadMessage = '';
        renderTagOptions();
      }, () => {
        if (!form.isConnected) return;
        tagLoadMessage = '暂时无法读取已有标签';
        renderTagOptions();
      });
    }
    renderSource();
    heading.focus({preventScroll: true});
    if (files?.length) acceptFiles(files);
  }

  async function showPDFCrop(onUse, initial = null) {
    const wholePage = [0, 0, 1, 1];
    const dialog = node('dialog', 'materials-dialog materials-pdf-crop');
    const heading = node('h2', '', '从 PDF 截图');
    heading.id = 'materials-pdf-title';
    dialog.setAttribute('aria-labelledby', heading.id);
    const select = node('div', 'app-select materials-pdf-select');
    select.id = 'materials-pdf-select';
    const trigger = button('', 'app-select-trigger');
    trigger.disabled = true;
    trigger.setAttribute('aria-label', 'PDF 文献');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', 'materials-pdf-options');
    trigger.append(node('span', 'app-select-value', '正在读取文献…'), typeTrigger.querySelector('svg').cloneNode(true));
    const options = node('div', 'app-select-menu');
    options.id = 'materials-pdf-options';
    options.setAttribute('role', 'listbox');
    options.setAttribute('aria-label', 'PDF 文献');
    select.append(trigger, options);
    const page = node('input', 'materials-input');
    page.type = 'number';
    page.min = '1';
    page.value = '1';
    page.setAttribute('aria-label', 'PDF 页码');
    const previousPage = button('上一页');
    const nextPage = button('下一页');
    const loadPage = button('预览');
    const pagination = node('div', 'materials-pdf-pagination');
    pagination.append(previousPage, page, nextPage, loadPage);
    const controls = node('div', 'materials-pdf-controls');
    controls.append(select, pagination);
    const canvas = node('canvas', 'materials-crop-canvas');
    canvas.tabIndex = 0;
    canvas.setAttribute('aria-label', 'PDF 页面预览。拖动框选，也可按回车创建选区');
    const stage = node('div', 'materials-pdf-stage');
    const paper = node('div', 'materials-pdf-paper');
    const selection = node('div', 'materials-crop-selection');
    paper.hidden = selection.hidden = true;
    selection.tabIndex = 0;
    selection.dataset.cropAction = 'move';
    selection.setAttribute('role', 'group');
    selection.setAttribute('aria-label', '裁剪选区，用方向键移动');
    for (const [action, label, x, y, cursor] of [
      ['nw', '左上角', '0%', '0%', 'nwse-resize'],
      ['n', '上边', '50%', '0%', 'ns-resize'],
      ['ne', '右上角', '100%', '0%', 'nesw-resize'],
      ['w', '左边', '0%', '50%', 'ew-resize'],
      ['e', '右边', '100%', '50%', 'ew-resize'],
      ['sw', '左下角', '0%', '100%', 'nesw-resize'],
      ['s', '下边', '50%', '100%', 'ns-resize'],
      ['se', '右下角', '100%', '100%', 'nwse-resize']
    ]) {
      const handle = button('', 'materials-crop-handle');
      handle.dataset.cropAction = action;
      handle.setAttribute('aria-label', '调整选区' + label);
      handle.style.setProperty('--handle-x', x);
      handle.style.setProperty('--handle-y', y);
      handle.style.setProperty('--handle-cursor', cursor);
      selection.append(handle);
    }
    paper.append(canvas, selection);
    stage.append(paper);
    const message = node('p', 'materials-secondary', '选择 PDF 后预览页面');
    message.setAttribute('role', 'status');
    const cancel = button('取消');
    const use = button('使用整页', 'action-btn materials-pdf-use');
    const clearSelection = button('清除选区', 'action-btn materials-pdf-clear');
    use.disabled = clearSelection.disabled = true;
    const actions = node('div', 'materials-form-actions');
    actions.append(clearSelection, cancel, use);
    dialog.append(heading, controls, message, stage, actions);
    document.body.append(dialog);
    let result, image, drag, box, draftBox;
    let pageCount = 0;
    let previewRequested = false;
    let generation = 0;
    let previewTimer;
    dialog.addEventListener('close', () => {
      clearTimeout(previewTimer);
      dialog.remove();
    }, {once: true});
    cancel.addEventListener('click', () => dialog.close());
    const context = canvas.getContext('2d');
    const syncPagination = () => {
      const number = page.valueAsNumber;
      const valid = pageCount && Number.isInteger(number) && number >= 1 && number <= pageCount;
      previousPage.disabled = !valid || number <= 1;
      nextPage.disabled = !valid || number >= pageCount;
    };
    const clear = (keepPaper = false) => {
      clearTimeout(previewTimer);
      generation++;
      result = box = drag = draftBox = null;
      delete message.dataset.error;
      stage.setAttribute('aria-busy', 'false');
      if (!keepPaper) {
        paper.hidden = true;
        image = null;
      }
      paper.inert = true;
      showSelection();
      syncPagination();
    };
    const describeSelection = () => {
      message.textContent = 'PDF 第 ' + result.page + ' / ' + pageCount
        + (box ? ' 页 · 拖动选框或手柄调整' : ' 页 · 拖动页面即可框选');
    };
    const draw = () => {
      context.drawImage(image, 0, 0);
      const region = draftBox || box;
      if (!region) return;
      const [left, top, right, bottom] = region;
      context.fillStyle = getComputedStyle(dialog).getPropertyValue('--dialog-backdrop').trim();
      context.beginPath();
      context.rect(0, 0, canvas.width, canvas.height);
      context.rect(left * canvas.width, top * canvas.height,
        (right - left) * canvas.width, (bottom - top) * canvas.height);
      context.fill('evenodd');
    };
    const syncPaperSize = () => {
      if (!image) return;
      const scale = Math.min(
        (stage.clientWidth - 24) / image.naturalWidth,
        (stage.clientHeight - 24) / image.naturalHeight
      );
      paper.style.setProperty('--preview-width', Math.max(0, image.naturalWidth * scale) + 'px');
      paper.style.setProperty('--preview-height', Math.max(0, image.naturalHeight * scale) + 'px');
    };
    const stageSize = new ResizeObserver(syncPaperSize);
    stageSize.observe(stage);
    dialog.addEventListener('close', () => stageSize.disconnect(), {once: true});

    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const position = event => {
      const bounds = canvas.getBoundingClientRect();
      return [(event.clientX - bounds.left) / bounds.width, (event.clientY - bounds.top) / bounds.height]
        .map(value => clamp(value, 0, 1));
    };
    const showSelection = () => {
      const region = draftBox || box;
      selection.hidden = !region;
      clearSelection.disabled = !box || Boolean(drag);
      use.disabled = !result || Boolean(drag);
      use.textContent = box ? '使用选区' : '使用整页';
      if (image) draw();
      if (!region) return;
      const [left, top, right, bottom] = region;
      Object.assign(selection.style, {
        left: left * 100 + '%', top: top * 100 + '%',
        width: (right - left) * 100 + '%', height: (bottom - top) * 100 + '%'
      });
    };
    const adjust = (region, action, dx, dy) => {
      const [left, top, right, bottom] = region;
      if (action === 'move') {
        const x = clamp(dx, -left, 1 - right);
        const y = clamp(dy, -top, 1 - bottom);
        return [left + x, top + y, right + x, bottom + y];
      }
      const minWidth = 2 / canvas.width;
      const minHeight = 2 / canvas.height;
      return [
        action.includes('w') ? clamp(left + dx, 0, right - minWidth) : left,
        action.includes('n') ? clamp(top + dy, 0, bottom - minHeight) : top,
        action.includes('e') ? clamp(right + dx, left + minWidth, 1) : right,
        action.includes('s') ? clamp(bottom + dy, top + minHeight, 1) : bottom
      ];
    };
    function moveSelection(event) {
      if (drag?.pointer !== event.pointerId) return;
      const [x, y] = position(event);
      const [startX, startY] = drag.anchor;
      draftBox = drag.action === 'draw'
        ? [Math.min(x, startX), Math.min(y, startY), Math.max(x, startX), Math.max(y, startY)]
        : adjust(drag.box, drag.action, x - startX, y - startY);
      showSelection();
    }
    paper.addEventListener('pointerdown', event => {
      if (!result || !event.isPrimary || event.button !== 0) return;
      const action = event.target.closest('[data-crop-action]')?.dataset.cropAction || 'draw';
      drag = {pointer: event.pointerId, anchor: position(event), action, box};
      event.preventDefault();
      paper.setPointerCapture(event.pointerId);
      moveSelection(event);
    });
    paper.addEventListener('pointermove', moveSelection);
    paper.addEventListener('pointerup', event => {
      if (drag?.pointer !== event.pointerId) return;
      moveSelection(event);
      const [left, top, right, bottom] = draftBox;
      if ((right - left) * canvas.width >= 2 && (bottom - top) * canvas.height >= 2) box = draftBox;
      finishSelection(event);
    });
    function finishSelection(event) {
      if (drag?.pointer !== event.pointerId) return;
      drag = draftBox = null;
      showSelection();
      describeSelection();
    }
    paper.addEventListener('pointercancel', finishSelection);
    paper.addEventListener('lostpointercapture', finishSelection);
    paper.addEventListener('keydown', event => {
      if (!result || drag) return;
      if (event.target === canvas && !box && event.key === 'Enter') {
        event.preventDefault();
        box = [.2, .2, .8, .8];
        showSelection();
        describeSelection();
        selection.focus();
        return;
      }
      const action = event.target.dataset.cropAction;
      const offset = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
      if (!box || !action || !offset) return;
      event.preventDefault();
      const step = event.shiftKey ? 10 : 1;
      box = adjust(box, action, offset[0] * step / canvas.clientWidth, offset[1] * step / canvas.clientHeight);
      showSelection();
    });
    clearSelection.addEventListener('click', () => {
      if (!result) return;
      box = null;
      showSelection();
      describeSelection();
      canvas.focus();
    });
    bindMaterialSelect(select, value => {
      selectMaterialOption(select, value, 'PDF 文献');
      page.value = '1';
      page.removeAttribute('max');
      pageCount = 0;
      previewRequested = false;
      clear();
      message.textContent = '选择 PDF 后预览页面';
    });
    options.addEventListener('keydown', event => {
      if (event.isComposing || event.ctrlKey || event.altKey || event.metaKey
          || event.key.length !== 1 || event.key === ' ') return;
      const key = event.key.toLocaleLowerCase();
      const choices = [...options.children];
      const current = choices.indexOf(document.activeElement);
      const next = choices.slice(current + 1).concat(choices.slice(0, current + 1))
        .find(option => option.textContent.toLocaleLowerCase().startsWith(key));
      if (!next) return;
      event.preventDefault();
      next.focus();
    });
    async function previewPage(restore = null) {
      clear(true);
      const sourceId = select.dataset.value;
      const number = page.valueAsNumber;
      if (!sourceId) return;
      if (!Number.isInteger(number) || number < 1 || (pageCount && number > pageCount)) {
        paper.hidden = true;
        message.dataset.error = '';
        message.textContent = pageCount ? '请输入 1–' + pageCount + ' 之间的页码' : '请输入有效页码';
        return;
      }
      previewRequested = true;
      const current = generation;
      stage.setAttribute('aria-busy', 'true');
      message.textContent = '正在预览第 ' + number + ' 页…';
      try {
        const response = await post({action: 'preview_pdf', source_id: sourceId, page: number});
        const bitmap = new Image();
        bitmap.src = response.data;
        await bitmap.decode();
        if (generation !== current || !dialog.open) return;
        result = response;
        if (restore && restore.revision === response.revision
            && restore.box.some((edge, index) => edge !== wholePage[index])) {
          box = restore.box;
        }
        pageCount = response.page_count;
        page.max = String(pageCount);
        syncPagination();
        image = bitmap;
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        paper.hidden = false;
        paper.inert = false;
        syncPaperSize();
        showSelection();
        describeSelection();
      } catch (failure) {
        if (generation === current && dialog.open) {
          clear();
          message.dataset.error = '';
          message.textContent = failure.message;
        }
      } finally {
        if (generation === current) stage.setAttribute('aria-busy', 'false');
      }
    }
    loadPage.disabled = true;
    syncPagination();
    loadPage.addEventListener('click', () => previewPage());
    function stepPage(offset) {
      page.value = String(page.valueAsNumber + offset);
      previewPage();
    }
    previousPage.addEventListener('click', () => stepPage(-1));
    nextPage.addEventListener('click', () => stepPage(1));
    page.addEventListener('input', () => {
      clear(true);
      if (previewRequested) {
        message.textContent = '正在更新页码…';
        previewTimer = setTimeout(previewPage, 160);
      }
    });
    page.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        previewPage();
      }
    });
    use.addEventListener('click', () => {
      if (!result || use.disabled) return;
      try {
        let preview = result.data;
        if (box) {
          const [left, top, right, bottom] = box;
          const width = (right - left) * canvas.width;
          const height = (bottom - top) * canvas.height;
          const crop = node('canvas');
          crop.width = Math.ceil(width);
          crop.height = Math.ceil(height);
          crop.getContext('2d').drawImage(image, left * canvas.width, top * canvas.height,
            width, height, 0, 0, crop.width, crop.height);
          preview = crop.toDataURL();
        }
        const label = result.title + (result.citation_page ? ' · 印刷页 ' + result.citation_page : '')
          + ' · PDF 第 ' + result.page + ' 页';
        onUse({source_id: result.source_id, page: result.page, revision: result.revision,
          box: box || wholePage}, preview, label);
        dialog.close();
      } catch {
        message.dataset.error = '';
        message.textContent = '无法使用这张图，请重新预览后重试';
      }
    });
    dialog.showModal();
    try {
      const {source_files: sources} = await global.MEFinderApi.getJSON('/api/sources');
      for (const source of sources.filter(source => source.source_type === 'pdf')) {
        const title = source.display_title || source.title || source.file_name;
        const option = button('', 'app-select-option');
        option.dataset.value = source.source_file_id;
        option.title = title;
        option.tabIndex = -1;
        option.setAttribute('role', 'option');
        option.append(node('span', 'app-select-value', title));
        options.append(option);
      }
      if (options.firstElementChild) {
        const originalPDF = initial && [...options.children].find(option => option.dataset.value === initial.source_id);
        selectMaterialOption(select, (originalPDF || options.firstElementChild).dataset.value, 'PDF 文献');
        trigger.disabled = loadPage.disabled = false;
        if (originalPDF) {
          page.value = String(initial.page);
          previewPage(initial);
        } else if (initial) {
          message.textContent = '原 PDF 已不在文献库';
        }
      } else {
        trigger.querySelector('.app-select-value').textContent = '暂无 PDF';
        message.textContent = '文献库里暂无 PDF';
      }
    } catch (failure) {
      trigger.querySelector('.app-select-value').textContent = '文献读取失败';
      message.textContent = failure.message;
    }
  }

  function openTag(path) {
    choose(path);
    if (!host.classList.contains('active')) navigateTo('materials');
  }

  async function exportMarkdown() {
    const payload = {
      action: 'export', heading: active ? displayPath(active) : '素材库',
      excerpt_ids: visible.filter(excerpt => !excerpt.digest).map(excerpt => excerpt.excerpt_id)
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
  bindMaterialSelect(typeFilter, value => {
    selectMaterialOption(typeFilter, value, '素材类型');
    render();
  });
  addImage.addEventListener('click', () => showImageImport());
  layout.addEventListener('dragover', event => {
    if (event.dataTransfer.types.includes('Files')) event.preventDefault();
  });
  layout.addEventListener('drop', event => {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    showImageImport(event.dataTransfer.files);
  });
  document.addEventListener('paste', event => {
    if (!host.classList.contains('active') || document.querySelector('dialog[open]')) return;
    const files = [...event.clipboardData.files].filter(file => file.type.startsWith('image/'));
    if (!files.length) return;
    event.preventDefault();
    if (imageImport.hidden) showImageImport(files);
    else imageImport.dispatchEvent(new CustomEvent('material-image-files', {detail: files}));
  });
  exportButton.addEventListener('click', exportMarkdown);
  global.addEventListener('mefinder-materials-change', () => {
    if (host.classList.contains('active')) refresh();
  });
  global.MEFinder.materials = Object.freeze({refresh, openTag, chips});
}(window));
