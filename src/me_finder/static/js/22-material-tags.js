(function (global) {  // module: 22-material-tags.js, shared with the standalone reader
  'use strict';

  const endpoint = '/api/materials';
  const fields = ['source_id', 'kind', 'start_anchor', 'end_anchor', 'start_offset', 'end_offset', 'text'];
  const selectionKey = (selection) => JSON.stringify(fields.map(field => selection[field]));
  const parentPath = (path) => path.slice(0, Math.max(0, path.lastIndexOf('/')));
  const tagName = (path) => path.slice(path.lastIndexOf('/') + 1);
  const displayPath = (path) => path.split('/').join(' / ');
  const load = () => global.MEFinderApi.getJSON(endpoint);
  const post = (payload) => global.MEFinderApi.postJSON(endpoint, payload);

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = text;
    return element;
  }

  function button(text, className = 'action-btn') {
    const element = node('button', className, text);
    element.type = 'button';
    return element;
  }

  function tagsByExcerpt(snapshot) {
    const map = new Map(snapshot.material_excerpts.map(excerpt => [excerpt.excerpt_id, []]));
    snapshot.material_excerpt_tags.forEach(({excerpt_id, path}) => map.get(excerpt_id).push(path));
    return map;
  }

  function tagTree(paths) {
    const children = new Map();
    paths.slice().sort((a, b) => a.localeCompare(b, 'zh-CN')).forEach(path => {
      const parent = parentPath(path);
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(path);
    });
    return children;
  }

  function normalizePath(value, parent) {
    const parts = value.trim().replace(/^#/, '').split(/[/／]/).map(part => part.trim());
    const path = (parent ? parent.split('/').concat(parts) : parts);
    if (path.length > 3) throw new Error('标签最多三级');
    if (parts.some(part => !part || Array.from(part).length > 30)) {
      throw new Error('每级标签名需为 1–30 个字符，层级之间用 / 分隔');
    }
    return path.join('/');
  }

  function createPicker(selection, onResize, onSaved) {
    const picker = node('div', 'materials-picker');
    picker.setAttribute('role', 'dialog');
    picker.setAttribute('aria-label', '给选中文字加标签');
    const input = node('input', 'materials-input');
    input.placeholder = '搜索或新建标签';
    input.maxLength = 100;
    input.autocomplete = 'off';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-label', input.placeholder);
    input.setAttribute('aria-controls', 'materials-picker-options');
    input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-autocomplete', 'list');
    const list = node('div', 'materials-picker-list');
    list.id = 'materials-picker-options';
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', '标签');
    const status = node('span', 'materials-picker-status materials-secondary', '正在读取标签…');
    status.setAttribute('role', 'status');
    picker.append(input, list, status);

    const known = new Set();
    let choices = [];
    let active = -1;
    let loaded = false;

    function setStatus(message, error = false) {
      status.hidden = !message;
      status.textContent = message;
      status.classList.toggle('materials-error', error);
    }

    function setActive(index) {
      const previous = list.children[active];
      if (previous) {
        previous.setAttribute('aria-selected', 'false');
        previous.lastElementChild.hidden = true;
      }
      active = index;
      const option = list.children[active];
      if (option) {
        option.setAttribute('aria-selected', 'true');
        option.lastElementChild.hidden = false;
        input.setAttribute('aria-activedescendant', option.id);
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    async function applyTag(path, keepOpen) {
      if (picker.inert) return;
      picker.inert = true;
      try {
        const result = await post({action: 'collect', selections: [selection], tags: [path]});
        global.dispatchEvent(new Event('mefinder-materials-change'));
        onSaved(result, keepOpen);
        if (keepOpen && picker.isConnected) {
          for (let tag = path; tag; tag = parentPath(tag)) known.add(tag);
          input.value = '';
          render();
        }
      } catch (failure) {
        setStatus(failure.message, true);
        onResize();
      } finally {
        picker.inert = false;
        if (picker.isConnected && !picker.hidden) input.focus({preventScroll: true});
      }
    }

    function render() {
      if (!loaded) return;
      const query = input.value.trim().replace(/^#/, '').split(/[/／]/).map(part => part.trim()).join('/');
      const term = query.toLowerCase();
      const paths = [...known].filter(path => path.toLowerCase().includes(term)).sort((a, b) => a.localeCompare(b, 'zh-CN'));
      const exact = known.has(query);
      if (exact) {
        paths.splice(paths.indexOf(query), 1);
        paths.unshift(query);
      }
      choices = paths.map(path => ({path, create: false}));
      let hint = query ? '' : '用 / 建立多级标签';
      let invalid = false;
      if (query && !exact) {
        try {
          choices.splice(2, 0, {path: normalizePath(input.value, ''), create: true});
        } catch (error) {
          hint = error.message;
          invalid = true;
        }
      }
      active = -1;
      list.replaceChildren(...choices.map(({path, create}, index) => {
        const option = button('', 'app-select-option' + (create ? ' materials-picker-create' : ''));
        option.id = 'materials-picker-option-' + index;
        option.tabIndex = -1;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        const label = node('span', 'materials-picker-path', create ? '＋ 创建并收录「' + path + '」' : displayPath(path));
        label.title = label.textContent;
        const shortcut = node('kbd', 'materials-picker-shortcut', '↵');
        shortcut.setAttribute('aria-hidden', 'true');
        shortcut.hidden = true;
        option.append(label, shortcut);
        option.addEventListener('mousedown', event => event.preventDefault());
        option.addEventListener('click', () => applyTag(path, false));
        return option;
      }));
      list.scrollTop = 0;
      setActive(query && choices.length ? 0 : -1);
      setStatus(hint, invalid);
      onResize();
    }

    input.addEventListener('input', render);
    input.addEventListener('keydown', event => {
      if (event.isComposing) return;
      if (event.key === 'Enter') {
        event.preventDefault();
        if (active >= 0) applyTag(choices[active].path, event.ctrlKey || event.metaKey);
      } else if (choices.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        setActive(active < 0 && step < 0 ? choices.length - 1 : (active + step + choices.length) % choices.length);
        list.children[active].scrollIntoView({block: 'nearest'});
      }
    });
    load().then(snapshot => {
      if (!picker.isConnected) return;
      for (const {path} of snapshot.material_tags) known.add(path);
      loaded = true;
      render();
    }).catch(failure => {
      setStatus(failure.message, true);
      onResize();
    });
    return picker;
  }

  function editRequest(material) {
    return material.digest ? {action: 'edit_image', digest: material.digest}
      : {action: 'edit', excerpt_id: material.excerpt_id};
  }

  function openDialog({selections, excerpt, parent = '', onSaved, onError}) {
    if (document.querySelector('.materials-dialog')) return;
    const create = !selections && !excerpt;
    const dialog = node('dialog', 'materials-dialog');
    dialog.setAttribute('aria-labelledby', 'material-tag-title');
    const form = node('form', 'materials-form');
    const title = node('h2', '', create ? (parent ? '新建子标签' : '新建标签')
      : excerpt ? '编辑标签' : '收集语料并加标签');
    title.id = 'material-tag-title';
    const context = node('p', 'materials-secondary', create ? displayPath(parent)
      : excerpt ? '勾选要保留的标签' : '已选 ' + selections.length + ' 条语料 · 原有标签会保留');
    context.hidden = !context.textContent;
    const list = node('div', 'materials-check-list');
    list.hidden = create;
    const label = node('label', '', create ? '名称' : '新建标签');
    label.htmlFor = 'material-tag-name';
    const input = node('input', 'materials-input');
    input.id = 'material-tag-name';
    input.maxLength = 100;
    input.placeholder = parent ? '输入子标签名' : '#实验/表征/Raman';
    input.setAttribute('aria-describedby', 'material-tag-hint');
    const hint = node('p', 'materials-secondary', '用 / 分层，最多三级');
    hint.id = 'material-tag-hint';
    const inputs = node('div', 'materials-form-row');
    const stage = button('添加');
    stage.hidden = create;
    inputs.append(input, stage);
    const error = node('p', 'materials-error');
    error.setAttribute('role', 'status');
    error.hidden = true;
    const actions = node('div', 'materials-form-actions');
    const cancel = button('取消');
    const submit = button(create ? '创建' : excerpt ? '保存标签' : '收集', 'action-btn primary');
    submit.type = 'submit';
    actions.append(cancel, submit);
    form.append(title, context, list, label, hint, inputs, error, actions);
    dialog.appendChild(form);
    document.body.appendChild(dialog);

    const known = new Set();
    const drafts = new Set();
    const checked = new Set(excerpt ? excerpt.tags : []);
    let busy = false;

    function showError(message) {
      error.hidden = !message;
      error.textContent = message;
    }

    function renderChoices(newPath) {
      const tree = tagTree([...known, ...drafts]);
      list.replaceChildren();
      function renderBranch(path, depth) {
        for (const child of tree.get(path) || []) {
          const row = node('label', 'materials-tag-choice');
          row.style.setProperty('--material-depth', depth);
          const checkbox = node('input');
          checkbox.type = 'checkbox';
          checkbox.checked = checked.has(child);
          checkbox.setAttribute('aria-label', displayPath(child));
          checkbox.addEventListener('change', () => {
            if (checkbox.checked) checked.add(child);
            else checked.delete(child);
            showError('');
          });
          row.append(checkbox, node('span', '', tagName(child)));
          if (child === newPath) {
            row.appendChild(node('small', 'materials-secondary', '新建'));
          }
          list.appendChild(row);
          if (child === newPath) row.scrollIntoView({block: 'nearest'});
          renderBranch(child, depth + 1);
        }
      }
      renderBranch('', 0);
    }

    function stagePath() {
      const path = normalizePath(input.value, parent);
      if (known.has(path) || drafts.has(path)) throw new Error('此标签已存在');
      const parts = path.split('/');
      parts.forEach((_, index) => {
        const prefix = parts.slice(0, index + 1).join('/');
        if (!known.has(prefix)) drafts.add(prefix);
      });
      checked.add(path);
      input.value = '';
      if (!create) renderChoices(path);
      showError('');
      return path;
    }

    stage.addEventListener('click', () => {
      try { stagePath(); }
      catch (failure) { showError(failure.message); }
      input.focus();
    });
    input.addEventListener('input', () => showError(''));
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.isComposing && !create) {
        event.preventDefault();
        stage.click();
      }
    });
    cancel.addEventListener('click', () => dialog.close());
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => dialog.remove(), {once: true});
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy) return;
      try {
        let path = '';
        if (create || input.value.trim()) path = stagePath();
        if (selections && !checked.size) throw new Error('请选择或新建至少一个标签');
        busy = true;
        form.inert = true;
        dialog.setAttribute('aria-busy', 'true');
        const payload = create ? {action: 'create_tag', path}
          : excerpt ? {...editRequest(excerpt), tags: [...checked]}
            : {action: 'collect', selections, tags: [...checked]};
        const result = await post(payload);
        dialog.close();
        global.dispatchEvent(new Event('mefinder-materials-change'));
        onSaved(result);
      } catch (failure) {
        showError(failure.message);
      } finally {
        busy = false;
        form.inert = false;
        dialog.removeAttribute('aria-busy');
        if (dialog.open) input.focus();
        if (dialog.open && create && !input.value) {
          input.value = [...checked].at(-1);
          drafts.clear();
          checked.clear();
        }
      }
    });

    load().then(snapshot => {
      for (const {path} of snapshot.material_tags) known.add(path);
      if (selections) {
        const map = tagsByExcerpt(snapshot);
        const tagSets = new Map(snapshot.material_excerpts.map(row => [selectionKey(row), map.get(row.excerpt_id)]));
        const first = tagSets.get(selectionKey(selections[0])) || [];
        first.filter(path => selections.every(item => tagSets.get(selectionKey(item))?.includes(path)))
          .forEach(path => checked.add(path));
      }
      if (!create) renderChoices();
      dialog.showModal();
      (list.querySelector(':checked') || list.querySelector('input') || input).focus();
    }).catch(failure => {
      dialog.remove();
      onError(failure.message);
    });
  }

  global.MEFinderMaterials = Object.freeze({
    load, post, openDialog, editRequest, createPicker, selectionKey, tagTree, tagsByExcerpt, normalizePath, displayPath, tagName, parentPath, node, button,
  });
}(window));
