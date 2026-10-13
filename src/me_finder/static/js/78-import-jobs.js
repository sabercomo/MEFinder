/* 导入任务：队列、上传、轮询、取消、恢复和重试的唯一状态写入方。
   界面通过 snapshot() 读取平面副本（File 为不可变对象），不持有内部任务对象。
   同一任务只保留一条轮询链；删除或切换任务使旧响应与定时器失效。
   确认、提示、渲染及文献刷新由 80-import.js 注入，本模块不操作 DOM。 */
(function (global) {  // module: 78-import-jobs.js
  'use strict';

  var host;
  var importStore = {queue: []};
  var watches = new Map();

  function configure(nextHost) { host = nextHost; }

  function snapshot() {
    return importStore.queue.map(function(item) { return Object.assign({}, item); });
  }

  function add(item) { importStore.queue.push(Object.assign({}, item)); }

  function stopPolling(id) {
    var watch = watches.get(id);
    if (watch) clearTimeout(watch.timer);
    watches.delete(id);
  }

  function isCurrent(watch) {
    return watches.get(watch.item.id) === watch && watch.item.jobId === watch.jobId;
  }

  function drop(id) {
    stopPolling(id);
    importStore.queue = importStore.queue.filter(function(item) { return item.id !== id; });
  }

  async function removeImport(id, options) {
    options = options || {};
    var q = importStore.queue.find(function(item) { return item.id === id; });
    if (q && q.jobId && q.status === 'processing' && !options.skipConfirm) {
      var serviceName = q.route === 'local_ocr' ? (q.providerName || '本地 OCR') : q.route === 'mineru' ? 'MinerU' : (q.providerName || '视觉解析 API');
      if (!await host.askConfirm(
        '将停止 ' + serviceName + ' 后台解析；当前请求完成后不会再提交新页面',
        {title:'停止并移除任务？', confirmText:'停止任务', tone:'danger'}
      )) return false;
    }
    if (q && q.uploadId) {
      var activeUploadIds = [q.uploadId];
      q.uploadId = null;
      for (var uploadIndex = 0; uploadIndex < activeUploadIds.length; uploadIndex += 1) {
        try {
          await MEFinderApi.fetch('/api/import-upload/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({upload_id: activeUploadIds[uploadIndex]})
          });
        } catch (cancelError) {
          console.warn('cancel chunked upload failed:', cancelError);
        }
      }
    }
    if (q && q.jobId && ['processing', 'paused', 'error'].indexOf(q.status) >= 0) {
      try {
        var resp = await MEFinderApi.fetch('/api/import-resume-dismiss', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({job_id: q.jobId})
        });
        var data = await resp.json();
        if (!resp.ok || data.error) throw new Error(data.error || '移除任务失败');
        if (data.state === 'cancelling') {
          stopPolling(q.id);
          q.cancelRequested = true;
          q.status = 'processing';
          q.canResume = false;
          q.message = '正在停止后台解析，停止完成后即可重新导入…';
          if (!options.silent) host.toast(q.message, 'success');
          if (!options.deferRender) host.changed();
          pollImportJob(q.id);
          return true;
        }
      } catch (e) {
        if (!options.silent) host.toast('移除导入任务失败：' + e.message);
        return false;
      }
    }
    drop(id);
    if (!options.deferRender) host.changed();
    return true;
  }

  var IMPORT_UPLOAD_FALLBACK_CHUNK_BYTES = 4 * 1024 * 1024;

  async function uploadImportFile(q, importKind, progressLabel) {
    var startResp = await MEFinderApi.fetch('/api/import-upload/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        file_name: q.file.name,
        size: q.file.size,
        parse_mode: q.parseMode || 'auto',
        provider_id: q.providerId || '',
        import_kind: importKind || 'document'
      })
    });
    var startData = await startResp.json();
    if (!startResp.ok || startData.error) throw new Error(startData.error || '无法开始读取文件');
    var uploadId = startData.upload_id;
    if (!uploadId) throw new Error('上传任务编号缺失');
    q.uploadId = uploadId;
    try {
      var totalSize = Number(q.file.size || 0);
      var chunkSize = Number(startData.chunk_size || IMPORT_UPLOAD_FALLBACK_CHUNK_BYTES);
      if (!Number.isFinite(chunkSize) || chunkSize <= 0) chunkSize = IMPORT_UPLOAD_FALLBACK_CHUNK_BYTES;
      chunkSize = Math.min(chunkSize, 8 * 1024 * 1024);
      var offset = 0;
      while (offset < totalSize) {
        var end = Math.min(offset + chunkSize, totalSize);
        var uploadChunk = q.file.slice(offset, end);
        var chunkResp = await MEFinderApi.fetch('/api/import-upload/chunk', {
          method: 'POST',
          headers: {
            'Content-Type': q.file.type || 'application/octet-stream',
            'X-Upload-ID': uploadId,
            'X-Upload-Offset': String(offset)
          },
          body: uploadChunk
        });
        var chunkData = await chunkResp.json();
        if (!chunkResp.ok || chunkData.error) throw new Error(chunkData.error || '读取文件失败');
        var received = Number(chunkData.received_size);
        if (!Number.isFinite(received) || received !== end) throw new Error('上传分块位置校验失败');
        offset = received;
        q.message = progressLabel + ' ' + (totalSize ? Math.round(offset * 100 / totalSize) : 0) + '%';
        host.changed();
      }
    } catch (error) {
      try {
        await MEFinderApi.fetch('/api/import-upload/cancel', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({upload_id: uploadId})
        });
      } catch (cancelError) {
        console.warn('cancel chunked upload failed:', cancelError);
      }
      q.uploadId = null;
      throw error;
    }
    return uploadId;
  }

  async function uploadImport(id) {
    var q = importStore.queue.find(function(q) { return q.id === id; });
    if (!q) return;
    q.status = 'processing';
    q.step = 0;
    q.message = '正在读取文件…';
    host.changed();
    var uploadId = null;
    try {
      uploadId = await uploadImportFile(q, q.importKind || 'document', '正在读取文件…');
      q.uploadId = uploadId;
      var resp = await MEFinderApi.fetch('/api/import-upload/finish', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({upload_id: uploadId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '导入失败');
      uploadId = null;
      q.uploadId = null;
      q.jobId = data.job_id;
      if (data.already_running) {
        importStore.queue.filter(function(item) {
          return item !== q && item.jobId === q.jobId;
        }).forEach(function(item) { drop(item.id); });
      }
      q.providerId = data.provider_id || q.providerId;
      if (q.type === 'pdf' && data.detected_pdf_type) {
        q.detectedType = data.detected_pdf_type;
        q.route = data.parse_route || (data.detected_pdf_type === 'native_text' ? 'native' : 'mineru');
        q.step = 2;
        q.message = '检测结果：' + pdfTypeLabel(data.detected_pdf_type)
          + (q.route === 'vision' ? '，将使用其他视觉 API'
            : q.route === 'local_ocr' ? '，将使用本地 OCR'
            : q.route === 'mineru' && q.providerId === 'mineru-local' ? '，将使用本地 MinerU'
            : q.route === 'mineru' ? '，将使用 MinerU 在线解析' : '，使用本地快速解析');
      } else {
        q.step = 1;
        q.message = '文件已保存，正在建立索引…';
      }
      if (data.already_running) {
        q.providerId = data.provider_id || null;
        q.cancelRequested = data.status === 'cancelling';
        q.message = q.cancelRequested
          ? '正在停止后台解析，停止完成后即可重新导入…'
          : '同一文献已有导入任务，正在跟踪原任务进度…';
      }
      host.changed();
      if (q.jobId) pollImportJob(q.id);
    } catch (e) {
      var pendingUploadIds = [uploadId].filter(Boolean);
      for (var pendingIndex = 0; pendingIndex < pendingUploadIds.length; pendingIndex += 1) {
        try {
          await MEFinderApi.fetch('/api/import-upload/cancel', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({upload_id: pendingUploadIds[pendingIndex]})
          });
        } catch (cancelError) {
          console.warn('cancel chunked upload failed:', cancelError);
        }
      }
      q.uploadId = null;
      q.status = 'error';
      q.message = e.message || '导入失败';
      host.changed();
    }
  }

  function pollImportJob(id) {
    var q = importStore.queue.find(function(item) { return item.id === id; });
    if (!q || !q.jobId) return;
    var existing = watches.get(id);
    if (existing && existing.jobId === q.jobId && existing.item === q) return;
    stopPolling(id);
    var watch = {item: q, jobId: q.jobId, timer: null};
    watches.set(id, watch);
    poll(watch);
  }

  function poll(watch) {
    var q = watch.item;
    MEFinderApi.fetch('/api/import-status?job_id=' + encodeURIComponent(watch.jobId))
      .then(function(resp) {
        if (!isCurrent(watch)) return null;
        if (resp.status === 404 && q.cancelRequested) {
          drop(q.id);
          host.changed();
          return null;
        }
        return resp.json();
      })
      .then(function(data) {
        if (data === null || !isCurrent(watch)) return;
        if (data.error) throw new Error(data.error);
        if (data.parse_route) q.route = data.parse_route;
        if (data.provider_id) q.providerId = data.provider_id;
        if (data.provider_name) q.providerName = data.provider_name;
        if (data.source_file_id) q.sourceFileId = data.source_file_id;
        q.mineruFailed = !!data.mineru_failed;
        q.visionFailed = !!data.vision_failed;
        q.mineruInterrupted = !!data.mineru_interrupted;
        q.canRetryLocalMineru = !!data.can_retry_with_local_mineru;
        if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.route = 'mineru';
        else if (data.phase === 'local_ocr_processing') q.route = 'local_ocr';
        else if (data.phase === 'vision_processing') q.route = 'vision';
        else if (data.phase === 'text_parsing' && q.type === 'pdf') q.route = 'native';
        var steps = importStepsFor(q);
        if (data.phase === 'validating_result') q.step = 1;
        else if (data.phase === 'mineru_submitting' || data.phase === 'mineru_processing') q.step = steps.indexOf('MinerU 解析');
        else if (data.phase === 'local_ocr_processing') q.step = steps.indexOf('本地 OCR');
        else if (data.phase === 'vision_processing') q.step = 2;
        else if (data.phase === 'text_parsing') q.step = q.type === 'pdf' ? steps.indexOf('本地解析') : steps.indexOf('恢复书目与页码');
        else if (data.phase === 'rebuilding_index' || data.phase === 'metadata_recognition') q.step = steps.indexOf('建立索引');
        else if (data.status === 'completed') q.step = steps.length;
        q.message = data.message || q.message;
        if (data.status === 'cancelling') q.cancelRequested = true;
        if (q.cancelRequested) q.message = '正在停止后台解析，停止完成后即可重新导入…';
        if (data.status === 'completed') {
          q.status = 'done';
          q.message = data.message || '导入完成，已自动更新索引';
          if (q.sourceFileId) host.settled(q.sourceFileId);
          host.completed();
        } else if (data.status === 'failed') {
          q.cancelRequested = false;
          q.status = 'error';
          q.message = data.message || '导入失败';
          if (q.sourceFileId) host.settled(q.sourceFileId);
          q.failureStage = data.failure_stage || null;
          q.canResume = !!data.can_resume;
          q.canRetryVision = !!data.can_retry_with_provider;
          q.retryProviderId = data.retry_provider_id || q.providerId || null;
          q.retryProviderName = data.retry_provider_name || q.providerName || null;
          q.needsProviderConfig = !!data.needs_provider_config;
        } else if (data.status === 'paused') {
          q.status = 'paused';
          q.canResume = !!data.can_resume;
          q.message = data.message || '上次导入已暂停，可继续导入';
        }
        host.changed();
        if (q.status === 'processing') {
          watch.timer = setTimeout(function() {
            if (isCurrent(watch)) poll(watch);
          }, 2500);
        } else stopPolling(q.id);
      })
      .catch(function(err) {
        if (!isCurrent(watch)) return;
        stopPolling(q.id);
        q.status = 'error';
        q.message = err.message || '读取导入状态失败';
        if (q.sourceFileId) host.settled(q.sourceFileId);
        host.changed();
      });
  }

  async function loadResumableImports() {
    try {
      var resp = await MEFinderApi.fetch('/api/import-resumable');
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '读取恢复任务失败');
      (data.jobs || []).forEach(function(job) {
        if (importStore.queue.some(function(item) { return item.jobId === job.job_id; })) return;
        var isPaused = job.status === 'paused';
        importStore.queue.push({
          id: 'resume-' + job.job_id,
          jobId: job.job_id,
          name: job.file_name || '未命名文献',
          size: Number(job.size_bytes || 0),
          type: job.file_type === 'pdf' ? 'pdf' : job.file_type === 'epub' ? 'epub' : 'docx',
          status: isPaused ? 'paused' : 'error',
          step: job.file_type === 'pdf' ? 2 : 1,
          route: job.parse_route || null,
          providerId: job.provider_id || null,
          providerName: job.provider_name || null,
          detectedType: job.detected_pdf_type || null,
          message: job.message || (isPaused ? '上次导入已暂停，可继续导入' : '上次导入未完成'),
          failureStage: job.failure_stage || null,
          canResume: !!job.can_resume,
          canRetryVision: !!job.can_retry_with_provider,
          retryProviderId: job.retry_provider_id || job.provider_id || null,
          retryProviderName: job.retry_provider_name || job.provider_name || null,
          needsProviderConfig: !!job.needs_provider_config,
          mineruFailed: !!job.mineru_failed,
          visionFailed: !!job.vision_failed,
          mineruInterrupted: !!job.mineru_interrupted,
          canRetryLocalMineru: !!job.can_retry_with_local_mineru,
          fromJournal: true
        });
      });
      host.changed();
    } catch (e) {
      console.warn('load resumable imports failed:', e);
    }
  }

  async function loadActiveImports() {
    try {
      var resp = await MEFinderApi.fetch('/api/import-active');
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '读取在途任务失败');
      (data.jobs || []).forEach(function(job) {
        if (importStore.queue.some(function(item) { return item.jobId === job.job_id; })) return;
        importStore.queue.push({
          id: 'active-' + job.job_id,
          jobId: job.job_id,
          name: job.file_name || '未命名文献',
          size: Number(job.size_bytes || 0),
          type: job.file_type === 'pdf' ? 'pdf' : job.file_type === 'epub' ? 'epub' : 'docx',
          status: 'processing',
          step: job.file_type === 'pdf' ? 2 : 1,
          route: job.parse_route || null,
          providerId: job.provider_id || null,
          providerName: job.provider_name || null,
          detectedType: job.detected_pdf_type || null,
          message: job.message || '正在导入…',
          canResume: false,
          cancelRequested: job.status === 'cancelling',
          fromJournal: true
        });
        pollImportJob('active-' + job.job_id);
      });
      host.changed();
    } catch (e) {
      console.warn('load active imports failed:', e);
    }
  }

  async function resumeImport(id, options) {
    options = options || {};
    var q = importStore.queue.find(function(item) { return item.id === id; });
    if (!q || !q.jobId || !q.canResume) return;
    var serviceName = q.providerId === 'mineru-local' ? '本地 MinerU' : q.route === 'local_ocr' ? (q.providerName || '本地 OCR') : q.route === 'mineru' ? 'MinerU' : (q.providerName || '视觉解析 API');
    if (!options.skipConfirm && q.failureStage !== 'index' && q.type === 'pdf' && q.route !== 'native'
        && q.providerId !== 'mineru-local'
        && q.route !== 'local_ocr'
        && !await host.askConfirm(
          '将从上次断点继续调用 ' + serviceName + '，未完成部分可能产生费用',
          {title:'继续联网解析？', confirmText:'继续任务', tone:'warning'}
        )) return;
    stopPolling(q.id);
    try {
      var resp = await MEFinderApi.fetch('/api/import-resume', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: q.jobId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '继续任务失败');
      q.status = 'processing';
      q.canResume = false;
      q.message = q.failureStage === 'index'
        ? '正在重新建立索引，不会再次调用解析 API…'
        : '正在继续导入…';
      host.changed();
      pollImportJob(q.id);
    } catch (e) {
      host.toast((options.silent ? q.name + '：' : '') + '继续导入失败：' + e.message);
    }
  }

  async function retryImportWithVision(id) {
    var q = importStore.queue.find(function(item) { return item.id === id; });
    if (!q || !q.jobId) return;
    var provider = host.retryProvider(Object.assign({}, q));
    if (!provider) {
      host.openSettings();
      host.toast('请先配置一个其他解析 API');
      return;
    }
    var providerId = provider.id;
    var providerName = provider.name || '其他解析 API';
    if (!await host.askConfirm(
      '将改用「' + providerName + '」重新解析这份 PDF，可能产生费用',
      {title:'切换解析接口？', confirmText:'切换并重试', tone:'warning'}
    )) return;
    stopPolling(q.id);
    try {
      var resp = await MEFinderApi.fetch('/api/import-retry', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: q.jobId, provider_id: providerId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '重试失败');
      q.jobId = data.job_id;
      q.status = 'processing';
      q.route = 'vision';
      q.providerId = data.provider_id;
      q.providerName = data.provider_name || providerName;
      q.step = 2;
      q.message = '正在切换到 ' + q.providerName + '…';
      q.canRetryVision = false;
      q.needsProviderConfig = false;
      q.visionFailed = false;
      host.changed();
      pollImportJob(q.id);
    } catch (e) {
      host.toast('切换解析接口失败：' + e.message);
    }
  }

  async function retryImportWithMinerU(id) {
    var q = importStore.queue.find(function(item) { return item.id === id; });
    if (!q || !q.jobId || q.type !== 'pdf') return;
    if (!await host.askConfirm(
      '将保留原文件，停止使用当前视觉 API，改由免费 MinerU 解析',
      {title:'改用 MinerU？', confirmText:'改用 MinerU'}
    )) return;
    stopPolling(q.id);
    try {
      var resp = await MEFinderApi.fetch('/api/import-retry-mineru', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: q.jobId})
      });
      var data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || '切换 MinerU 失败');
      q.jobId = data.job_id;
      q.status = 'processing';
      q.route = 'mineru';
      q.providerId = null;
      q.providerName = null;
      q.step = 2;
      q.message = '正在切换到 MinerU，不需要重新上传文件…';
      q.canResume = false;
      q.canRetryVision = false;
      q.visionFailed = false;
      host.changed();
      pollImportJob(q.id);
    } catch (e) {
      host.toast('切换 MinerU 失败：' + e.message);
    }
  }

  async function retryImportWithLocalMinerU(id) {
    var q = importStore.queue.find(function(item) { return item.id === id; });
    if (!q || !q.jobId || q.type !== 'pdf' || !q.canRetryLocalMineru) return;
    if (!await host.askConfirm(
      '将保留原文件，停止在线 MinerU 任务，改由你已运行的本地 MinerU 服务重新解析',
      {title:'切换到本地部署？', confirmText:'切换到本地部署'}
    )) return;
    stopPolling(q.id);
    try {
      var response = await MEFinderApi.fetch('/api/import-retry-mineru-local', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({job_id: q.jobId})
      });
      var data = await response.json();
      if (!response.ok || data.error) throw new Error(data.error || '切换失败');
      q.jobId = data.job_id;
      q.status = 'processing';
      q.route = 'mineru';
      q.providerId = 'mineru-local';
      q.providerName = '本地 MinerU';
      q.step = 2;
      q.message = '正在切换到本地 MinerU，不需要重新上传文件…';
      q.canResume = false;
      q.canRetryLocalMineru = false;
      q.mineruFailed = false;
      q.mineruInterrupted = false;
      host.changed();
      pollImportJob(q.id);
    } catch (error) {
      host.toast('切换到本地部署失败：' + error.message);
    }
  }

  async function reparse(sourceId, source) {
    var queueItem = {
      id: 'mineru-reparse-' + Date.now(),
      sourceFileId: sourceId,
      name: source.file_name || source.title || '未命名 PDF',
      size: Number(source.size_bytes || 0),
      type: 'pdf',
      parseMode: 'mineru',
      status: 'processing',
      step: 2,
      route: 'mineru',
      detectedType: source.pdf_profile && source.pdf_profile.detected_pdf_type,
      message: '正在提交 MinerU 在线解析…'
    };
    importStore.queue.push(queueItem);
    host.changed();
    try {
      var resp = await MEFinderApi.fetch('/api/mineru-reparse', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({source_id: sourceId})
      });
      var data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || '提交失败');
      var tracked = importStore.queue.find(function(item) {
        return item !== queueItem && item.jobId === data.job_id;
      });
      if (tracked) {
        drop(queueItem.id);
        tracked.status = 'processing';
        tracked.route = 'mineru';
        tracked.message = 'MinerU 解析已在进行中…';
        host.changed();
        pollImportJob(tracked.id);
        return;
      }
      queueItem.jobId = data.job_id;
      queueItem.detectedType = data.detected_pdf_type || queueItem.detectedType;
      queueItem.message = data.already_running
        ? 'MinerU 解析已在进行中，正在读取进度…'
        : '已提交 MinerU，正在等待解析进度…';
      host.parsing(sourceId);
      host.changed();
      pollImportJob(queueItem.id);
    } catch(e) {
      queueItem.status = 'error';
      queueItem.message = '提交 MinerU 解析失败：' + e.message;
      host.changed();
      host.toast('提交 MinerU 解析失败：' + e.message, 'danger');
    }
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.importJobs = Object.freeze({
    configure: configure, snapshot: snapshot, add: add,
    upload: uploadImport, pollJob: pollImportJob, remove: removeImport,
    loadResumableImports: loadResumableImports, loadActiveImports: loadActiveImports,
    resume: resumeImport, retryWithVision: retryImportWithVision,
    retryWithMinerU: retryImportWithMinerU, retryWithLocalMinerU: retryImportWithLocalMinerU,
    reparse: reparse
  });
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
