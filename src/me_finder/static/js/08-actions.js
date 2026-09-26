/* C4: 动态元素用 data-action 声明动作；业务模块注册回调，不把数据拼进 JS。 */
(function (global) {  // module: 08-actions.js
  const actions = Object.create(null);

  function register(name, callback) {
    actions[name] = callback;
  }

  function registerInline(name, callback) {
    register(name, function(event, target) {
      const inlineEvent = new Proxy(event, {get: function(source, key) {
        if (key === 'currentTarget') return target;
        if (key === 'stopPropagation') return function() { source.stopImmediatePropagation(); };
        const value = Reflect.get(source, key, source);
        return typeof value === 'function' ? value.bind(source) : value;
      }});
      if (callback.call(target, inlineEvent, target) === false) event.preventDefault();
    });
  }

  function dispatch(event, attribute, key) {
    if (!event.target.closest) return false;
    const target = event.target.closest('[' + attribute + ']');
    if (!target || !actions[target.dataset[key]]) return false;
    actions[target.dataset[key]](event, target);
    return true;
  }

  document.addEventListener('click', function(event) {
    dispatch(event, 'data-action', 'action');
  });

  ['change', 'input', 'paste', 'submit', 'cancel', 'dblclick'].forEach(function(type) {
    document.addEventListener(type, function(event) {
      const suffix = type.charAt(0).toUpperCase() + type.slice(1);
      dispatch(event, 'data-action-' + type, 'action' + suffix);
    }, type === 'cancel');
  });

  document.addEventListener('keydown', function(event) {
    if (dispatch(event, 'data-action-keydown', 'actionKeydown')) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (!event.target.closest) return;
    const target = event.target.closest('[data-action][role="button"]');
    if (target && actions[target.dataset.action]) {
      event.preventDefault();
      actions[target.dataset.action](event, target);
    }
  });

  global.MEFinderActions = {register: register, registerInline: registerInline};
}(typeof window !== 'undefined' ? window : globalThis));
