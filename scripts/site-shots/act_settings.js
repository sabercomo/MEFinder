// Open the settings page with a demo Zotero collection tree.
// The live overview lists the capturing machine's own Zotero collections, which
// must not reach the public site, so /api/zotero/overview is rewritten in-page.
// Pick the settings section via shoot.mjs clickLabels, e.g. "Zotero" or "引文格式".
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const demo = [
    { key: "DEMO0001", name: "黑格尔", parent: null, item_count: 4, linked_count: 0, unsynced_count: 4 },
    { key: "DEMO0002", name: "法哲学原理", parent: "DEMO0001", item_count: 2, linked_count: 0, unsynced_count: 2 },
    { key: "DEMO0003", name: "马克思", parent: null, item_count: 6, linked_count: 0, unsynced_count: 6 },
    { key: "DEMO0004", name: "法兰克福学派", parent: null, item_count: 5, linked_count: 0, unsynced_count: 5 },
    { key: "DEMO0005", name: "译本对照", parent: null, item_count: 3, linked_count: 0, unsynced_count: 3 },
  ];
  const orig = window.fetch;
  window.fetch = async (u, opt) => {
    const url = String((u && u.url) || u);
    const r = await orig(u, opt);
    if (url.includes("/api/zotero/overview")) {
      const j = await r.clone().json();
      j.collections = demo;
      return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return r;
  };
  const click = (t) => {
    const el = [...document.querySelectorAll("button,[role=tab],a,span,div")]
      .find((x) => x.textContent.trim() === t && x.offsetParent !== null);
    if (!el) return false;
    (el.closest("button,[role=tab],a") || el).click();
    return true;
  };
  await sleep(800);
  const opened = click("设置"); await sleep(1200);
  // Leave the default section so the target section re-fetches through the stub.
  click("外观"); await sleep(600);
  return { opened };
})()
