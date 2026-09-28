(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const inp = [...document.querySelectorAll("input")].find((i) => /搜索引文/.test(i.placeholder || ""));
  if (!inp) return "no search input";
  inp.focus();
  inp.value = "密涅瓦的猫头鹰";
  inp.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(200);
  const btn = [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "搜索");
  btn?.click();
  await sleep(2500);
  // Drop focus so the capture shows no focus ring or caret in the query box.
  inp.blur();
  // Select the preface hit (the one whose citation page is 序言第15—16页) and
  // bring its highlighted sentence to the middle of the context panel.
  const pick = [...document.querySelectorAll("span,div")].find((x) => x.textContent.trim() === "序言第15—16页");
  (pick?.closest("button,a,[role=option],li,article,.result-card") || pick)?.click();
  await sleep(1200);
  const marks = [...document.querySelectorAll("mark")].filter((m) => m.offsetParent !== null);
  marks[marks.length - 1]?.scrollIntoView({ block: "center" });
  await sleep(400);
  return document.body.innerText.slice(0, 400);
})()
