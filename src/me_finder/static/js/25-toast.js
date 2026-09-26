/* ═══ Toasts ═══ */
const TOAST_TONES = ['success', 'danger', 'warning', 'info'];
const TOAST_STACK_LIMIT = 3;
const TOAST_ICONS = {
  success: [['circle', {cx: '9', cy: '9', r: '7.2'}], ['path', {d: 'm5.8 9.2 2.2 2.2 4.2-4.4'}]],
  danger: [['circle', {cx: '9', cy: '9', r: '7.2'}], ['path', {d: 'M9 5.4v4.4'}], ['path', {d: 'M9 12.4h.01'}]],
  warning: [['path', {d: 'M9 2.4 1.6 15.4h14.8Z'}], ['path', {d: 'M9 7v3.6'}], ['path', {d: 'M9 12.9h.01'}]],
  info: [['circle', {cx: '9', cy: '9', r: '7.2'}], ['path', {d: 'M9 8.4v4.2'}], ['path', {d: 'M9 5.6h.01'}]]
};


function dismissToast(item, immediate) {
  if (!item || item.dataset.dismissing === '1') return;
  clearTimeout(Number(item.dataset.timer));
  if (immediate) {
    item.remove();
    return;
  }
  item.dataset.dismissing = '1';
  item.classList.add('is-leaving');
  setTimeout(function() { item.remove(); }, 200);
}

function showToast(message, tone) {
  var stack = document.getElementById('toast-stack');
  var text = String(message == null ? '' : message).trim();
  if (!stack || !text) return null;
  var variant = TOAST_TONES.indexOf(tone) >= 0 ? tone : 'info';
  // 连续提示互相叠放，而不是后一条把前一条顶掉。
  while (stack.children.length >= TOAST_STACK_LIMIT) {
    dismissToast(stack.firstElementChild, true);
  }
  var item = document.createElement('div');
  item.className = 'toast toast--' + variant;
  var icon = document.createElement('span');
  icon.className = 'toast-icon';
  var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  [['viewBox', '0 0 18 18'], ['fill', 'none'], ['stroke', 'currentColor'],
    ['stroke-width', '1.7'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round'],
    ['aria-hidden', 'true']].forEach(function(attribute) {
    svg.setAttribute(attribute[0], attribute[1]);
  });
  TOAST_ICONS[variant].forEach(function(shape) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', shape[0]);
    Object.keys(shape[1]).forEach(function(name) { node.setAttribute(name, shape[1][name]); });
    svg.appendChild(node);
  });
  icon.appendChild(svg);
  item.appendChild(icon);
  var label = document.createElement('span');
  label.className = 'toast-text';
  label.textContent = text;
  item.appendChild(label);
  stack.appendChild(item);
  item.dataset.timer = String(setTimeout(function() { dismissToast(item); }, toastDuration(text)));
  return item;
}
