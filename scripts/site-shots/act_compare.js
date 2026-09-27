(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const click = (t) => { const e = [...document.querySelectorAll("button,a,[role=tab],li,div,span")].find(x => x.textContent.trim() === t && x.offsetParent !== null); if (e) { (e.closest("button,a,[role=tab]") || e).click(); } return !!e; };
  click("译本对照"); await sleep(1800);
  const hit = click("对照阅读"); await sleep(4500);
  return JSON.stringify({ clicked: hit, cols: document.body.innerText.slice(0, 300) });
})()
