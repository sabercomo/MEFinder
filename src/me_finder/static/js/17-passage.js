/* 以 MathML 排版原文，复制时转成纯文本，定位仍使用 LaTeX 源文本。 */
(function (global) {  // module: 17-passage.js
  const doubleStruck = Array.from('𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ');

  function plainMath(node) {
    // KaTeX 的 MathML DOM 会将 \, 等间距作为文本节点插入。
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    const type = node.localName;
    if (type === 'mi' && node.getAttribute('mathvariant') === 'double-struck' && /^[A-Z]$/.test(node.textContent)) {
      return doubleStruck[node.textContent.charCodeAt(0) - 65];
    }
    if (type === 'mo' && node.textContent === '\u2061') return '';
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
    // KaTeX aligns the two halves of an equation at the relation sign.
    // Other tables (such as matrices) are not a sequence of equation lines.
    if (type === 'mtable' && node.getAttribute('columnalign') === 'right left' &&
        node.getAttribute('columnspacing') === '0em') {
      const rows = Array.from(node.children, row => Array.from(row.children, plainMath));
      return rows.flat().includes(null) ? null : rows.map(row => row.join('')).join('\n');
    }
    if (type === 'munder' && node.children.length === 2) {
      const [expression, label] = node.children;
      if (label.localName === 'mo' && label.textContent === '⏟') {
        const body = plainMath(expression);
        return body === null ? null : '⏟(' + body + ')';
      }
      if (expression.localName === 'munder' && expression.lastElementChild.textContent === '⏟') {
        const body = plainMath(expression.firstElementChild);
        const note = plainMath(label);
        return body === null || note === null ? null : '⏟(' + body + '; ' + note + ')';
      }
    }
    if (!['mrow', 'mstyle', 'mtd', 'msub', 'msup', 'msubsup', 'munder', 'munderover', 'mfrac', 'msqrt', 'mroot', 'mo'].includes(type)) {
      return null;
    }
    const parts = Array.from(node.childNodes, plainMath);
    if (parts.includes(null)) return null;
    const [base, script, upper] = parts;
    const group = text => /^(?:[\p{L}]\d*|\d+(?:\.\d+)?)$/u.test(text) ? text : '(' + text + ')';
    const lower = text => /^\d+$/.test(text) ? text : '_' + group(text);
    const power = text => {
      if (text === '∘') return '°';
      if (!/^[+−-]?\d+$/.test(text)) return '^' + group(text);
      return Array.from(text, char => char === '+' ? '⁺' : /[−-]/.test(char) ? '⁻' : '⁰¹²³⁴⁵⁶⁷⁸⁹'[Number(char)]).join('');
    };
    if (type === 'msub') return base + lower(script);
    // KaTeX can attach a power to the closing delimiter of an existing group.
    if (type === 'msup') return (node.firstElementChild.localName === 'mo' ? base : group(base)) + power(script);
    if (type === 'msubsup') return base + lower(script) + power(upper);
    if (type === 'munder' || type === 'munderover') {
      if (base !== '∑') return null;
      return base + '_' + group(script) + (type === 'munderover' ? power(upper) : '') + ' ';
    }
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
      const math = formula.querySelector('semantics');
      const text = math ? plainMath(math) : formula.textContent;
      // 不把暂不能线性表示的结构压成可能误读的文本。
      formula.replaceWith(text === null ? formula.dataset.formulaSource : text.replace(/[\u00a0\u2000-\u200a\u202f]+/g, ' '));
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
      // MinerU 有时把普通的参考文献标号包进 $…$。保留原片段用于定位。
      const reference = inlineDollar && /^\[\d+(?:\s*[,–-]\s*\d+)*\]$/.test(inlineDollar);
      // 保留误配进来的普通散文，如“$10 至 $20”一类金额。
      if (!tex || (inlineDollar && !reference && !/[\\_^=+*/<>-]/.test(tex) && !/^[\w.,()]+$/.test(tex))) continue;
      const formula = document.createElement('span');
      if (reference) {
        formula.textContent = tex;
      } else {
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
