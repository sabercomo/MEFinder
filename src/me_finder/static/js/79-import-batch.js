/* 联网书目批量补全与候选选择。 */
(function (global) {  // module: 79-import-batch.js
  /* ═══ 联网知网批量补全（茉莉花式候选选择）═══
   * 复用单篇详情里的 lookup-cnki / cnki-candidate / save 端点：顺序处理每一篇
   * 缺信息的期刊论文，天然满足知网单并发与“只补空字段”约束。高匹配唯一候选
   * 自动补，其余弹出候选选择框由用户决定，全程可随时停止。 */
  let cnkiBatchActive = false;
  let cnkiBatchChoiceResolve = null;
  let cnkiBatchCandidates = [];
  let cnkiBatchOpenUrl = '';

  function batchNode(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  // isForeignTitle / batchLookupSourceFor 已抽到 06-pure.js（纯逻辑，可单测）。

  function cnkiBatchLookupTargets() {
    return (libraryStore.sources || []).filter(function(src) {
      if (!src || String(src.source_type || '') !== 'pdf') return false;
      var meta = sourceBibliographicMetadata(src);
      // 人工维护的文献也纳入：applyBatchCandidateToSource 只补当前为空的字段，
      // 绝不覆盖已手动填写的值，因此不会破坏人工维护的内容。
      if (!batchLookupSourceFor(meta)) return false;
      var hasQueryKey = String(meta.title || '').trim() || String(meta.doi || '').trim() || String(meta.isbn || '').trim();
      if (!hasQueryKey) return false;
      return bibliographicMissingFields(meta).length > 0;
    });
  }

  async function runCnkiBatchButton() {
    await startCnkiBatchCompletion(document.getElementById('batch-cnki-btn'));
  }

  // 由「联网补全期刊信息」按钮触发：点击按钮本身即为联网授权，不再逐次弹确认框。
  async function startCnkiBatchCompletion(button) {
    if (cnkiBatchActive) return;
    var buttonLabel = button ? button.textContent : '';
    var targets = cnkiBatchLookupTargets();
    if (!targets.length) {
      showToast('没有需要联网补全的文献');
      return;
    }
    cnkiBatchActive = true;
    var stats = {auto:0, manual:0, notfound:0, skipped:0, failed:0};
    var stopped = false;
    var abortReason = '';
    for (var i = 0; i < targets.length; i++) {
      var src = targets[i];
      if (button) { button.disabled = true; button.textContent = '联网补全 ' + (i + 1) + '/' + targets.length + '…'; }
      var outcome;
      try {
        outcome = await processCnkiBatchItem(src, sourceBibliographicMetadata(src), i + 1, targets.length);
      } catch (e) {
        stats.failed++;
        continue;
      }
      if (outcome.action === 'stop') { stopped = true; break; }
      if (outcome.action === 'abort') { stopped = true; abortReason = outcome.reason || ''; break; }
      stats[outcome.result] = (stats[outcome.result] || 0) + 1;
    }
    closeCnkiBatchModal();
    cnkiBatchActive = false;
    if (button) { button.disabled = false; button.textContent = buttonLabel || '联网补全'; }
    await global.MEFinder.library.load(true);
    var parts = [];
    if (stats.auto) parts.push('自动补全 ' + stats.auto + ' 篇');
    if (stats.manual) parts.push('手动选择 ' + stats.manual + ' 篇');
    if (stats.notfound) parts.push('未找到 ' + stats.notfound + ' 篇');
    if (stats.skipped) parts.push('跳过 ' + stats.skipped + ' 篇');
    if (stats.failed) parts.push('失败 ' + stats.failed + ' 篇');
    var summary = parts.join('、') || '无变化';
    if (abortReason) {
      showToast('联网源暂时不可用（' + abortReason + '），已停止。已处理：' + summary, 'warning');
    } else {
      showToast((stopped ? '已停止联网补全：' : '联网补全完成：') + summary, stats.failed ? 'warning' : 'success');
    }
  }

  var _BATCH_SOURCE_META = {
    cnki: {endpoint:'/api/bibliographic-metadata/lookup-cnki', label:'知网', evSource:'cnki_lookup'},
    crossref: {endpoint:'/api/bibliographic-metadata/lookup-crossref', label:'Crossref', evSource:'crossref'},
    google_books: {endpoint:'/api/bibliographic-metadata/lookup-google-books', label:'图书目录', evSource:'k10plus'}
  };

  function setOnlineAutoMatchThreshold(pct) {
    var value = Math.round(Number(pct));
    if (!Number.isFinite(value)) return;
    value = Math.min(100, Math.max(ONLINE_METADATA_AUTO_MATCH_MIN_PERCENT, value));
    // 值没变只补 UI：偏好写盘有启动期被 syncOnlineAutoMatchControl 程序性回填触发的路径。
    if (Math.round(onlineMetadataAutoMatchThreshold * 100) === value) {
      syncOnlineAutoMatchControl();
      return;
    }
    onlineMetadataAutoMatchThreshold = value / 100;
    try { localStorage.setItem('meFinderOnlineAutoMatchThreshold', String(value)); } catch (_) {}
    global.persistDisplayPreference('online_auto_match_threshold', onlineMetadataAutoMatchThreshold);  // 随数据备份/迁移（C-01）
    syncOnlineAutoMatchControl();
  }

  function syncOnlineAutoMatchControl() {
    var pct = Math.round(onlineMetadataAutoMatchThreshold * 100);
    var slider = document.getElementById('online-auto-match-range');
    var label = document.getElementById('online-auto-match-value');
    if (slider && String(slider.value) !== String(pct)) slider.value = String(pct);
    // 程序性赋值不触发 input 事件（且这里绝不能 dispatch 合成 input：
    // inline oninput 会再进 setOnlineAutoMatchThreshold，形成写偏好的递归风暴），
    // 填充比例与算法对齐 60-settings.js 的 syncRangeFill，就地补一次。
    if (slider) {
      var span = Number(slider.max || 0) - Number(slider.min || 0);
      var ratio = span > 0 ? (pct - Number(slider.min || 0)) / span : 0;
      slider.style.setProperty(
        '--range-fill',
        (Math.min(Math.max(ratio, 0), 1) * 100).toFixed(2) + '%'
      );
    }
    if (label) label.textContent = pct + '%';
  }

  function automaticBatchCandidateIndex(candidates) {
    var bestIndex = -1;
    var bestScore = -1;
    (candidates || []).forEach(function(candidate, index) {
      var score = Number(candidate && candidate.match && candidate.match.score);
      if (Number.isFinite(score) && score >= onlineMetadataAutoMatchThreshold && score > bestScore) {
        bestIndex = index;
        bestScore = score;
      }
    });
    return bestIndex;
  }

  async function processCnkiBatchItem(src, meta, index, total) {
    var sourceId = src.source_file_id;
    var source = batchLookupSourceFor(meta);
    var info = _BATCH_SOURCE_META[source];
    var resp = await MEFinderApi.fetch(info.endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({metadata:batchQueryFor(source, meta)})
    });
    var data = await resp.json();
    if (!resp.ok || !data.ok) {
      // 验证码或限流表示站点在拦截：立即停止整批，不要继续冲击。
      if (data.code === 'verification_required' || data.code === 'rate_limited') {
        return {action:'abort', reason: info.label + (data.code === 'rate_limited' ? '限流' : '需要验证')};
      }
      throw new Error(data.error || (info.label + '查询失败'));
    }
    var candidates = data.candidates || [];
    if (!candidates.length) return {action:'next', result:'notfound'};
    var automaticIndex = automaticBatchCandidateIndex(candidates);
    if (automaticIndex >= 0) {
      var ok = await applyBatchCandidateToSource(sourceId, meta, candidates[automaticIndex], source);
      return {action:'next', result: ok ? 'auto' : 'failed'};
    }
    var choice = await promptCnkiBatchChoice(src, meta, candidates, data.open_url, index, total, info.label);
    if (choice.action === 'stop') return {action:'stop'};
    if (choice.action !== 'select') return {action:'next', result:'skipped'};
    var applied = await applyBatchCandidateToSource(sourceId, meta, candidates[choice.index], source);
    return {action:'next', result: applied ? 'manual' : 'failed'};
  }

  // 仅把当前为空的字段补进去，其余保持原样后整份保存。知网需再取详情页完整题录；
  // Crossref / Google Books 的候选一次即完整。图书补图书字段，期刊补期刊字段。
  async function applyBatchCandidateToSource(sourceId, currentMeta, candidate, source) {
    var fullMeta = candidate.metadata || {};
    var evidence = candidate.evidence || {};
    if (source === 'cnki' && candidate.record_url) {
      try {
        var resp = await MEFinderApi.fetch('/api/bibliographic-metadata/cnki-candidate', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({candidate:{record_url:candidate.record_url}})
        });
        var data = await resp.json();
        if (resp.ok && data.ok && data.metadata) {
          fullMeta = data.metadata;
          evidence = data.evidence || evidence;
        }
      } catch (e) { /* 详情读取失败时退回列表级字段 */ }
    }
    var payload = {};
    ['author','country','title','translator','publish_place','publisher','publish_year','isbn','journal_name','volume','issue','page_range','doi','issn'].forEach(function(k) {
      payload[k] = String(currentMeta[k] || '').trim();
    });
    payload.document_type = bibliographicDocType(currentMeta);
    var fillKeys = source === 'google_books'
      ? ['author','title','publisher','publish_place','publish_year','isbn']
      : Object.keys(global.MEFinder.bibliography.lookupFields);
    var defaultEvSource = _BATCH_SOURCE_META[source].evSource;
    var evidenceOut = {};
    var filledAny = false;
    fillKeys.forEach(function(k) {
      var incoming = String(fullMeta[k] || '').trim();
      if (!incoming || payload[k]) return;  // 只补当前为空的字段
      payload[k] = incoming;
      filledAny = true;
      var ev = evidence[k] || {source:defaultEvSource, evidence_text: incoming};
      evidenceOut[k] = Object.assign({}, ev, {value: incoming});
    });
    if (!filledAny) return false;
    payload.metadata_evidence = evidenceOut;
    var saveResp = await MEFinderApi.fetch('/api/bibliographic-metadata/save', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_id:sourceId, metadata:payload})
    });
    var saveData = await saveResp.json();
    return saveResp.ok && !!saveData.ok;
  }

  function promptCnkiBatchChoice(src, meta, candidates, openUrl, index, total, sourceLabel) {
    var backdrop = document.getElementById('cnki-batch-modal');
    var docEl = document.getElementById('cnki-batch-doc');
    var progressEl = document.getElementById('cnki-batch-progress');
    var listEl = document.getElementById('cnki-batch-list');
    if (!backdrop || !docEl || !listEl) return Promise.resolve({action:'skip'});
    cnkiBatchCandidates = candidates;
    cnkiBatchOpenUrl = openUrl || '';
    if (progressEl) progressEl.textContent = '第 ' + index + '/' + total + ' 条 · 请从' + (sourceLabel || '联网结果') + '选择正确记录';
    var docTitle = meta.title || (src.file_name || src.source_file_id);
    var docMeta = [meta.author, meta.publish_year, meta.journal_name || meta.publisher].filter(Boolean).join(' · ');
    docEl.replaceChildren(batchNode('div', 'cnki-batch-doc-title', docTitle));
    if (docMeta) docEl.appendChild(batchNode('div', 'cnki-batch-doc-meta', '本地信息：' + docMeta));
    listEl.replaceChildren();
    candidates.forEach(function(candidate, i) {
      var m = candidate.metadata || {};
      var match = candidate.match || {};
      var levelLabel = match.level === 'high' ? '高匹配' : (match.level === 'medium' ? '需核对' : '低匹配');
      var detail = [m.author, m.journal_name || m.publisher, candidate.publish_date || m.publish_year].filter(Boolean).join(' · ');
      var reasons = (match.reasons || []).join('、');
      var conflicts = (match.conflicts || []).join('、');
      var card = batchNode('div', 'cnki-candidate ' + (match.level || 'low'));
      var main = batchNode('div', 'cnki-candidate-main');
      main.appendChild(batchNode('div', 'cnki-candidate-title', m.title || '未识别篇名'));
      main.appendChild(batchNode('div', 'cnki-candidate-detail', detail || '联网记录'));
      var matchInfo = batchNode('div', 'cnki-candidate-match');
      matchInfo.appendChild(batchNode('span', null, levelLabel + (match.score != null ? ' · ' + Math.round(Number(match.score) * 100) + '%' : '')));
      if (reasons) matchInfo.appendChild(batchNode('span', null, reasons));
      if (conflicts) matchInfo.appendChild(batchNode('span', 'has-warning', '冲突：' + conflicts));
      main.appendChild(matchInfo);
      card.appendChild(main);
      var actions = batchNode('div', 'cnki-candidate-actions');
      [['打开记录', 'openCnkiBatchRecord', false], ['选择这条', 'selectCnkiBatchChoice', true]].forEach(function(option) {
        var button = batchNode('button', 'action-btn' + (option[2] ? ' primary' : ''), option[0]);
        button.type = 'button';
        button.dataset.action = option[1];
        button.dataset.index = String(i);
        actions.appendChild(button);
      });
      card.appendChild(actions);
      listEl.appendChild(card);
    });
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden', 'false');
    return new Promise(function(resolve) { cnkiBatchChoiceResolve = resolve; });
  }

  function openCnkiBatchRecord(i) {
    var candidate = (cnkiBatchCandidates || [])[i];
    global.openCnkiExternal((candidate && candidate.record_url) || cnkiBatchOpenUrl);
  }

  function openCnkiBatchCurrentRecord() {
    global.openCnkiExternal(cnkiBatchOpenUrl);
  }

  function resolveCnkiBatchChoice(choice) {
    var resolve = cnkiBatchChoiceResolve;
    cnkiBatchChoiceResolve = null;
    closeCnkiBatchModal();
    if (resolve) resolve(choice || {action:'skip'});
  }

  function closeCnkiBatchModal() {
    var backdrop = document.getElementById('cnki-batch-modal');
    if (!backdrop) return;
    backdrop.classList.remove('open');
    backdrop.setAttribute('aria-hidden', 'true');
  }

  function cnkiBatchBackdropClick(event) {
    if (event.target && event.target.id === 'cnki-batch-modal') resolveCnkiBatchChoice({action:'skip'});
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.importBatch = {syncOnlineAutoMatchControl: syncOnlineAutoMatchControl, openCnkiBatchRecord: openCnkiBatchRecord};
  global.runCnkiBatchButton = runCnkiBatchButton;
  global.setOnlineAutoMatchThreshold = setOnlineAutoMatchThreshold;
  global.openCnkiBatchCurrentRecord = openCnkiBatchCurrentRecord;
  global.resolveCnkiBatchChoice = resolveCnkiBatchChoice;
  global.cnkiBatchBackdropClick = cnkiBatchBackdropClick;
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
