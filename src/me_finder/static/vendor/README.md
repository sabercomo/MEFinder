# static/vendor

随安装包分发的第三方前端文件。用户运行时不联网、不需要 Node。

| 文件 | 版本 | 来源 | 许可 |
|---|---|---|---|
| `vue.global.prod.js` | Vue 3.5.43 | npm `vue@3.5.43` 包内 `dist/vue.global.prod.js`（`npm pack` 取得，tarball shasum `bab91368e7b9aad9b89dd92e866b120081deb094`） | MIT，见 `vue.LICENSE.txt` |
| 设置目录内联 SVG（`templates/index.html`） | Lucide 0.468.0 | npm `lucide-static@0.468.0/icons/`，保留 `currentColor` 描边 | ISC，见 `lucide.LICENSE.txt` |

- 文件 SHA-256 钉在 `tests/test_frontend_assets.py`（`VendorAssetTests`）；升级 Vue 时同步更新哈希与本表。
- `.gitattributes` 对本目录关闭换行符转换，保证检出后字节不变。
- 装配：`web_assets.py` 把它内联进主窗口独立的 `<script>` 块，位于应用脚本之前；阅读器独立窗口不加载。
- 当前用途（Vue 试点）：设置页「译本对齐模型」行（`static/js/64-settings-model-view.js`）、托管 MinerU 卡片（`static/js/70-managed-mineru-view.js`）。
- 渲染测试：`tests/test_vue_components.py` 用 `tests/fixtures/vue_mini_dom.js` 在 node 里执行本文件；模板里不要写 `&`。

设置目录的 Lucide 图标直接内联在模板内，不加载图标运行时或远程资源；Zotero 的 Z 标记沿用原图形。
