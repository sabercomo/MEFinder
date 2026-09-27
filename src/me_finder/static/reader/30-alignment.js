(function (global) {
  'use strict';
  var r = global.__MEFinderReaderInternal;
  var state = r.state, config = r.config, alignmentJobs = r.alignmentJobs;

  function showPendingPane(show) {
    if (!state.elements) return;
    state.elements.pending.hidden = !show;
    state.elements.comparisonViewport.hidden = show;
    state.elements.comparisonPrevious.hidden = show;
    state.elements.comparisonNext.hidden = show;
    state.elements.readerBody.classList.toggle('is-pending', show);
  }

  function openPendingComparison(targetId, info) {
    r.markComparisonOpen(targetId);
    showPendingPane(true);
    var elements = state.elements;
    var running = info.status === 'running';
    elements.pendingAction.dataset.readerForce = info.stale_reason ? 'true' : 'false';
    if (running) {
      elements.pendingTitle.textContent = '正在生成对齐';
      elements.pendingText.textContent = '在本机计算，文本不会上传。关闭阅读器也会在后台继续';
      elements.pendingAction.textContent = '取消';
      elements.pendingAction.dataset.readerAction = 'cancel-alignment';
      elements.pendingAction.classList.remove('is-primary');
      elements.pendingAction.disabled = false;
    } else if (r.canGenerate()) {
      elements.pendingTitle.textContent = info.stale_reason ? '旧对齐已不可读' : '这两版尚未对齐';
      elements.pendingText.textContent = '在本机生成，文本不会上传。生成时左栏照常阅读，完成后译文出现在这里';
      elements.pendingAction.textContent = info.stale_reason ? '重新对齐' : '生成对齐';
      elements.pendingAction.dataset.readerAction = 'generate-alignment';
      elements.pendingAction.classList.add('is-primary');
      elements.pendingAction.disabled = false;
    } else {
      elements.pendingTitle.textContent = '这两版尚未对齐';
      elements.pendingText.textContent = r.generateBlockedReason();
      var installable = typeof config.onInstallComponent === 'function' && state.availability !== 'unknown';
      elements.pendingAction.textContent = state.availability === 'model_missing' ? '去设置' : '重新安装';
      elements.pendingAction.dataset.readerAction = 'install-component';
      elements.pendingAction.classList.remove('is-primary');
      elements.pendingAction.hidden = !installable;
    }
    if (r.canGenerate() || running) elements.pendingAction.hidden = false;
    updateComparisonNotice();
    r.updateComparisonControls();
    r.renderToolbar();
    r.noteReadingSessionChanged();
  }

  function updateComparisonNotice() {
    if (!state.elements) return;
    var elements = state.elements;
    var notice = elements.comparisonNotice;
    var targetId = state.comparison.targetSourceId;
    if (!state.comparison.open || !targetId || !elements.pending.hidden) {
      notice.hidden = true;
      elements.readerBody.classList.remove('is-indirect');
      return;
    }
    var info = r.pairInfo(state.sourceId, targetId);
    var target = (state.alignmentTargets || []).find(function (t) {
      return String(t.source_file_id || '') === targetId;
    });
    var viaId = (info.status === 'indirect' && info.via_source_file_id) ||
      (target && target.via_source_file_id) || '';
    elements.readerBody.classList.toggle('is-indirect', !!viaId);
    var text = '';
    var actionLabel = '';
    var force = false;
    if (info.status === 'running') {
      text = '正在生成对齐，完成后自动刷新';
      actionLabel = '取消';
    } else if (info.stale_reason) {
      text = info.stale_reason === 'model_changed'
        ? '对齐模型已更换，以下是旧结果'
        : info.stale_reason === 'body_range_changed'
          ? '正文范围识别已修正，以下是旧结果'
          : '对齐算法已更新，以下是旧结果';
      actionLabel = '重新对齐';
      force = true;
    } else if (viaId) {
      var viaName = r.alignmentTargetName(String(viaId));
      text = '这两版没有直接对齐，位置经' + (viaName ? '「' + viaName + '」' : '基准版本') + '换算，可能错位或漏段';
      actionLabel = '生成直接对齐';
    } else if (state.comparison.lowConfidence) {
      text = '此处为粗定位，可能锚到相邻段落或注释';
    }
    notice.hidden = !text;
    elements.comparisonNoticeText.textContent = text;
    var action = elements.comparisonNoticeAction;
    action.dataset.readerForce = force ? 'true' : 'false';
    if (!actionLabel) {
      action.hidden = true;
    } else if (info.status === 'running') {
      action.hidden = false;
      action.textContent = actionLabel;
      action.dataset.readerAction = 'cancel-alignment';
    } else if (r.canGenerate()) {
      action.hidden = false;
      action.textContent = actionLabel;
      action.dataset.readerAction = 'generate-alignment';
    } else {
      action.hidden = state.availability !== 'unavailable' || typeof config.onInstallComponent !== 'function';
      action.textContent = '重新安装';
      action.dataset.readerAction = 'install-component';
    }
  }

  async function startComparisonAlignment(force) {
    var targetId = state.comparison.targetSourceId;
    var groupId = state.work.groupId || state.alignmentGroupId;
    if (!targetId || !groupId) {
      r.setAlert('这两个版本不在同一部作品中，无法生成对齐', 'warning');
      return;
    }
    if (!r.canGenerate()) {
      r.setAlert(r.generateBlockedReason(), 'warning');
      return;
    }
    var pivotId = state.sourceId;
    if (state.work.baseId === targetId) {
      pivotId = targetId;
      targetId = state.sourceId;
    }
    try {
      var payload = await r.postJSON(config.alignmentStartEndpoint, {
        document_group_id: groupId,
        pivot_source_file_id: pivotId,
        target_source_file_id: targetId,
        force: !!force
      });
      alignmentJobs.watch(payload.job_id, {
        origin: 'reader', groupId: groupId, key: r.pairKey(pivotId, targetId)
      });
      refreshComparisonAfterStatusChange();
    } catch (error) {
      r.setAlert(error && error.message ? error.message : '生成对齐失败', 'warning');
    }
  }

  async function cancelComparisonAlignment() {
    try {
      await r.postJSON(config.alignmentCancelEndpoint, {});
      r.notify('正在取消，当前批次结束后停止');
    } catch (error) {
      r.setAlert(error && error.message ? error.message : '取消失败', 'warning');
    }
  }

  function refreshComparisonAfterStatusChange() {
    r.renderToolbar();
    var targetId = state.comparison.targetSourceId;
    if (!state.comparison.open || !targetId) return;
    if (!state.elements.pending.hidden) {
      var info = r.pairInfo(state.sourceId, targetId);
      if (r.pairReadable(info)) r.openComparisonWith(targetId);
      else openPendingComparison(targetId, info);
    } else {
      updateComparisonNotice();
    }
  }

  async function applyAlignmentJobEnd(event) {
    // 提示只给发起方；由作品页发起的任务由作品页报告结果。
    if (event.meta.origin === 'reader') {
      if (event.outcome === 'ok') r.notify('对齐已生成');
      else if (event.outcome === 'cancelled') r.notify('已取消生成对齐');
      else if (event.outcome === 'failed') {
        // 阅读器已关闭时没有可写的提示条，退回宿主 toast。
        if (state.open) r.setAlert(event.error || '生成对齐失败', 'warning');
        else r.notify(event.error || '生成对齐失败');
      }
    }
    if (!state.open) return;
    var sourceId = state.sourceId;
    await r.loadAlignmentTargets(sourceId);
    // 等待目标列表时可能已换书或关闭；旧刷新不能使新书的作品请求失效。
    if (!state.open || state.sourceId !== sourceId) return;
    await r.loadWorkContext(sourceId);
    if (!state.open || state.sourceId !== sourceId) return;
    if (event.outcome === 'ok' && event.meta.groupId &&
        event.meta.groupId === state.work.groupId && state.comparison.open) {
      // A base-leg update also changes indirect pairs within this work.
      state.links = null;
      state.linkRequestSerial += 1;
      r.clearLinkedSelection();
      state.comparison.lastSourceRange = '';
      r.openComparisonWith(state.comparison.targetSourceId);
      r.renderToolbar();
    } else {
      refreshComparisonAfterStatusChange();
    }
  }

  /* ── 新窗口 / 回到主窗口 / 跳到页 ─────────────────────────────── */
  /* ── 阅读会话：一次阅读的完整位置 ─────────────────────────────
   * 同一份会话被四处使用，形状只在这里定义一次：
   *   1. 地址栏深链（刷新、独立窗口重载后可恢复，带右栏）
   *   2. state.lastSession（本窗口内的 restore）
   *   3. 「在新窗口打开 / 回到主窗口」的交接
   *   4. 服务端 reading_positions（按作品记「上次读到」，供「继续阅读」）
   * 恢复优先级：显式 options（宿主的「继续阅读」，其右栏来自服务端位置）
   * > 地址栏深链 > lastSession > 每本书的对照记忆（localStorage）。
   * 阅读器自己不去读服务端位置擅自跳转——跳不跳由宿主决定。
   */

  r.showPendingPane = showPendingPane;
  r.openPendingComparison = openPendingComparison;
  r.updateComparisonNotice = updateComparisonNotice;
  r.startComparisonAlignment = startComparisonAlignment;
  r.cancelComparisonAlignment = cancelComparisonAlignment;
  r.applyAlignmentJobEnd = applyAlignmentJobEnd;
}(window));
