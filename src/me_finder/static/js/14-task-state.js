/* 任务状态的请求代次：同一份状态只采信最后发起的请求。
   组件卡片（模型下载、运行时安装、本地 OCR 等）同时有两类请求在改同一份状态：
   定时轮询的 GET 与用户操作的 POST。它们的响应可能乱序到达，晚到的旧轮询会把
   操作后的新状态覆盖回去，甚至因为"已不在忙"而停掉轮询，界面从此卡住。
   规则：每次发请求前 begin() 取一个代次，响应回来先 isCurrent(token)，
   不是最新的就丢弃；用户操作开始时也 begin()，让在途的轮询全部作废。
   invalidate() 用于卡片卸载或需要整体作废时。 */
(function (global) {  // module: 14-task-state.js
  function createLatest() {
    var generation = 0;
    return Object.freeze({
      begin: function () { generation += 1; return generation; },
      isCurrent: function (token) { return token === generation; },
      invalidate: function () { generation += 1; }
    });
  }

  global.MEFinderTaskState = Object.freeze({
    createLatest: createLatest
  });
}(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this)));
