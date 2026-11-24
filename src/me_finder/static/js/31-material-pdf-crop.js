(function (global) {  // module: 31-material-pdf-crop.js
  'use strict';

  const {node, button, post} = global.MEFinderMaterials;

  // The import form owns its draft and the app-select presentation.
  global.MEFinder.createMaterialPDFCrop = function ({selectMaterialOption, bindMaterialSelect, cloneSelectIcon}) {
    return async function showPDFCrop(onUse, initial = null) {
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
      trigger.append(node('span', 'app-select-value', '正在读取文献…'), cloneSelectIcon());
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
    };
  };
}(window));
