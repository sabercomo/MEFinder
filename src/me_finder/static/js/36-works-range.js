/* 作品双版本正文范围审阅。 */
(function (global) {  // module: 36-works-range.js
  /* ── body range review ───────────────────────────────────────── */
  // 正文范围：每本书一个连续区间，界面上的「结尾」就是最后一段（首尾都算正文），
  // 提交时换算成库内的半开区间。两边独立浏览、独立修改，一次提交带两个范围。
  var RANGE_WINDOW = 7;

  function rangeSegmentsUrl(side, params) {
    var query = ['source_id=' + encodeURIComponent(side.source_file_id),
      'segment_set_id=' + encodeURIComponent(side.segment_set_id)];
    Object.keys(params).forEach(function (key) { query.push(key + '=' + encodeURIComponent(params[key])); });
    return '/api/text-alignments/body-range/segments?' + query.join('&');
  }

  function openBodyRangeDialog(ctx, group, a, b) {
    var works = ctx.works, rangeDraftKey = ctx.rangeDraftKey, pivotFor = ctx.pivotFor;
    var openDialog = ctx.openDialog, el = ctx.el, button = ctx.button;
    var canGenerate = ctx.canGenerate, generateBlockedReason = ctx.generateBlockedReason;
    var memberName = ctx.memberName, sourceTitle = ctx.sourceTitle, languageName = ctx.languageName;
    var requestJSON = ctx.requestJSON, postJSON = ctx.postJSON, closeDialog = ctx.closeDialog, startJob = ctx.startJob;
    var order = pivotFor(group, a, b);
    var draftKey = rangeDraftKey(group.document_group_id, a, b);
    var state = {loading: true, error: '', submitting: false, sides: [], restored: false};
    var dialog = openDialog(el('div'), 'tw-range-title');
    dialog.classList.add('is-wide', 'tw-range-dialog');

    function sideOf(name) {
      return state.sides.find(function (item) { return item.side === name; });
    }

    function invalid(side) { return side.end < side.start; }

    function submittable() {
      return !state.loading && !state.error && state.sides.length === 2 &&
        !state.sides.some(invalid) && !state.submitting;
    }

    function draw(side, resetWindow) {
      var focusId = document.activeElement && document.activeElement.id;
      if (side) {
        updateBook(side, resetWindow);
        dialog.querySelector('#tw-range-submit').disabled = !submittable() || !canGenerate() || !!works.running || !!works.queue;
        if (focusId) restoreFocus(focusId);
        return;
      }
      var body = el('div', {className: 'tw-dialog-body'});
      if (state.loading) body.appendChild(el('p', {className: 'tw-range-note', text: '正在读取两本书的文本…'}));
      else if (state.error) {
        body.appendChild(el('p', {className: 'tw-range-error', role: 'alert', text: state.error}));
        body.appendChild(button('重试', 'sm', load));
      } else {
        if (state.restored) {
          body.appendChild(el('p', {className: 'tw-range-note', text: '已恢复上次未成功提交的范围'}));
        }
        body.appendChild(el('div', {className: 'tw-range-pair'},
          state.sides.map(bookColumn)));
      }
      dialog.replaceChildren(
        el('div', {className: 'tw-dialog-head'}, [
          el('h3', {id: 'tw-range-title', text: '正文范围'}),
          el('p', {className: 'tw-range-note', text: '分别确定两本书从哪一段开始、到哪一段结束，首尾两段都算正文'})
        ]),
        body,
        el('div', {className: 'tw-dialog-foot'}, [
          el('span', {className: 'tw-push-left tw-range-note', text: '范围只对这一对版本生效，按当前解析的文本段记录'}),
          button('取消', '', closeDialog),
          button(state.submitting ? '正在提交' : '保存范围并重新对齐', 'primary', submit, {
            id: 'tw-range-submit',
            disabled: !submittable() || !canGenerate() || !!works.running || !!works.queue,
            title: canGenerate() ? null : generateBlockedReason()
          })
        ])
      );
      if (focusId) restoreFocus(focusId);
    }

    // 重绘后把焦点还回发起操作的控件；它用完就变灰时（设过的那一端、用尽的撤销）
    // 退到同一本书当前选中的文本段，键盘操作不掉回页面开头。
    function restoreFocus(focusId) {
      var restore = dialog.querySelector('#' + focusId);
      if (restore && !restore.disabled) { restore.focus({preventScroll: true}); return; }
      var owner = /-(pivot|target)$/.exec(focusId);
      var side = owner && sideOf(owner[1]);
      var fallback = side && dialog.querySelector('#tw-range-pick-' + side.side + '-' + side.selected);
      if (fallback) fallback.focus({preventScroll: true});
    }

    function bookColumn(side) {
      var column = el('section', {className: 'tw-range-book', 'aria-label': memberName(group, side.source_file_id) + ' 正文范围'}, [
        el('div', {className: 'tw-range-book-head'}, [
          el('h4', {text: memberName(group, side.source_file_id)}),
          el('p', {className: 'tw-range-note', text: [sourceTitle(side.source_file_id), languageName(side.language_code)]
            .filter(Boolean).join(' · ')})
        ]),
        boundsSummary(side),
        navRow(side)
      ]);
      var reading = el('div', {className: 'tw-range-reading'});
      if (side.outline.length) reading.appendChild(outlineRow(side));
      reading.appendChild(segmentList(side));
      column.appendChild(reading);
      column.appendChild(pagerRow(side));
      column.appendChild(setterRow(side));
      column.appendChild(el('p', {className: 'tw-range-error', role: 'alert',
        text: invalid(side) ? '结尾在开头之前，请重新设置这一端' : side.message}));
      side.column = column;
      return column;
    }

    // 仅更新这一侧的范围与文本；保留两侧目录、输入框和滚动容器本身。
    function updateBook(side, resetWindow) {
      var column = side.column;
      var reading = column.querySelector('.tw-range-reading');
      var scrollTop = reading.scrollTop;
      column.querySelector('.tw-range-bounds').replaceWith(boundsSummary(side));
      column.querySelector('.tw-range-segments').replaceWith(segmentList(side));
      column.querySelector('.tw-range-pager').replaceWith(pagerRow(side));
      column.querySelector('.tw-range-set').replaceWith(setterRow(side));
      column.querySelector(':scope > .tw-range-error').textContent = invalid(side)
        ? '结尾在开头之前，请重新设置这一端' : side.message;
      reading.scrollTop = resetWindow ? 0 : scrollTop;
      if (resetWindow) {
        column.querySelector('.tw-range-number').value = side.locator_kind === 'pdf_page'
          ? ((side.knownSegments[side.selected] || {}).physical_page_1based || '') : side.selected + 1;
      }
    }

    function pagerRow(side) {
      return el('div', {className: 'tw-range-pager'}, [
        button('↑ 前文', 'quiet sm', function () { showSegment(side, Math.max(0, side.offset - RANGE_WINDOW)); },
          {disabled: side.offset === 0 || side.windowLoading}),
        button('后文 ↓', 'quiet sm', function () { showSegment(side, side.offset + RANGE_WINDOW); },
          {disabled: side.offset + RANGE_WINDOW >= side.segment_count || side.windowLoading})
      ]);
    }

    function boundsSummary(side) {
      return el('div', {className: 'tw-range-bounds'}, [
        boundRow(side, 'start'), boundRow(side, 'end')
      ]);
    }

    function boundRow(side, edge) {
      var index = edge === 'start' ? side.start : side.end;
      var segment = side.knownSegments[index];
      return el('div', {className: 'tw-range-bound'}, [
        el('span', {className: 'tw-range-note', text: edge === 'start' ? '开头' : '结尾'}),
        el('span', {className: 'tw-range-bound-main'}, [
          el('span', {className: 'tw-range-note', text: (segment ? segment.page_display : '') + ' · 文本段 ' + (index + 1)}),
          el('span', {className: 'tw-range-snippet', text: segment ? segment.text : '正在读取…'})
        ]),
        button('查看', 'quiet sm', function () { showSegment(side, index, index); },
          {'aria-label': '查看' + (edge === 'start' ? '开头' : '结尾') + '所在原文'})
      ]);
    }

    function navRow(side) {
      var inputId = 'tw-range-goto-' + side.side;
      var byPage = side.locator_kind === 'pdf_page';
      var input = el('input', {
        id: inputId, type: 'number', min: '1', className: 'tw-range-number',
        value: String(byPage
          ? ((side.knownSegments[side.selected] || {}).physical_page_1based || '')
          : side.selected + 1),
        onkeydown: function (event) {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          go(Number(event.target.value));
        }
      });
      function go(value) {
        if (!(value >= 1)) return;
        if (byPage) showSegment(side, null, null, value);
        else showSegment(side, Math.min(value - 1, side.segment_count - 1), Math.min(value - 1, side.segment_count - 1));
      }
      return el('div', {className: 'tw-range-nav'}, [
        el('label', {className: 'tw-range-note', for: inputId, text: byPage ? 'PDF 页' : '文本段'}),
        input,
        button('跳转', 'sm', function () { go(Number(input.value)); })
      ]);
    }

    function outlineRow(side) {
      var chapters = el('div', {className: 'tw-range-chapters'}, side.outline.map(function (entry) {
        return button(entry.title, 'quiet sm', function () {
          showSegment(side, entry.segment_index, entry.segment_index);
        });
      }));
      return el('details', {className: 'tw-range-outline'}, [
        el('summary', {text: '目录'}), chapters
      ]);
    }

    function segmentList(side) {
      var list = el('fieldset', {className: 'tw-range-segments', 'aria-label': memberName(group, side.source_file_id) + ' 文本段'});
      if (side.windowError) {
        list.appendChild(el('p', {className: 'tw-range-error', role: 'alert', text: side.windowError}));
        list.appendChild(button('重试', 'sm', function () { showSegment(side, side.offset); }));
        return list;
      }
      if (side.windowLoading && !side.window.length) {
        list.appendChild(el('p', {className: 'tw-range-note', text: '正在读取文本…'}));
        return list;
      }
      side.window.forEach(function (segment) {
        var index = segment.segment_index;
        var tag = index === side.start && index === side.end ? '开头 / 结尾'
          : index === side.start ? '正文开头'
            : index === side.end ? '正文结尾'
              : index < side.start || index > side.end ? '范围外' : '';
        var radio = el('input', {
          type: 'radio', name: 'tw-range-pick-' + side.side,
          id: 'tw-range-pick-' + side.side + '-' + index,
          checked: side.selected === index,
          onchange: function () { side.selected = index; draw(side); }
        });
        list.appendChild(el('label', {
          className: 'tw-range-segment' + (side.selected === index ? ' is-selected' : '')
        }, [
          radio,
          el('span', {className: 'tw-range-segment-main'}, [
            el('span', {className: 'tw-range-segment-meta'}, [
              el('span', {className: 'tw-range-note', text: segment.page_display + ' · 文本段 ' + (index + 1)}),
              tag ? el('span', {className: 'tw-range-tag', text: tag}) : null
            ]),
            el('span', {className: 'tw-range-text', text: segment.text})
          ])
        ]));
      });
      return list;
    }

    function setterRow(side) {
      return el('div', {className: 'tw-range-set'}, [
        el('span', {className: 'tw-range-note', text: '已选文本段 ' + (side.selected + 1)}),
        el('div', {className: 'tw-range-set-actions'}, [
          button('设为开头', 'sm', function () { setEdge(side, 'start'); }, {
            id: 'tw-range-start-' + side.side, disabled: side.start === side.selected
          }),
          button('设为结尾', 'sm', function () { setEdge(side, 'end'); }, {
            id: 'tw-range-end-' + side.side, disabled: side.end === side.selected
          }),
          button('撤销', 'quiet sm', function () { undo(side); }, {
            id: 'tw-range-undo-' + side.side, disabled: !side.history.length
          })
        ])
      ]);
    }

    // 点选只改「当前选中段」；只有这里才改范围，且只改这一本、只改一端。
    function setEdge(side, edge) {
      side.history.push([side.start, side.end]);
      side[edge] = side.selected;
      side.message = '';
      draw(side);
    }

    function undo(side) {
      var previous = side.history.pop();
      if (!previous) return;
      side.start = previous[0];
      side.end = previous[1];
      side.message = '';
      draw(side);
    }

    async function showSegment(side, start, select, page) {
      side.windowLoading = true;
      side.windowError = '';
      side.message = '';
      draw(side);
      var params = page ? {count: RANGE_WINDOW, pdf_page: page} : {count: RANGE_WINDOW, start: Math.max(0, start)};
      try {
        var data = await requestJSON(rangeSegmentsUrl(side, params));
        if (!dialog.isConnected) return;
        side.offset = data.start;
        side.window = data.segments;
        data.segments.forEach(function (segment) { side.knownSegments[segment.segment_index] = segment; });
        var picked = select == null ? (page ? data.start : null) : select;
        if (picked != null) side.selected = picked;
        else if (!data.segments.some(function (segment) { return segment.segment_index === side.selected; })) {
          side.selected = data.start;
        }
      } catch (error) {
        if (!dialog.isConnected) return;
        if (page) side.message = error.message || '这一页没有可选文本，请换一页';
        else side.windowError = error.message || '文本读取失败';
      }
      side.windowLoading = false;
      draw(side, !side.windowError && !side.message);
    }

    async function load() {
      state.loading = true;
      state.error = '';
      draw();
      try {
        var data = await postJSON('/api/text-alignments/body-range', {
          document_group_id: group.document_group_id,
          pivot_source_file_id: order[0],
          target_source_file_id: order[1]
        });
        if (!dialog.isConnected) return;
        var draft = works.rangeDrafts[draftKey];
        state.sides = data.sides.map(function (side) {
          var known = {};
          known[side.start_segment.segment_index] = side.start_segment;
          known[side.end_segment.segment_index] = side.end_segment;
          return Object.assign({}, side, {
            start: side.body_start_index, end: side.body_end_index,
            selected: side.body_start_index, offset: side.body_start_index,
            knownSegments: known, window: [], windowLoading: false, windowError: '',
            history: [], message: ''
          });
        });
        state.restored = applyDraft(draft);
        state.loading = false;
        draw();
        state.sides.forEach(function (side) { showSegment(side, side.start, side.start); });
      } catch (error) {
        if (!dialog.isConnected) return;
        state.loading = false;
        state.error = error.message || '正文范围读取失败';
        draw();
      }
    }

    // 上次提交失败的草稿只在同一份分段数据上恢复；重新解析过就按当前范围来。
    function applyDraft(draft) {
      if (!draft) return false;
      var usable = state.sides.every(function (side) {
        var saved = draft.ranges[side.side];
        return draft.sets[side.side] === side.segment_set_id && saved &&
          saved[0] >= 0 && saved[1] <= side.segment_count && saved[0] < saved[1];
      });
      if (!usable) {
        delete works.rangeDrafts[draftKey];
        return false;
      }
      state.sides.forEach(function (side) {
        side.start = draft.ranges[side.side][0];
        side.end = draft.ranges[side.side][1] - 1;
        side.selected = side.start;
        side.offset = side.start;
      });
      return true;
    }

    async function submit() {
      if (!submittable()) return;
      var ranges = {}, sets = {};
      state.sides.forEach(function (side) {
        // 界面的结尾是最后一段，库内区间右端开区间，这里 +1 才不漏末段。
        ranges[side.side] = [side.start, side.end + 1];
        sets[side.side] = side.segment_set_id;
      });
      state.submitting = true;
      draw();
      works.rangeDrafts[draftKey] = {sets: sets, ranges: ranges};
      closeDialog();
      await startJob(group, order[0], order[1], true, ranges, sets);
    }

    draw();
    load();
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.workRange = {open: openBodyRangeDialog};
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
