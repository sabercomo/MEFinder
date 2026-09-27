// Minimal CDP screenshot harness for the landing-page UI captures.
// Uses Chrome's remote debugging protocol through Node's built-in WebSocket,
// so nothing has to be installed into the project venv.
//
// usage: node shoot.mjs <url> <outPng> <waitForMs> [actionJsFile] [clickLabels]
//   clickLabels: comma-separated button texts to click in order, leaf-matched.

import { readFile, writeFile } from "node:fs/promises";

const [, , url, outPath, waitForRaw, actionFile, clickLabels, widthRaw, heightRaw, fullRaw] = process.argv;
const waitFor = Number(waitForRaw ?? 1500);
const VW = Number(widthRaw ?? 1440);
const VH = Number(heightRaw ?? 900);
const full = fullRaw === "full";
const DEBUG_PORT = 9222;

const targets = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/new?${encodeURIComponent(url)}`, { method: "PUT" })).json();
const ws = new WebSocket(targets.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

let seq = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
  }
};
const send = (method, params = {}) =>
  new Promise((res, rej) => { const id = ++seq; pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params })); });

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: VW, height: VH, deviceScaleFactor: 2, mobile: false,
});

await new Promise((res) => {
  const onMsg = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.method === "Page.loadEventFired") { ws.removeEventListener("message", onMsg); res(); }
  };
  ws.addEventListener("message", onMsg);
  setTimeout(res, 12000);
});

if (actionFile) {
  const code = await readFile(actionFile, "utf8");
  const r = await send("Runtime.evaluate", { expression: code, awaitPromise: true, returnByValue: true });
  console.log("action:", JSON.stringify(r?.result?.value ?? r));
}

for (const label of (clickLabels ?? "").split(",").filter(Boolean)) {
  if (/^[\d.]+$/.test(label)) {
    const code = `(async () => { const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
      for (let i = 0; i <= ${label}; i += 0.05) { window.scrollTo(0, i * document.body.scrollHeight); await sleep(90); }
      window.scrollTo(0, ${label} * document.body.scrollHeight); await sleep(700);
      return "scroll ${label}"; })()`;
    const r = await send("Runtime.evaluate", { expression: code, awaitPromise: true, returnByValue: true });
    console.log("nav:", r?.result?.value);
    continue;
  }
  const code = `(async () => {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const want = ${JSON.stringify(label)};
    const els = [...document.querySelectorAll("button,a,[role=tab],li,span,div")];
    const hit = els.find((x) => x.textContent.trim() === want && x.offsetParent !== null)
      || els.find((x) => x.textContent.trim() === want);
    if (!hit) return "not-found: " + want;
    (hit.closest("button,a,[role=tab]") || hit).click();
    await sleep(1400);
    return "clicked: " + want;
  })()`;
  const r = await send("Runtime.evaluate", { expression: code, awaitPromise: true, returnByValue: true });
  console.log("nav:", r?.result?.value);
}

await new Promise((res) => setTimeout(res, waitFor));
const shot = await send("Page.captureScreenshot", {
  format: "png",
  ...(full ? { captureBeyondViewport: true } : {}),
});
await writeFile(outPath, Buffer.from(shot.data, "base64"));
console.log("wrote", outPath);
ws.close();
