2026-09-27：两块试点（译本对齐模型行、托管 MinerU 卡片）均行为一致、离线正常，已有 CI 渲染测试；手工 DOM 同步归零，但两块总行数都上升。结论：维持零构建，只在改到某张卡片时顺带迁移，不做整体迁移，不引入 Vite/TS。

（下文「验收结果」「下一步建议」为第一块完成时的记录，第二块结果见文末追加。）

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

## 2026-09-27 追加：第二块（托管 MinerU 卡片）与 CI 渲染测试

结论：第二块同样行为一致、状态同步点归零，但总行数仍上升；**净收益没有随卡片变复杂而转为行数下降**。建议维持零构建，只在改到某张卡片时顺带迁移，不做整体迁移，不引入 Vite/TS。

### CI 渲染测试（事实）

- 新增 `tests/fixtures/vue_mini_dom.js`（只实现 Vue runtime-dom 实际调用的接口）与 `tests/test_vue_components.py`：在 node 里执行随包 Vue，真实渲染两块组件，断言文字、属性、进度条与点击接线。
- 变异检查：把模板里的 `stateText` 改成 `stateTxt`、把点击参数改错，测试均失败；还原后通过。
- 限制：最小 DOM 没有 `innerHTML`，Vue 模板里不能出现 `&`（含 `&&` 与实体），逻辑放进 setup 函数。

### 第二块迁移（事实）

| 项 | 结果 |
|---|---|
| 范围 | `#managed-mineru`：硬件说明、检查新版本/安装推荐配置、Pipeline 与 VLM 两套配置 × 状态/进度/5 个按钮、底部提示 |
| 分工 | `70-vision.js` 仍负责请求、代次、轮询与「本地部署」总状态，只经 `setRuntime / setNotice / setChecking / setPending` 写 store；`70-managed-mineru-view.js` 只读 |
| 手工 DOM 写入 | 30 处（按配置循环执行）→ 0 |
| 模板 | 两套配置、10 个按钮的重复标记改为一份 `v-for`；模板事件 −12 |
| 行数 | `70-vision.js` 1217→1072（−145，其中 17 行格式化函数移入 `06-pure.js`、约 35 行文案函数移入视图）；视图 +249；模板 −16；CSS +2。合计约 +96 |
| 门禁 | 全量 2596 OK（36 skip），ruff 零告警；浏览器预览核对真实数据（CPU）与注入的运行中/下载中状态 |

### 迁移中发现的既有问题（事实）

`.managed-mineru-profile { display: grid }` 盖掉了 `[hidden]`：0.5.7 在不支持 VLM 的机器上把 VLM 行设为 `hidden`，但它仍然显示。原测试只断言 `hidden` 属性，未断言是否真的收起。已补 `.managed-mineru-profile[hidden] { display: none; }`，预览中计算样式为 `none`。

### 推断

- 行数不降的主要原因：原命令式代码虽然啰嗦，但每个元素只写一行；视图化后要写纯函数（便于测试）加模板两份。收益在"改状态不必找元素"和"可测试"，不在代码量。
- 上 Vite + TS 的理由因此只剩单文件组件的可读性与类型检查，不足以抵消构建链、打包脚本与守卫体系的改造成本。

