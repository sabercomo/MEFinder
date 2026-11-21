(function (global) {  // module: 32-materials.js
  'use strict';

  const materials = global.MEFinderMaterials;
  const {node, button, load, post, openDialog, displayPath, tagName, parentPath, tagTree, tagsByExcerpt, normalizePath, editRequest} = materials;
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
    layout.hidden = true;
    addImage.hidden = true;
    imageImport.hidden = false;
    const heading = document.getElementById('materials-page-title');
    heading.textContent = '添加图片';
    const back = button('返回素材库');
    const form = node('form', 'materials-image-form');
    const input = node('input');
    input.type = 'file';
    input.accept = 'image/png,image/jpeg';
    input.hidden = true;
    const chooseFile = button('选择文件');
    chooseFile.addEventListener('click', () => input.click());
    const preview = node('img', 'materials-image-preview');
    preview.alt = '待保存的图片';
    preview.hidden = true;
    const drop = node('div', 'materials-image-drop');
    const emptyDrop = node('div', 'materials-image-drop-empty');
    emptyDrop.append(node('p', '', '拖入图片，或使用 ⌘V / Ctrl+V 粘贴'),
      node('p', 'materials-secondary', 'PNG / JPEG · 单张不超过 25 MB'), chooseFile);
    drop.append(preview, emptyDrop, input);
    const previewMeta = node('div', 'materials-image-alternative');
    previewMeta.hidden = true;
    const filename = node('span', 'materials-secondary');
    const replace = button('更换');
    replace.addEventListener('click', () => input.click());
    const remove = button('移除');
    previewMeta.append(filename, replace, remove);
    const fromPDF = button('从库内 PDF 截图…', 'action-btn materials-image-secondary');
    const cropIcon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    cropIcon.setAttribute('viewBox', '0 0 24 24');
    cropIcon.setAttribute('aria-hidden', 'true');
    const cropStrokes = document.createElementNS(cropIcon.namespaceURI, 'path');
    cropStrokes.setAttribute('d', 'M6 2v16h16M2 6h16v16');
    cropStrokes.setAttribute('fill', 'none');
    cropStrokes.setAttribute('stroke', 'currentColor');
    cropStrokes.setAttribute('stroke-width', '2');
    cropIcon.append(cropStrokes);
    fromPDF.prepend(cropIcon);
    const alternative = node('div', 'materials-image-alternative');
    alternative.append(node('span', 'materials-secondary', '也可以'), fromPDF,
      node('span', 'materials-secondary', '选页后框选区域'));
    const left = node('div');
    left.append(drop, previewMeta, alternative);
    const note = node('textarea', 'materials-input');
    note.rows = 4;
    note.maxLength = 200;
    const noteLabel = node('label', '', '说明（可选）');
    noteLabel.append(note);
    const tags = node('fieldset', 'materials-image-tags');
    tags.append(node('legend', '', '标签'));
    const choices = node('div', 'materials-image-tag-choices');
    const knownTags = new Set([active || '未整理', ...(snapshot?.material_tags || []).map(row => row.path)]);
    const tagInput = node('input', 'materials-input');
    tagInput.placeholder = '新建标签，例如 理论/异化';
    tagInput.setAttribute('aria-label', '新建标签');
    const newTag = button('新建');
    const tagControls = node('div', 'materials-form-actions');
    tagControls.append(tagInput, newTag);
    tags.append(choices, tagControls);
    const save = node('button', 'action-btn primary', '保存到素材库');
    save.type = 'submit';
    save.disabled = true;
    const message = node('p', 'materials-secondary', '选择图片后即可保存');
    message.setAttribute('role', 'status');
    const provenance = node('p', 'materials-secondary', '还没有选择图片');
    const right = node('div', 'materials-image-fields');
    right.append(provenance, noteLabel, tags, save, message);
    const selectedTags = () => [...choices.querySelectorAll('input:checked')].map(item => item.value);
    const updateSave = (hint = '') => {
      const hasTags = selectedTags().length > 0;
      save.disabled = !pending || !hasTags;
      message.textContent = hint || (!pending ? '选择图片后即可保存' : hasTags ? '' : '至少选择一个标签');
    };
    const addTagChoice = (path, checked) => {
      const label = node('label');
      const choice = node('input');
      choice.type = 'checkbox';
      choice.value = path;
      choice.checked = checked;
      choice.addEventListener('change', () => updateSave());
      label.append(choice, node('span', '', displayPath(path)));
      choices.append(label);
    };
    for (const path of [...knownTags].sort((a, b) => a.localeCompare(b, 'zh-CN'))) {
      addTagChoice(path, path === (active || '未整理'));
    }
    newTag.addEventListener('click', () => {
      try {
        const path = normalizePath(tagInput.value, '');
        if (knownTags.has(path)) {
          [...choices.querySelectorAll('input')].find(item => item.value === path).checked = true;
        } else {
          knownTags.add(path);
          addTagChoice(path, true);
        }
        tagInput.value = '';
        updateSave();
      } catch (failure) {
        message.textContent = failure.message;
      }
    });
    tagInput.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      newTag.click();
    });
    form.append(left, right);
    imageImport.replaceChildren(back, form);
    let pending;
    let revision = 0;
    let objectURL;
    const update = (source, url, name, hint = '') => {
      if (objectURL) URL.revokeObjectURL(objectURL);
      objectURL = url.startsWith('blob:') ? url : null;
      preview.src = url;
      preview.hidden = false;
      previewMeta.hidden = false;
      emptyDrop.hidden = true;
      filename.textContent = name;
      provenance.textContent = source.source_id ? name : '外部图片';
      pending = source;
      updateSave(hint);
    };
    const acceptFiles = async incoming => {
      if (!incoming.length) return;
      const current = ++revision;
      const file = incoming[0];
      if (file.size > 25 * 1024 * 1024) {
        message.textContent = '每张图片最多 25 MB';
        return;
      }
      const url = URL.createObjectURL(file);
      const test = new Image();
      test.src = url;
      try {
        await test.decode();
        if (revision !== current) return;
        if (!['image/png', 'image/jpeg'].includes(file.type)) {
          throw new Error('请选择 PNG、JPEG、WebP 或 GIF 图片');
        }
        update({file}, url, file.name, incoming.length > 1 ? '一次保存一张，已选第一张' : '');
      } catch {
        if (current === revision) message.textContent = '无法读取图片，请选择 PNG、JPEG、WebP 或 GIF';
      } finally {
        if (objectURL !== url) URL.revokeObjectURL(url);
      }
    };
    const acceptPaste = event => acceptFiles(event.detail);
    imageImport.addEventListener('material-image-files', acceptPaste);
    const close = () => {
      revision++;
      if (objectURL) URL.revokeObjectURL(objectURL);
      imageImport.removeEventListener('material-image-files', acceptPaste);
      imageImport.replaceChildren();
      imageImport.hidden = true;
      layout.hidden = false;
      addImage.hidden = false;
      heading.textContent = '素材库';
      addImage.focus();
    };
    back.addEventListener('click', async () => {
      if (pending && !await showAppConfirm('放弃这张图片？', {
        title: '放弃添加图片？', confirmText: '放弃', tone: 'danger'
      })) return;
      close();
    });
    remove.addEventListener('click', () => {
      revision++;
      if (objectURL) URL.revokeObjectURL(objectURL);
      objectURL = pending = null;
      input.value = '';
      preview.removeAttribute('src');
      preview.hidden = previewMeta.hidden = true;
      emptyDrop.hidden = false;
      provenance.textContent = '还没有选择图片';
      updateSave();
    });
    input.addEventListener('change', () => acceptFiles(input.files));
    drop.addEventListener('dragover', event => event.preventDefault());
    drop.addEventListener('drop', event => {
      event.preventDefault();
      acceptFiles(event.dataTransfer.files);
    });
    fromPDF.addEventListener('click', () => showPDFCrop((source, url, name) => {
      revision++;
      update(source, url, name);
    }));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!pending || save.disabled) return;
      save.disabled = true;
      form.inert = true;
      back.disabled = true;
      try {
        let source = pending;
        if (pending.file) {
          const file = pending.file;
          const data = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(',')[1]);
            reader.onerror = () => reject(new Error('无法读取图片'));
            reader.readAsDataURL(file);
          });
          source = {title: file.name, data};
        }
        const paths = selectedTags();
        const result = await post({action: 'collect_image', ...source, note: note.value, tags: paths});
        if (result.created) {
          filter.value = '';
          selectMaterialOption(typeFilter, 'all', '素材类型');
          const firstTag = result.tags[0];
          if (active && !result.tags.some(path => path === active || path.startsWith(active + '/'))) {
            active = firstTag;
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
        message.textContent = failure.message;
      } finally {
        save.disabled = false;
        form.inert = false;
        back.disabled = false;
      }
    });
    back.focus();
    if (files) acceptFiles(files);
  }

  async function showPDFCrop(onUse) {
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
    canvas.hidden = true;
    canvas.setAttribute('aria-label', 'PDF 页面预览。拖动框选截图区域');
    const message = node('p', 'materials-secondary', '选择 PDF 后预览页面');
    message.setAttribute('role', 'status');
    const cancel = button('取消');
    const use = button('使用这张图', 'action-btn materials-pdf-use');
    use.disabled = true;
    const actions = node('div', 'materials-form-actions');
    actions.append(cancel, use);
    dialog.append(heading, controls, message, canvas, actions);
    document.body.append(dialog);
    let result, image, start, box, draftBox;
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
    const clear = () => {
      clearTimeout(previewTimer);
      generation++;
      result = image = box = start = draftBox = null;
      canvas.hidden = true;
      use.disabled = true;
      syncPagination();
    };
    const describeSelection = () => {
      message.textContent = 'PDF 第 ' + result.page + ' / ' + pageCount
        + (box ? ' 页 · 已框选局部' : ' 页 · 可使用整页，或拖动框选');
    };
    const draw = () => {
      context.drawImage(image, 0, 0);
      const region = draftBox || box;
      if (!region) return;
      const [left, top, right, bottom] = region;
      context.strokeStyle = getComputedStyle(host).getPropertyValue('--accent-text').trim();
      context.lineWidth = 4;
      context.strokeRect(left * canvas.width, top * canvas.height,
        (right - left) * canvas.width, (bottom - top) * canvas.height);
    };
    const position = event => {
      const bounds = canvas.getBoundingClientRect();
      return [(event.clientX - bounds.left) / bounds.width, (event.clientY - bounds.top) / bounds.height]
        .map(value => Math.max(0, Math.min(1, value)));
    };
    const move = event => {
      if (!start) return;
      const [x, y] = position(event);
      draftBox = [Math.min(x, start[0]), Math.min(y, start[1]), Math.max(x, start[0]), Math.max(y, start[1])];
      draw();
    };
    canvas.addEventListener('pointerdown', event => {
      if (!image || !event.isPrimary || event.button !== 0) return;
      canvas.setPointerCapture(event.pointerId);
      start = position(event);
      use.disabled = true;
      move(event);
    });
    canvas.addEventListener('pointermove', move);
    const finishSelection = () => {
      if (!start) return;
      start = draftBox = null;
      use.disabled = false;
      draw();
      describeSelection();
    };
    canvas.addEventListener('pointerup', event => {
      if (!start) return;
      move(event);
      const [left, top, right, bottom] = draftBox;
      if ((right - left) * canvas.width >= 2 && (bottom - top) * canvas.height >= 2) box = draftBox;
      finishSelection();
    });
    canvas.addEventListener('pointercancel', finishSelection);
    canvas.addEventListener('lostpointercapture', finishSelection);
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
    async function previewPage() {
      clear();
      const sourceId = select.dataset.value;
      const number = page.valueAsNumber;
      if (!sourceId) return;
      if (!Number.isInteger(number) || number < 1 || (pageCount && number > pageCount)) {
        message.textContent = pageCount ? '请输入 1–' + pageCount + ' 之间的页码' : '请输入有效页码';
        return;
      }
      previewRequested = true;
      const current = generation;
      message.textContent = '正在预览第 ' + number + ' 页…';
      try {
        const response = await post({action: 'preview_pdf', source_id: sourceId, page: number});
        const bitmap = new Image();
        bitmap.src = response.data;
        await bitmap.decode();
        if (generation !== current || !dialog.open) return;
        result = response;
        pageCount = response.page_count;
        page.max = String(pageCount);
        syncPagination();
        image = bitmap;
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        canvas.hidden = false;
        draw();
        use.disabled = false;
        describeSelection();
      } catch (failure) {
        if (generation === current && dialog.open) message.textContent = failure.message;
      }
    }
    loadPage.disabled = true;
    syncPagination();
    loadPage.addEventListener('click', previewPage);
    function stepPage(offset) {
      page.value = String(page.valueAsNumber + offset);
      previewPage();
    }
    previousPage.addEventListener('click', () => stepPage(-1));
    nextPage.addEventListener('click', () => stepPage(1));
    page.addEventListener('input', () => {
      clear();
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
          box: box || [0, 0, 1, 1]}, preview, label);
        dialog.close();
      } catch {
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
        selectMaterialOption(select, options.firstElementChild.dataset.value, 'PDF 文献');
        trigger.disabled = loadPage.disabled = false;
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
