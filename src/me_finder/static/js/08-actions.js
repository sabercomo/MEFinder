/* C4: 动态元素用 data-action 声明动作；业务模块注册回调，不把数据拼进 JS。 */
(function (global) {  // module: 08-actions.js
  const actions = Object.create(null);

  function register(name, callback) {
    actions[name] = callback;
  }

  document.addEventListener('click', function(event) {
    const target = event.target.closest('[data-action]');
    if (target && actions[target.dataset.action]) {
      actions[target.dataset.action](event, target);
    }
  });

  global.MEFinderActions = {register: register};
}(typeof window !== 'undefined' ? window : globalThis));
