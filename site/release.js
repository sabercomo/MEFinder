/* The one place release facts live; every version, date, size and download
 * link on the page is filled from here at load. The values written in
 * index.html are only the no-JS fallback.
 * released / date / sizes are rewritten on deploy from the latest GitHub
 * release (scripts/update_site_release.py) — edit dev / devTopic by hand. */
window.MEF_RELEASE = {
  released: "0.5.5",        // latest published version
  date: "2026-09-24",       // its publish date
  dev: "0.5.7",             // version in development; null (or already released) hides the "迭代中" notes
  devTopic: "架构重构与跨平台验收",
  sizes: {                  // asset suffix -> size shown in the download table
    "windows-setup.exe": "74.9 MB",
    "windows-portable.zip": "89.7 MB",
    "macos-arm64.dmg": "95.6 MB",
    "macos-x86_64.dmg": "99.0 MB",
  },
};

(() => {
  "use strict";
  const R = window.MEF_RELEASE;
  const repo = "https://github.com/sabercomo/MEFinder/releases";
  const assetUrl = (suffix) => `${repo}/download/v${R.released}/MEFinder-v${R.released}-${suffix}`;
  R.assetUrl = assetUrl;

  // A dev version that the latest release has caught up with is no longer "in progress".
  const newer = (a, b) => {
    const pa = a.split(".").map(Number), pb = b.split(".").map(Number);
    for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
      if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) > (pb[i] || 0);
    }
    return false;
  };
  if (R.dev && !newer(R.dev, R.released)) R.dev = null;

  // "current" is the version badge in the top bar: the dev version while one is in progress.
  const slots = { released: R.released, date: R.date, dev: R.dev, devTopic: R.devTopic, current: R.dev || R.released };
  document.querySelectorAll("[data-rel]").forEach((el) => {
    const v = slots[el.dataset.rel];
    if (v) el.textContent = v;
  });
  document.querySelectorAll("[data-asset]").forEach((el) => { el.href = assetUrl(el.dataset.asset); });
  document.querySelectorAll("[data-asset-sha]").forEach((el) => { el.href = assetUrl(el.dataset.assetSha) + ".sha256.txt"; });
  document.querySelectorAll("[data-rel-tag]").forEach((el) => { el.href = `${repo}/tag/v${R.released}`; });
  document.querySelectorAll("[data-size]").forEach((el) => {
    const v = R.sizes[el.dataset.size];
    if (v) el.textContent = v;
  });
  if (!R.dev) document.querySelectorAll("[data-rel-dev-only]").forEach((el) => { el.hidden = true; });
})();
