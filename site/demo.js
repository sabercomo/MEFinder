/* Hero demo: a real search over four embedded passages.
 * The modes mirror the app's own semantics (自动 / 精确 / 忽略标点 / 模糊);
 * the score is computed here, so it is a similarity figure of this page's
 * implementation, not a claim about the desktop engine. */
(() => {
  "use strict";

  const PASSAGES = [
    {
      doc: "法哲学原理：或自然法和国家学纲要",
      meta: "黑格尔 · 范杨、张企泰译",
      page: "序言第16页",
      pageResolved: true,
      text: "关于教导世界应该怎样，……无论如何哲学总是来得太迟。哲学作为有关世界的思想，要直到现实结束其形成过程并完成其自身之后，才会出现。……当哲学用灰色的颜料绘成灰色的图画的时候，这一生活形态就变老了。对灰色绘成灰色，不能使生活形态变得年青，而只能作为认识的对象。密纳发的猫头鹰要等黄昏到来，才会起飞。",
    },
    {
      doc: "法哲学原理：或自然法和国家学纲要",
      meta: "黑格尔 · 第161节",
      page: "第201页",
      pageResolved: true,
      text: "婚姻作为直接伦理关系首先包括自然生活的环节。因为伦理关系是实体性的关系，所以它包括生活的全部，亦即类及其生命过程的现实。但其次，自然性别的统一只是内在的或自在地存在的，正因为如此，它在它的实存中纯粹是外在的统一，这种统一在自我意识中就转变为精神的统一，自我意识的爱。",
    },
    {
      doc: "Who's Afraid of Gender?",
      meta: "Judith Butler",
      page: "第8页",
      pageResolved: true,
      text: "In taking aim at gender, some proponents of the anti-gender movement claim to be defending not just family values but values themselves, not just a way of life but life itself.",
    },
    {
      doc: "谁在害怕性别",
      meta: "朱迪斯·巴特勒 · 中译本",
      page: "页码尚未解析",
      pageResolved: false,
      text: "导论 社会性别意识形态和对破坏的恐惧",
    },
  ];

  // Hyphen goes last: a bare "-" between two CJK punctuation marks would be read
  // as a character range, and －-（ is a descending (invalid) range.
  const PUNCT_CHARS = "\\s，。、；：？！…—－（）()《》〈〉「」『』“”\"'‘’·．.,;:?!|-";
  const PUNCT = new RegExp("[" + PUNCT_CHARS + "]", "g");
  const PUNCT_CHAR = new RegExp("[" + PUNCT_CHARS + "]");
  const strip = (s) => s.replace(PUNCT, "");
  const lower = (s) => s.toLowerCase();

  // Character-bigram Dice coefficient — language-agnostic, works on CJK runs.
  function bigrams(s) {
    const out = [];
    for (let i = 0; i < s.length - 1; i += 1) out.push(s.slice(i, i + 2));
    if (out.length === 0 && s.length === 1) out.push(s);
    return out;
  }
  function dice(a, b) {
    const A = bigrams(a);
    const B = bigrams(b);
    if (!A.length || !B.length) return 0;
    const bag = new Map();
    for (const g of A) bag.set(g, (bag.get(g) || 0) + 1);
    let hit = 0;
    for (const g of B) {
      const n = bag.get(g);
      if (n) { hit += 1; bag.set(g, n - 1); }
    }
    return (2 * hit) / (A.length + B.length);
  }

  // Recall of the query's bigrams inside a window of roughly the query's length.
  // Plain Dice would rather return a short window that overlaps a few bigrams;
  // recall plus a length tie-break returns the whole phrase the reader typed.
  function bestWindow(hay, needle) {
    const n = needle.length;
    if (!n) return { start: 0, end: 0, score: 0 };
    if (hay.length < n) return { start: 0, end: hay.length, score: dice(hay, needle) };
    const qSet = [...new Set(bigrams(needle))];
    let best = { start: 0, end: n, score: -1, off: n };
    for (let off = 0; off <= 2; off += 1) {
      for (const size of off === 0 ? [n] : [n - off, n + off]) {
        if (size < 1 || size > hay.length) continue;
        for (let i = 0; i + size <= hay.length; i += 1) {
          const win = hay.slice(i, i + size);
          const bag = new Map();
          for (const g of bigrams(win)) bag.set(g, (bag.get(g) || 0) + 1);
          let hit = 0;
          for (const g of qSet) {
            const c = bag.get(g);
            if (c) { hit += 1; bag.set(g, c - 1); }
          }
          const score = hit / qSet.length;
          if (score > best.score + 1e-9 || (Math.abs(score - best.score) <= 1e-9 && off < best.off)) {
            best = { start: i, end: i + size, score, off };
          }
        }
      }
    }
    return best;
  }

  // Similarity shown to the reader: how much of the two strings aligns character
  // by character. Bigram recall picks the window; this scores it.
  function lcsRatio(a, b) {
    if (!a.length || !b.length) return 0;
    let prev = new Array(b.length + 1).fill(0);
    for (let i = 1; i <= a.length; i += 1) {
      const cur = new Array(b.length + 1).fill(0);
      for (let j = 1; j <= b.length; j += 1) {
        cur[j] = a[i - 1] === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], cur[j - 1]);
      }
      prev = cur;
    }
    return (2 * prev[b.length]) / (a.length + b.length);
  }

  const FUZZY_FLOOR = 0.34;

  function searchOne(passage, query, mode) {
    const q = lower(query.trim());
    if (!q) return null;

    if (mode === "exact") {
      const i = lower(passage.text).indexOf(q);
      if (i < 0) return null;
      return { start: i, end: i + q.length, score: 1, modeLabel: "精确" };
    }

    if (mode === "ignore-punct") {
      const hay = lower(strip(passage.text));
      const needle = lower(strip(q));
      const i = hay.indexOf(needle);
      if (i < 0) return null;
      return { punct: true, punctStart: i, punctEnd: i + needle.length, score: 0.95, modeLabel: "忽略标点" };
    }

    if (mode === "fuzzy") {
      const w = bestWindow(lower(passage.text), q);
      if (w.score < FUZZY_FLOOR) return null;
      return {
        start: w.start, end: w.end, modeLabel: "模糊",
        score: lcsRatio(q, lower(passage.text).slice(w.start, w.end)),
      };
    }

    // auto: exact → ignore-punct → fuzzy, keep the first that lands
    const exact = searchOne(passage, query, "exact");
    if (exact) return exact;
    const np = searchOne(passage, query, "ignore-punct");
    if (np) return np;
    return searchOne(passage, query, "fuzzy");
  }

  // Map a punctuation-stripped span back onto the original text for display.
  function punctSpanToOriginal(text, start, end) {
    const kept = [];
    for (let i = 0; i < text.length; i += 1) if (!PUNCT_CHAR.test(text[i])) kept.push(i);
    const a = kept[start];
    const b = kept[Math.min(end, kept.length) - 1];
    if (a === undefined) return { start: 0, end: 0 };
    return { start: a, end: b === undefined ? text.length : b + 1 };
  }

  const out = document.getElementById("demo-out");
  const form = document.getElementById("demo-form");
  const input = document.getElementById("demo-q");
  const chips = [...document.querySelectorAll(".chip")];
  let mode = "auto";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function render(query) {
    out.textContent = "";
    const results = PASSAGES
      .map((p) => ({ p, hit: searchOne(p, query, mode) }))
      .filter((r) => r.hit)
      .sort((a, b) => b.hit.score - a.hit.score);

    if (!query.trim()) {
      out.append(el("p", "demo-empty", "输入一段引文，或记得的任意片段。"));
      return;
    }
    if (!results.length) {
      out.append(el("p", "demo-empty", "这四段里没有命中。可以换模糊模式，或者只记得几个词时也试试——真实软件会告诉你「没找到」，不会编一个答案。"));
      return;
    }

    const count = el("p", "hit-mode", `找到 ${results.length} 条候选 · ${mode === "auto" ? "自动" : chips.find((c) => c.dataset.mode === mode)?.textContent}模式`);
    results.forEach(({ p, hit }) => {
      const span = hit.punct ? punctSpanToOriginal(p.text, hit.punctStart, hit.punctEnd) : { start: hit.start, end: hit.end };
      const card = el("div", "hit");

      const top = el("div", "hit-top");
      top.append(el("span", "hit-doc", p.doc));
      top.append(el("span", null, p.meta));
      top.append(el("span", "hit-score", Math.round(hit.score * 100) + "%"));
      top.append(el("span", "hit-page" + (p.pageResolved ? "" : " is-none"), p.page));
      card.append(top);

      const from = Math.max(0, span.start - 42);
      const body = el("p", "hit-body");
      if (from > 0) body.append("……");
      body.append(p.text.slice(from, span.start));
      const mark = el("mark", null, p.text.slice(span.start, span.end));
      body.append(mark);
      body.append(p.text.slice(span.end, span.end + 52));
      if (span.end + 52 < p.text.length) body.append("……");
      card.append(body);
      out.append(card);
    });
    out.prepend(count);
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); render(input.value); });
  input.addEventListener("input", () => render(input.value));
  chips.forEach((c) => c.addEventListener("click", () => {
    chips.forEach((x) => x.classList.toggle("is-on", x === c));
    mode = c.dataset.mode;
    render(input.value);
  }));

  render(input.value);
})();

/* Platform-aware primary CTA — the label and target follow the visitor's OS,
 * falling back to the releases page when the platform is unknown. */
(() => {
  "use strict";
  const cta = document.getElementById("cta-dl");
  if (!cta) return;
  const ua = navigator.userAgent;
  // iPhone reports "like Mac OS X" and iPadOS reports "Macintosh"; neither can
  // install the app, so leave them on the neutral label and release page.
  const isIOS = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  if (isIOS) return;
  if (/Windows/.test(ua)) {
    cta.textContent = "下载 Windows 版";
    if (window.MEF_RELEASE) cta.href = window.MEF_RELEASE.assetUrl("windows-setup.exe");
  } else if (/Macintosh|Mac OS X/.test(ua)) {
    // The browser cannot tell Apple silicon from Intel, so send Mac users to the
    // release page and let them pick the chip rather than guessing wrong.
    cta.textContent = "下载 macOS 版";
  }
})();
