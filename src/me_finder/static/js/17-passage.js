/* 以 MathML 排版原文，复制时转成纯文本，定位仍使用 LaTeX 源文本。 */
(function (global) {  // module: 17-passage.js
  function plainMath(node) {
    const type = node.localName;
    if (['mi', 'mn', 'mo', 'mtext'].includes(type) && !node.childElementCount) return node.textContent;
    if (type === 'mspace') return ' ';
    if (type === 'semantics') return plainMath(node.firstElementChild);
    if (type === 'mtable' && node.getAttribute('width') === '100%') {
      // KaTeX 的独立公式编号布局：空白、公式、空白、编号。
      const cells = node.children[0]?.children;
      if (node.childElementCount !== 1 || cells?.length !== 4 ||
          cells[0].textContent || cells[2].textContent) return null;
      const body = plainMath(cells[1]);
      const label = plainMath(cells[3]);
      return body === null || label === null ? null : body + ' ' + label;
    }
    if (!['mrow', 'mstyle', 'mtd', 'msub', 'msup', 'msubsup', 'mfrac', 'msqrt', 'mroot', 'mo'].includes(type)) {
      return null;
    }
    const parts = Array.from(node.children, plainMath);
    if (parts.includes(null)) return null;
    const [base, script, upper] = parts;
    const group = text => /^(?:[\p{L}\p{N}]*|\d+\.\d+)$/u.test(text) ? text : '(' + text + ')';
    const lower = text => /^[\p{L}\p{N}]+$/u.test(text) ? text : '_' + group(text);
    const power = text => text === '∘' ? '°' : '^' + group(text);
    if (type === 'msub') return base + lower(script);
    // KaTeX can attach a power to the closing delimiter of an existing group.
    if (type === 'msup') return (node.firstElementChild.localName === 'mo' ? base : group(base)) + power(script);
    if (type === 'msubsup') return base + lower(script) + power(upper);
    if (type === 'mfrac') return group(base) + '/' + group(script);
    if (type === 'msqrt') return '√(' + parts.join('') + ')';
    if (type === 'mroot') return group(script) + '√(' + base + ')';
    return parts.join('');
  }

  function formulaAt(node) {
    return (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement).closest('[data-formula-source]');
  }

  function plainRangeText(range) {
    const start = formulaAt(range.startContainer);
    const end = formulaAt(range.endContainer);
    range = range.cloneRange();
    if (start) range.setStartBefore(start);
    if (end) range.setEndAfter(end);
    const content = range.cloneContents();
    content.querySelectorAll('[data-formula-source]').forEach(formula => {
      const text = plainMath(formula.querySelector('semantics'));
      // 不把暂不能线性表示的结构压成可能误读的文本。
      formula.replaceWith(text === null ? formula.dataset.formulaSource : text);
    });
    return content.textContent.replace(/[₀-₉]/g, digit => String(digit.charCodeAt(0) - 0x2080));
  }

  function appendPassage(host, text, from = 0, to = text.length, matchStart = to, matchEnd = to) {
    function appendPlain(start, end) {
      start = Math.max(from, start);
      end = Math.min(to, end);
      if (start >= end) return;
      const markStart = Math.max(start, matchStart);
      const markEnd = Math.min(end, matchEnd);
      if (markStart >= markEnd) {
        host.append(text.slice(start, end));
        return;
      }
      const mark = document.createElement('mark');
      mark.textContent = text.slice(markStart, markEnd);
      host.append(text.slice(start, markStart), mark, text.slice(markEnd, end));
    }

    let cursor = from;
    // 不跨行配对单美元符号。尾部空白或紧跟数字的美元符号通常是下一个金额的开头。
    const math = /\\[$\\]|\\\[(?<display>[\s\S]+?)\\\]|\\\((?<inline>[^\r\n]+?)\\\)|\$\$(?<displayDollar>[^$]+)\$\$|\$(?<inlineDollar>(?=\S)(?:\\[^\r\n]|[^\\$\r\n])*?(?:\\[^\r\n]|[^\\$\s]))\$(?![\d$])/g;
    for (const match of text.matchAll(math)) {
      const start = match.index;
      const end = start + match[0].length;
      if (start >= to) break;
      if (end <= from) continue;
      const {display, inline, displayDollar, inlineDollar} = match.groups;
      const tex = display ?? inline ?? displayDollar ?? inlineDollar;
      // 保留误配进来的普通散文，如“$10 至 $20”一类金额。
      if (!tex || (inlineDollar && !/[\\_^=+*/<>-]/.test(tex) && !/^[\w.,()]+$/.test(tex))) continue;
      const formula = document.createElement('span');
      try {
        // 与 KaTeX auto-render 一样，按分隔符选择独立公式模式，以支持 \tag。
        global.katex.render(tex, formula, {
          output: 'mathml', displayMode: display !== undefined || displayDollar !== undefined,
          trust: false, maxSize: 10
        });
      } catch (error) {
        if (!(error instanceof global.katex.ParseError)) throw error;
        continue;  // 无法排版的公式按解析文本显示。
      }
      // MinerU 有时把下标单独输出，如 SnO $_{2}$。
      const attached = tex.startsWith('_') && /[a-z\d)] $/i.test(text.slice(start - 2, start));
      const visibleStart = attached ? start - 1 : start;
      formula.dataset.formulaSource = text.slice(visibleStart, end);
      appendPlain(cursor, visibleStart);
      if (matchStart < matchEnd && start < matchEnd && end > matchStart) {
        const mark = document.createElement('mark');
        mark.appendChild(formula);
        host.appendChild(mark);
      } else {
        host.appendChild(formula);
      }
      cursor = end;
    }
    appendPlain(cursor, to);
  }

  document.addEventListener('copy', function(event) {
    const selection = global.getSelection();
    if (selection.isCollapsed) return;
    const range = selection.getRangeAt(0);
    const ancestor = range.commonAncestorContainer;
    const element = ancestor.nodeType === Node.ELEMENT_NODE ? ancestor : ancestor.parentElement;
    if (!element.closest('.detail-hit, .result-snippet, .detail-context-preview, .detail-context-full, ' +
      '.materials-text-preview, .materials-quote')) return;
    event.clipboardData.setData('text/plain', plainRangeText(range));
    event.preventDefault();
  });

  global.MEFinder.appendPassage = appendPassage;
  global.MEFinder.plainRangeText = plainRangeText;
}(typeof window !== 'undefined' ? window : globalThis));
