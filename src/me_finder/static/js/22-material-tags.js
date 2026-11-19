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
          : excerpt ? {action: 'edit', excerpt_id: excerpt.excerpt_id, tags: [...checked]}
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
    load, post, openDialog, selectionKey, tagTree, tagsByExcerpt, displayPath, tagName, parentPath, node, button,
  });
}(window));
