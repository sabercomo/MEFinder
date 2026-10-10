/* 对齐任务监听：后端只跑一个，同一窗口只监听一份。
   后端一次只有一个对齐任务（单 job_id）。阅读器和作品页都要知道它何时结束，
   所以监听只有这一份：谁先认领就由谁记下 origin，结束后广播给所有订阅者，
   结局提示只由发起方给出——两份监听会让同一个任务弹两次提示、刷两次视图。
   - 主窗口与独立阅读窗口各装一份（各自的 JS 环境），不跨窗口共享轮询。
   - 本服务只查询状态并广播结果；不持有阅读器 DOM、不刷新作品页、不弹提示。
   - 默认经 MEFinderApi.fetch 查询；MEFinderReader.configure({fetch}) 会转发到
     configure({fetch})，与阅读器其他请求共用同一个注入。 */
(function (global) {  // module: 15-alignment-jobs.js
  var JOB_POLL_MS = 1500;
  var STATUS_ENDPOINT = '/api/text-alignments/status';
  var injectedFetch = null;
  var jobWatch = {jobId: '', meta: null, progress: null, subscribers: [], progressSubscribers: []};

  function configure(options) {
    options = options || {};
    if (typeof options.fetch === 'function') injectedFetch = options.fetch;
  }

  function fetchFunction() {
    return (injectedFetch || global.MEFinderApi.fetch).bind(global);
  }

  function subscribe(handler) {
    if (typeof handler !== 'function') return function () {};
    jobWatch.subscribers.push(handler);
    return function () {
      var index = jobWatch.subscribers.indexOf(handler);
      if (index >= 0) jobWatch.subscribers.splice(index, 1);
    };
  }

  // 已在监听的任务不改归属：后认领者只是共享同一份监听。
  function watch(jobId, meta) {
    if (!jobId || jobWatch.jobId === jobId) return;
    jobWatch.jobId = String(jobId);
    jobWatch.meta = meta || {};
    jobWatch.progress = jobWatch.meta.progress || null;
    poll(jobWatch.jobId);
  }

  function running() {
    if (!jobWatch.jobId) return null;
    var current = {jobId: jobWatch.jobId};
    Object.keys(jobWatch.meta || {}).forEach(function (name) {
      current[name] = jobWatch.meta[name];
    });
    current.progress = jobWatch.progress;
    return current;
  }

  function onProgress(handler) {
    jobWatch.progressSubscribers.push(handler);
    return function () {
      var index = jobWatch.progressSubscribers.indexOf(handler);
      if (index >= 0) jobWatch.progressSubscribers.splice(index, 1);
    };
  }

  async function poll(jobId) {
    // 后台生成期间每 ~1.5s 查询一次任务状态，直到非 202（完成、失败或取消）。
    while (jobWatch.jobId === jobId) {
      await new Promise(function (resolve) { global.setTimeout(resolve, JOB_POLL_MS); });
      if (jobWatch.jobId !== jobId) return;
      var response;
      try {
        response = await fetchFunction()(
          STATUS_ENDPOINT + '?job_id=' + encodeURIComponent(jobId),
          {headers: {'Accept': 'application/json'}}
        );
      } catch (_error) {
        continue;
      }
      var payload = {};
      try { payload = await response.json(); } catch (_error) { payload = {}; }
      if (jobWatch.jobId !== jobId) return;
      if (response.status === 202) {
        jobWatch.progress = payload.progress || null;
        jobWatch.progressSubscribers.slice().forEach(function (handler) { handler(running()); });
        continue;
      }
      var event = {
        jobId: jobId,
        meta: jobWatch.meta || {},
        outcome: response.ok && payload.ok ? 'ok'
          : payload.cancelled ? 'cancelled'
            : response.status === 404 ? 'unknown' : 'failed',
        error: payload.error || ''
      };
      jobWatch.jobId = '';
      jobWatch.meta = null;
      jobWatch.progress = null;
      jobWatch.subscribers.slice().forEach(function (handler) {
        try { handler(event); } catch (_error) { /* 一个订阅者出错不拖垮其他订阅者。*/ }
      });
      return;
    }
  }

  global.MEFinderAlignmentJobs = Object.freeze({
    watch: watch,
    subscribe: subscribe,
    onProgress: onProgress,
    running: running,
    configure: configure
  });
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
