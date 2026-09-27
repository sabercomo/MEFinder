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
  return document.body.innerText.slice(0, 400);
})()
