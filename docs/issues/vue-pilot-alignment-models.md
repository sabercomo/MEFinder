# Vue 3 试点：设置页「译本对齐模型」行

2026-09-27：试点可用、行为一致、离线正常；状态同步代码明显减少，但总行数未减少。建议继续零构建方式再迁一块同类卡片后再定是否上构建链，暂不引入 Vite/TS。

## 试点范围与方式

- 范围：设置页「译本对齐模型」两行（单选、状态、提示、进度条、下载/删除按钮）及标题栏状态。该区域曾多次修过状态呈现问题（d551a7d 等）。
- 方式：零构建。`static/vendor/vue.global.prod.js`（Vue 3.5.43，MIT）以独立 `<script>` 内联进主窗口，位于应用脚本之前；阅读器独立窗口不加载。不改打包脚本，不引入 Node 构建。
- 分工：`64-settings-model.js` 仍负责请求、请求代次、轮询，是状态的唯一写入方，只经 `MEFinderAlignmentModelView.setComponent / setSelection / setPending / setLoadError` 写 store；`64-settings-model-view.js` 只读 store，用 Vue 组件渲染模型行，用 `watchEffect` 更新标题栏状态。
- 旧代码与 Vue 各管各的 DOM：Vue 只挂在 `#embedding-model-options`；标题栏 `#alignment-model-status` 因 CSS 用直接子选择器，未作挂载点，由 `watchEffect` 写同一元素。

## 验收结果（事实）

| 验收项 | 结果 |
|---|---|
| 行为一致 | 状态文案逐字沿用原实现；浏览器预览（开发库副本）核对了已下载/未下载、下载中（有/无进度）、校验中、下载失败、正在启动、读取失败、切换被拒八种呈现，视觉与原来一致 |
| 离线打包 | Vue 文件在 `static/` 下，`packaging/desktop.spec` 与 `desktop_macos.spec` 整目录打包，无需改动；运行时不联网 |
| 门禁 | 全量 unittest 2593 项 OK（36 skip）；ruff F 零告警；新增 `VendorAssetTests` 按字节钉死 Vue 文件 SHA-256 |
| 状态同步代码 | 模型行相关的手工 DOM 写入从 59 处降为 0 处；两行重复的模板标记（36 行）合并为一份 `v-for` 模板；模型名称、体积只声明一次 |
| 代码总量 | 控制器 485→385 行（−100），模板 −36 行，模板事件 −3；新增视图文件 196 行。**合计约 +57 行，没有减少** |
| 体积 | 装配后主页面 1,327,952 → 1,497,818 字节（+13%，基本全是 Vue 本体） |

预览中发现并修复了一处 Vue 特有的问题：切换模型被拒（保存中或偏好未就绪）时，store 未变，Vue 不回写单选框，而浏览器已取消原选中项，导致两项都未选中。现由 `pick` 在 `nextTick` 后按 store 对齐整组单选框。

## 推断与局限

- 推断：收益主要体现在"改一个状态不必找齐所有受影响元素"，这正是此前反复出 bug 的地方；行数没有下降，是因为视图纯函数（`rowView` / `summaryView`）和模板本身也要写。卡片越多、状态越复杂，净收益越大。
- 局限：CI 的 node 测试没有 DOM，只能测 store 与纯函数；Vue 模板本身的渲染只在浏览器预览里验证过，没有自动化测试覆盖。继续扩大前应补一种能在 CI 跑的渲染测试（例如在 node 里提供最小 DOM），否则模板改错只能靠人工发现。
- 局限：`test_template_has_no_inline_events` 等守卫只检查 `index.html` 与自有 JS 的写法，Vue 模板里的 `@click` 不在其覆盖范围。

## 下一步建议

1. 再用零构建方式迁一块同类卡片（本地 OCR 或托管 MinerU，状态更多），检验净收益是否随规模变大。
2. 补 CI 可跑的 Vue 渲染测试。
3. 两块都成立后，再评估是否上 Vite + TS（引入 Node 构建链、改打包脚本和守卫体系）。
4. 回退办法：删除 `static/vendor/`、`64-settings-model-view.js`，还原模板与控制器（本试点为单一提交）。
