/* 译本对照：批量重新对齐队列。
   队列状态只在本模块内写入：作品页经 snapshot() / active() / isQueued() 读取，
   经 start / stop / onJobEnd / recordStartError 驱动，不直接改队列对象。
   后端一次只跑一个对齐任务，队列逐个发起、逐个等结局；启动单个任务、刷新视图、
   提示与确认等宿主能力由 35-works.js 经 configure(host) 注入。 */
(function (global) {  // module: 34-works-queue.js
  'use strict';

  var host = null;
  var queue = null;

  function configure(nextHost) { host = nextHost; }

  function active() { return !!queue; }

  // 任务项是只含字符串的平面对象，逐项浅复制即完全隔离。
  function copyItems(items) {
    return items.map(function (item) { return Object.assign({}, item); });
  }

  // 只读副本：数组和每个任务项都是新对象，改它不影响队列。
  function snapshot() {
    if (!queue) return null;
    return {
      items: copyItems(queue.items), index: queue.index,
      completed: queue.completed, stopped: queue.stopped
    };
  }

  // 还在排队（尚未开始）的一对；正在跑的那一对由作品页的 running 判断。
  function isQueued(groupId, a, b) {
    if (!queue) return false;
    return queue.items.slice(queue.index + 1).some(function (item) {
      return item.groupId === groupId && host.pairKey(item.pivot, item.target) === host.pairKey(a, b);
    });
  }

  async function start(items, scope) {
    if (!items.length || queue || host.isRunning()) return;
    if (!host.canGenerate()) { host.toast(host.generateBlockedReason(), 'warning'); return; }
    if (!await host.askConfirm(
      '将用当前模型依次重新计算 ' + items.length + ' 组对齐，耗时取决于书籍长度，可随时停止',
      {title: '重新对齐' + scope + '？', confirmText: '开始重新对齐'}
    )) return;
    // 入队时复制：调用方之后改自己手里的任务项，不会改变队列要跑的内容。
    queue = {items: copyItems(items), index: 0, completed: 0, failures: [], stopped: false, lastError: ''};
    run();
  }

  async function run() {
    var current = queue;
    while (current && !current.stopped && current.index < current.items.length) {
      var item = current.items[current.index];
      var group = host.groupById(item.groupId);
      if (group) {
        host.refreshViews();
        if (await host.startJob(group, item.pivot, item.target, true)) return;
        current.failures.push(current.lastError || '生成对齐失败');
      }
      current.index += 1;
    }
    finish();
  }

  // 队列中逐个任务不弹提示，结束时由 finish 汇总一次。
  function onJobEnd(outcome, error) {
    if (!queue) return;
    if (outcome === 'ok') queue.completed += 1;
    else if (outcome === 'cancelled') queue.stopped = true;
    else if (outcome === 'failed') queue.failures.push(error || '生成对齐失败');
    queue.index += 1;
    run();
  }

  // 发起失败时作品页不单独弹提示，把原因记给队列汇总。
  function recordStartError(message) {
    if (queue) queue.lastError = message;
  }

  function finish() {
    var ended = queue;
    queue = null;
    host.refreshViews();
    if (!ended) return;
    var total = ended.items.length;
    if (ended.stopped) {
      host.toast('已停止重新对齐，完成 ' + ended.completed + '/' + total + ' 组', 'info');
    } else if (ended.failures.length) {
      host.toast('重新对齐完成 ' + ended.completed + '/' + total + ' 组，' + ended.failures.length + ' 组失败：' + ended.failures[0], 'danger');
    } else {
      host.toast('已重新对齐 ' + ended.completed + ' 组', 'success');
    }
  }

  function stop() {
    if (!queue) return;
    queue.stopped = true;
    host.refreshViews();
    if (host.isRunning()) host.cancelAlignment();
    else finish();
  }

  global.MEFinder = global.MEFinder || {};
  global.MEFinder.workQueue = Object.freeze({
    configure: configure,
    active: active,
    snapshot: snapshot,
    isQueued: isQueued,
    start: start,
    stop: stop,
    onJobEnd: onJobEnd,
    recordStartError: recordStartError
  });
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
