/* 供 node 渲染 Vue 组件的最小 DOM：只实现 Vue 3 runtime-dom 实际调用的接口。
   目的：让 CI 在没有浏览器、不引入 jsdom 的情况下，真正执行随包 Vue 渲染设置页组件，
   断言渲染出的文字、属性与点击行为。不追求规范完整；Vue 用到新接口时这里会直接报错。
   限制：没有 innerHTML，Vue 模板里不能出现 `&`（含 `&&` 与实体），逻辑请放进 setup 函数。
   用法：require 本文件得到 {install, byId, text, click, change}；install() 在加载 Vue 前调用。 */
'use strict';

const BOOLEAN_PROPS = ['hidden', 'disabled', 'checked'];
const STRING_PROPS = ['id', 'title', 'value', 'type', 'name'];

class Node {
  constructor(nodeType) {
    this.nodeType = nodeType;
    this.parentNode = null;
    this.childNodes = [];
  }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  insertBefore(child, ref) {
    if (child.parentNode) child.parentNode.removeChild(child);
    const index = ref ? this.childNodes.indexOf(ref) : -1;
    if (index < 0) this.childNodes.push(child); else this.childNodes.splice(index, 0, child);
    child.parentNode = this;
    return child;
  }
  appendChild(child) { return this.insertBefore(child, null); }
  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index >= 0) this.childNodes.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  get textContent() {
    if (this.nodeType === 3) return this.nodeValue;
    if (this.nodeType === 8) return '';
    return this.childNodes.map(child => child.textContent).join('');
  }
  set textContent(value) {
    if (this.nodeType === 3 || this.nodeType === 8) { this.nodeValue = String(value); return; }
    this.childNodes.forEach(child => { child.parentNode = null; });
    this.childNodes = [];
    if (value !== '' && value != null) this.appendChild(new Text(String(value)));
  }
}

class Text extends Node {
  constructor(value) { super(3); this.nodeValue = value; }
}

class Comment extends Node {
  constructor(value) { super(8); this.nodeValue = value; }
}

class Element extends Node {
  constructor(tagName) {
    super(1);
    this.tagName = tagName.toUpperCase();
    this.attributes = {};
    this.listeners = {};
    this.style = {width: '', height: '', display: '', setProperty(name, value) { this[name] = value; }};
    BOOLEAN_PROPS.forEach(name => { this[name] = false; });
    STRING_PROPS.forEach(name => { this[name] = ''; });
    this.className = '';
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  hasAttribute(name) { return name in this.attributes; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  removeEventListener(type, handler) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== handler);
  }
  get classList() {
    const element = this;
    const names = () => element.className.split(/\s+/).filter(Boolean);
    return {contains: name => names().includes(name)};
  }
  querySelectorAll(selector) {
    // 只支持 `tag[name="x"]` 与 `.class` 两种写法，够组件内部对齐单选框用。
    const out = [];
    const walk = node => node.childNodes.forEach(child => {
      if (child.nodeType === 1 && matches(child, selector)) out.push(child);
      if (child.nodeType === 1) walk(child);
    });
    walk(this);
    return out;
  }
}

function matches(element, selector) {
  const attr = /^(\w+)\[(\w+)="([^"]+)"\]$/.exec(selector);
  if (attr) return element.tagName === attr[1].toUpperCase() && element[attr[2]] === attr[3];
  if (selector[0] === '.') return element.classList.contains(selector.slice(1));
  return element.tagName === selector.toUpperCase();
}

function install(global) {
  const body = new Element('body');
  global.document = {
    body,
    createElement: tag => new Element(tag),
    createElementNS: (_ns, tag) => new Element(tag),
    createTextNode: value => new Text(value),
    createComment: value => new Comment(value),
    getElementById: id => byId(body, id),
    querySelector: () => null,
    querySelectorAll: selector => body.querySelectorAll(selector)
  };
  global.window = global;
  global.Element = Element;
  global.SVGElement = class SVGElement extends Element {};
  return body;
}

function byId(root, id) {
  for (const child of root.childNodes) {
    if (child.nodeType !== 1) continue;
    if (child.id === id) return child;
    const found = byId(child, id);
    if (found) return found;
  }
  return null;
}

function text(root, id) {
  const element = byId(root, id);
  return element ? element.textContent : null;
}

function dispatch(element, type) {
  // Vue 的事件调用器会忽略时间戳不晚于绑定时刻的事件；给一个足够晚的时间戳。
  const event = {type, target: element, _vts: Date.now() + 60000,
    stopPropagation() {}, preventDefault() {}};
  (element.listeners[type] || []).forEach(handler => handler(event));
}

function click(element) { dispatch(element, 'click'); }

function change(element) {
  // 模拟浏览器点单选框：同名单选框只留一个选中，再派发 change。
  if (element.type === 'radio') {
    let root = element;
    while (root.parentNode) root = root.parentNode;
    root.querySelectorAll('input[name="' + element.name + '"]').forEach(input => { input.checked = false; });
    element.checked = true;
  }
  dispatch(element, 'change');
}

module.exports = {install, byId, text, click, change};
