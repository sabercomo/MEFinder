(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config;

  function closeReviewPopover(restoreFocus) {
    var review = state.review;
    if (!review) return;
    state.review = null;
    review.node.remove();
    if (restoreFocus && review.trigger && review.trigger.isConnected) review.trigger.focus();
  }

  function candidateLocation(candidate) {
    var span = (candidate.page_match_spans || [])[0];
    if (!span) return '';
    if (span.pdf_page_index != null) return 'PDF 第 ' + (Number(span.pdf_page_index) + 1) + ' 页';
    if (span.paragraph_index != null) return '第 ' + (Number(span.paragraph_index) + 1) + ' 段';
    return '';
  }

  async function openReviewPopover(trigger) {
    closeReviewPopover();
    var index = Number(trigger.dataset.readerLink);
    var link = state.links && state.links.items[index];
    if (!link) return;
    var near = link.target_segment_ids || [];
    var node = document.createElement('div');
    node.className = 'mef-reader-review';
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-labelledby', 'mef-reader-review-title');
    var heading = document.createElement('h4');
    heading.id = 'mef-reader-review-title';
    heading.textContent = '这一段的对应可能不准';
    var hint = document.createElement('p');
    hint.textContent = '勾选这段原文对应的全部译文段落';
    var list = document.createElement('div');
    list.className = 'mef-reader-review-list';
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', '候选译文段落');
    var loadingText = document.createElement('p');
    loadingText.className = 'mef-reader-review-loading';
    loadingText.textContent = '正在读取候选段落…';
    list.appendChild(loadingText);
    var actions = document.createElement('div');
    actions.className = 'mef-reader-review-actions';
    var save = r.createButton('保存校正', 'mef-reader-tool-btn is-primary', '');
    save.disabled = true;
    var none = r.createButton('译本无对应', 'mef-reader-tool-btn is-quiet', '');
    var later = r.createButton('暂不处理', 'mef-reader-tool-btn is-quiet', '');
    var grow = document.createElement('span');
    grow.className = 'mef-reader-toolbar-spacer';
    actions.appendChild(save);
    actions.appendChild(none);
    actions.appendChild(grow);
    actions.appendChild(later);
    node.appendChild(heading);
    node.appendChild(hint);
    node.appendChild(list);
    node.appendChild(actions);
    state.elements.root.appendChild(node);
    state.review = {node: node, trigger: trigger};
    var rect = trigger.getClientRects()[0];
    var rootRect = state.elements.root.getClientRects()[0];
    var width = Math.min(400, rootRect.width - 32);
    node.style.width = width + 'px';
    node.style.left = Math.max(16, Math.min(rect.right - rootRect.left - width, rootRect.width - width - 16)) + 'px';
    node.style.top = Math.max(16, Math.min(rect.bottom - rootRect.top + 8, rootRect.height - 320)) + 'px';

    var checked = new Set(link.target_segment_ids || []);
    function syncSave() { save.disabled = checked.size === 0; }
    async function submit(kind) {
      [save, none, later].forEach(function (button) { button.disabled = true; });
      try {
        if (kind === 'later') {
          await r.postJSON(config.correctionDeferEndpoint, {
            source_file_id: state.sourceId,
            target_source_file_id: state.comparison.targetSourceId,
            source_segment_ids: link.source_segment_ids
          });
        } else {
          await r.postJSON(config.correctionSaveEndpoint, {
            source_file_id: state.sourceId,
            target_source_file_id: state.comparison.targetSourceId,
            source_segment_ids: link.source_segment_ids,
            target_segment_ids: kind === 'none' ? [] : Array.from(checked)
          });
          r.notify(kind === 'none' ? '已记录：译本无对应' : '已保存校正，对应 ' + checked.size + ' 段译文');
        }
        closeReviewPopover(false);
        r.invalidateLinks();
        r.loadLinkWindow();
        // 校正与暂缓都会改变「N 处待检查」：同一条失效通道通知宿主。
        if (typeof config.onAlignmentDataChanged === 'function') {
          config.onAlignmentDataChanged();
        }
      } catch (error) {
        [none, later].forEach(function (button) { button.disabled = false; });
        syncSave();
        r.setAlert(error && error.message ? error.message : '保存失败', 'warning');
      }
    }
    save.addEventListener('click', function () { submit('save'); });
    none.addEventListener('click', function () { submit('none'); });
    later.addEventListener('click', function () { submit('later'); });
    none.focus();
    try {
      var payload = await r.postJSON(config.reviewCandidatesEndpoint, {
        source_file_id: state.sourceId,
        target_source_file_id: state.comparison.targetSourceId,
        source_segment_ids: link.source_segment_ids,
        near_target_segment_ids: near,
        radius: 3
      });
      if (!state.review || state.review.node !== node) return;
      list.replaceChildren();
      if (!(payload.candidates || []).length) {
        loadingText.textContent = '附近没有可选的译文段落';
        list.appendChild(loadingText);
      }
      (payload.candidates || []).forEach(function (candidate) {
        var id = String(candidate.segment_id);
        var row = document.createElement('label');
        row.className = 'mef-reader-review-row';
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.checked = checked.has(id);
        box.addEventListener('change', function () {
          if (box.checked) checked.add(id);
          else checked.delete(id);
          syncSave();
        });
        var text = document.createElement('span');
        text.className = 'mef-reader-review-text';
        text.textContent = String(candidate.text || '');
        var where = document.createElement('small');
        where.textContent = candidateLocation(candidate);
        row.appendChild(box);
        row.appendChild(text);
        row.appendChild(where);
        list.appendChild(row);
      });
      syncSave();
    } catch (error) {
      if (!state.review || state.review.node !== node) return;
      loadingText.textContent = error && error.message ? error.message : '候选段落读取失败';
    }
  }


  r.closeReviewPopover = closeReviewPopover;
  r.openReviewPopover = openReviewPopover;
}(window));
