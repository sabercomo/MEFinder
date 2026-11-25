/* 以 MathML 排版原文，复制时转成纯文本，定位仍使用 LaTeX 源文本。 */
(function (global) {  // module: 17-passage.js
  const doubleStruck = Array.from('𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ');
  // Presentation MathML operators → combining mark, or an explicit group in linear text.
  const accents = new Map([
    ['˙', ['\u0307', 'dot']], ['¨', ['\u0308', 'ddot']], ['^', ['\u0302', 'hat']],
    ['~', ['\u0303', 'tilde']], ['ˉ', ['\u0305', 'overline']], ['‾', ['\u0305', 'overline']],
    ['⃗', ['\u20d7', 'vec']],
  ]);

  function isIdentifier(node) {
    if (node?.localName === 'mi' || node?.localName === 'mtext') return /^\p{L}+$/u.test(node.textContent);
    // KaTeX emits an upright name as a row of normal letters, not a product.
    return node?.localName === 'mrow' && node.hasChildNodes() && Array.from(node.childNodes).every(letter =>
      letter.localName === 'mi' && letter.getAttribute('mathvariant') === 'normal' && isIdentifier(letter));
  }

  function plainAccent(node) {
    const [expression, operator] = node.children;
    const accent = node.getAttribute('accent') === 'true' && node.children.length === 2 &&
      operator.localName === 'mo' ? accents.get(operator.textContent) : null;
    if (!accent) return null;
    const text = plainMath(expression);
    // A numeric accent can be an OCR error. Preserve it without asserting a transcription.
    if (!text || /^[\p{N}.,\s]+$/u.test(text)) return null;
    const [mark, label] = accent;
    return /^[\p{L}\p{Sm}]\p{M}*$/u.test(text) ? text + mark : label + '(' + text + ')';
  }

  function rowTerms(node) {
    if (['mtd', 'mrow', 'mstyle'].includes(node.localName)) return Array.from(node.children).flatMap(rowTerms);
    return node.localName === 'mspace' ? [] : [node];
  }

  function plainTable(node) {
    // Row/cell ownership matters: merging cells or dropping fences changes the expression.
    if (['rowlines', 'columnlines', 'frame'].some(attr =>
      (node.getAttribute(attr) || '').split(/\s+/).some(value => value && value !== 'none'))) return null;
    const grid = Array.from(node.children, row => Array.from(row.children));
    if (!grid.length || Array.from(node.children).some(row => row.localName !== 'mtr') ||
        grid.some(row => !row.length || row.some(cell => cell.localName !== 'mtd' ||
          ['rowspan', 'columnspan'].some(attr => cell.hasAttribute(attr) && cell.getAttribute(attr) !== '1')))) return null;
    if (node.getAttribute('width') === '100%') {
      // KaTeX wraps a numbered equation in a separate one-row layout.
      const [cells] = grid;
      if (grid.length !== 1 || cells.length !== 4 || cells[0].textContent || cells[2].textContent) return null;
      const body = plainMath(cells[1]);
      const label = plainMath(cells[3]);
      return body === null || label === null ? null : body + ' ' + label;
    }
    const rows = grid.map(row => row.map(plainMath));
    if (rows.some(row => row.includes(null))) return null;
    if (node.getAttribute('columnalign') === 'right left' && node.getAttribute('columnspacing') === '0em') {
      return rows.map(row => row.join('')).join('\n');
    }
    if (rows.some(row => row.length !== rows[0].length)) return null;
    // An unfenced, left-aligned array can be a continued equation, not a matrix.
    // Require a top-level relation and explicit continuation operators in the remaining rows.
    if (rows.length > 1 && node.parentElement.localName === 'semantics' &&
        /^left(?: left)*$/.test(node.getAttribute('columnalign') || '')) {
      const [first, ...rest] = grid.map(row => row.flatMap(rowTerms));
      const hasOp = (term, operators) => term?.localName === 'mo' && operators.includes(term.textContent);
      if (first.some(term => hasOp(term, ['=', '≠', '<', '>', '≤', '≥', '≈', '≡'])) &&
          rest.every(row => row.length > 1 && hasOp(row[0], ['+', '−', '-']))) {
        return rows.map(row => row.map(cell => cell.trim()).filter(Boolean).join(' ')).join('\n');
      }
    }
    const open = node.previousElementSibling;
    const close = node.nextElementSibling;
    const hasFence = item => item?.localName === 'mo' && item.getAttribute('fence') === 'true';
    const fenced = hasFence(open) && hasFence(close) && open.textContent && close.textContent &&
      node.parentElement.childElementCount === 3;
    if (!fenced && (hasFence(open) || hasFence(close))) return null;
    // Nested expressions containing a separator get their own group.
    const cells = rows.map(row => '[' + row.map(cell => /[,\r\n]/.test(cell) ? '(' + cell + ')' : cell).join(', ') + ']').join(', ');
    return fenced ? cells : '[' + cells + ']';
  }

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
    if (type === 'mover') return plainAccent(node);
    if (type === 'mtable') return plainTable(node);
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
    const nodes = Array.from(node.childNodes);
    const parts = nodes.map(plainMath);
    if (parts.includes(null)) return null;
    const [base, script, upper] = parts;
    const group = text => /^(?:[\p{L}]\d*|\d+(?:\.\d+)?)$/u.test(text) ? text : '(' + text + ')';
    const lower = text => /^\d+$/.test(text) ? text : '_' + group(text);
    const power = text => {
      if (text === '∘') return '°';
      // A lone sign is a charge, including the parenthesized form from MinerU.
      text = text.replace(/^\(([+−-])\)$/, '$1');
      if (!/^(?:[+−-]\d*|\d+)$/.test(text)) return '^' + group(text);
      return Array.from(text, char => char === '+' ? '⁺' : /[−-]/.test(char) ? '⁻' : '⁰¹²³⁴⁵⁶⁷⁸⁹'[Number(char)]).join('');
    };
    if (type === 'msub') return base + lower(script);
    // KaTeX can attach a power to the closing delimiter of an existing group.
    if (type === 'msup') {
      const exponent = power(script);
      if (base === '') return exponent.startsWith('^') ? null : exponent; // e.g. {}^1O₂
      const operand = nodes[0];
      const fenced = operand.localName === 'mrow' &&
        operand.firstElementChild?.getAttribute('fence') === 'true' &&
        operand.lastElementChild?.getAttribute('fence') === 'true' &&
        Array.from(operand.children).filter(child => child.getAttribute('fence') === 'true').length === 2;
      return (operand.localName === 'mo' || isIdentifier(operand) || fenced ? base : group(base)) + exponent;
    }
    if (type === 'msubsup') return base + lower(script) + power(upper);
    if (type === 'munder' || type === 'munderover') {
      if (base !== '∑') return null;
      return base + '_' + group(script) + (type === 'munderover' ? power(upper) : '') + ' ';
    }
    if (type === 'mfrac') return group(base) + '/' + group(script);
    if (type === 'msqrt') return '√(' + parts.join('') + ')';
    if (type === 'mroot') return group(script) + '√(' + base + ')';
    if (type === 'mrow') {
      for (let index = 1; index + 1 < nodes.length; index++) {
        const multiply = nodes[index];
        const powerOfTen = nodes[index + 1];
        const next = nodes[index + 2];
        if (multiply.localName !== 'mo' || parts[index] !== '×' || nodes[index - 1].localName !== 'mn' ||
            powerOfTen.localName !== 'msup' || !/^10[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹]+$/.test(parts[index + 1])) continue;
        // Separate a scientific coefficient from an adjacent symbol without guessing its unit.
        parts[index] = ' × ';
        if (isIdentifier(next) || (['msub', 'msup', 'msubsup'].includes(next?.localName) &&
                                   isIdentifier(next.firstElementChild))) parts[index + 1] += ' ';
      }
    }
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

  function looksLikePaddedMath(tex) {
    if (/\\[A-Za-z]|[_^]/.test(tex) ||
        /^[\p{L}\p{N}().+−*/ \t-]+[=<>≤≥≠][\p{L}\p{N}().+−*/=<>≤≥≠ \t-]+$/u.test(tex)) return true;
    // A price per month or a date is not an arithmetic expression.
    return /\p{L}/u.test(tex) &&
      /^(?:\p{L}\d*|\d+(?:\.\d+)?)(?:[ \t]*[+−*/×÷-][ \t]*(?:\p{L}\d*|\d+(?:\.\d+)?))+$/u.test(tex);
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
    // Like markdown-it-dollarmath, do not pair across lines or into the next amount.
    // A dollar run that cannot open a formula is literal; do not retry from inside it.
    const math = /\\[$\\]|\\\[(?<display>[\s\S]+?)\\\]|\\\((?<inline>[^\r\n]+?)\\\)|\$\$(?!\$)(?<displayDollar>[^$]+)\$\$(?!\$)|\$(?!\$)(?:(?<inlineDollar>(?=\S)(?:\\[^\r\n]|[^\\$\r\n])*?(?:\\[^\r\n]|[^\\$\s]))\$(?![\d$])|[ \t]+(?<paddedDollar>(?:\\[^\r\n]|[^\\$\r\n])+?)[ \t]+\$(?!\$|[ \t]*\d))|\$+/g;
    let match;
    while ((match = math.exec(text))) {
      const start = match.index;
      const end = start + match[0].length;
      if (start >= to) break;
      const {display, inline, displayDollar, inlineDollar, paddedDollar} = match.groups;
      const tex = display ?? inline ?? displayDollar ?? inlineDollar ?? paddedDollar?.trim();
      if (!tex) continue;
      // MinerU 有时把普通的参考文献标号包进 $…$。保留原片段用于定位。
      const reference = (inlineDollar || paddedDollar) && /^\[\d+(?:\s*[,–-]\s*\d+)*\]$/.test(tex);
      // OCR may pad real math. A loose pair alone is not evidence (e.g. prices).
      if (paddedDollar && !reference && !looksLikePaddedMath(tex)) {
        math.lastIndex = start + 1; // The rejected closing dollar can open the next formula.
        continue;
      }
      if (end <= from) continue;
      // 保留误配进来的普通散文，如“$10 至 $20”一类金额。
      if (inlineDollar && !reference && !/[\\_^=+*/<>-]/.test(tex) && !/^[\w.,()]+$/.test(tex)) continue;
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
