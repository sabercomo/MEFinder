# 安装器取消与关停路径收口（0.6.1，2026-10-31）

## 为什么只做这一块：先更正一个判断

本轮起因是我在架构评估收尾后给出的建议：三个安装器（MinerU 本地版、译本对齐运行时、本地 OCR）"操作/线程/状态模型仍是三份，约 500 行近乎一样的管线"，并说它是"能力漂移"的根源。

动手前复核，**这个判断有一半是错的，如实更正**：

| 当初的说法 | 复核结果 |
|---|---|
| 续传只有对齐和 OCR 有，MinerU 没有 | 已不成立 |
| 下载速度与剩余时间只有对齐组件显示 | 已不成立。三者的 `summary()` 都暴露 `download_speed_bps` / `eta_seconds` / `downloaded_bytes` / `total_bytes` / `progress` |
| 三者同名函数 1,763 行"近乎一样" | 数字对，但多数不是重复逻辑：`_install` 141/95/166 行是各组件自己的安装步骤，`_start_operation` 的前置校验与文案也各不相同 |

也就是说：第 3 项当初要解决的**用户可见漂移已经闭合**（`begin_download_progress` / `record_download_progress` 收口 + 对齐组件自带 `transfer_rate.py`），而三个状态类本身是真差异 —— 对齐运行时是单实例、带 `step` / `step_total` / 维护期 / `uninstall_deferred`，另两个是多档位、带 `download_samples`。把它们合成一套要注入五六个回调，得不偿失。

所以本轮**只合并行为确实相同的那几块**，不碰状态模型、不碰 `_operation_worker` 的骨架、不碰 `_install`。

## 不碰的部分（有在案决策）

- `_run_command` 与各自的重试策略：2026-10-31 已决策不合并。
- 各组件的测速算法：已决策"保持原样"，因此对齐运行时继续用 `transfer_rate.py` 而不是共用的 `download_samples`。
- `alignment_runtime.close()`：单实例状态 + `begin_shutdown`，还要避开 join 当前线程，结构本就不同，登记为例外。
- `alignment_runtime._remove_tree(strict=True)`：Windows 文件占用的重试版，不动。
- `_set_state`：MinerU 与 OCR 相近（24/24 行），但一个总是写 message/error、另一个只在非 None 时写，且 MinerU 多一个"总量是估算"标记；合并要加哨兵值，不如留着。

## 合并了什么

共用件都加进**已有的** `components/install.py`，不新增一层。

| 合并项 | 实现份数 | 去处 |
|---|---|---|
| 关停：取消全部 → 停子进程 → join 工作线程 | 2 → 1 | `cancel_and_join_operations()` |
| 取消请求：判活 → 置事件 → 写"正在取消并清理…" → 停子进程 | 2 → 1 | `request_cancel()` |
| 取消检查：事件已置则抛各自的取消错误 | 3 → 1 | `check_cancelled()` |
| 取消感知等待：重试退避期间不许压住取消 | 3 处内联 → 1 | `wait_unless_cancelled()` |
| 轮询循环：事件已置则先收子进程再抛 | 3 处内联 → 1 | `stop_if_cancelled()` |
| 清单路径：路径或取路径的函数 | 3 → 1 | `resolve_manifest_path()` |
| 删目录（非 strict） | 2 → 1 | `remove_tree_if_present()` |
| 取消文案字面量 | 9 处 | 2 个常量 `CANCELLED_MESSAGE` / `CANCELLING_MESSAGE` |

各安装器的 `_raise_if_cancelled` / `_remove_tree` / `_current_manifest_path` 保留为两行委托，调用点（4/7/7、8/15/9、5/2/3 处）与测试的替身注入点（`test_managed_alignment_runtime.py:850` 直接替换该方法）都不用改。

`cancel_and_join_operations` 的 `before_join` 钩子是 MinerU 独有的服务停止：停完子进程、join 之前要先停服务，join 成功后再停一次收掉 worker 刚放手的那个。这个顺序是有意的，docstring 里写明了。

## 行数：没降，降的是实现份数

| | 之前 | 之后 |
|---|---:|---:|
| `components/install.py` | 279 | 409 |
| `components/mineru.py` | 1,517 | 1,502 |
| `components/alignment_runtime.py` | 1,517 | 1,517 |
| `components/local_ocr_installer.py` | 1,303 | 1,286 |
| 四个文件合计 | 4,616 | **4,714（+98）** |
| 三者同名函数合计 | 1,763 行 | 1,715 行 |

**总行数涨了 98 行**：共用件带 Protocol 和解释顺序的 docstring，比它替掉的内联重复更长。按仓库自己的标准（"文件变短或层数增加本身不算收益"），收益不在行数上，而在"改一个功能牵动几处"：关停顺序的修法此前要改 2 处（MinerU、OCR）再单独改对齐，现在是 1 处 + 对齐；取消文案此前散在 9 个字面量里。

## 守卫

- `test_component_installers_share_cancel_and_shutdown`：两句取消文案必须在 `install.py` 里、且三个安装器里一处都不许再出现；`components/` 下 `thread.join(` 的拥有者恰好是 `install.py`、`alignment_runtime.py`（例外）、`catalog.py`、`embedding_models.py`（各有自己的线程账本）。
- `components/install.py` 新增 409 行上限：它是"机制"的去处，不是杂物抽屉。

## 验证

- 新增 12 项针对共用件的单元测试（`tests/test_component_install.py` 15 → 27 项），覆盖取消信号、退避不被压住、轮询收子进程、判活、关停 join、`before_join` 顺序、超时未停报 False、清单路径三种入参、删缺失目录。
- **变异检查**（两轮，house style）：
  1. 把 `before_join()` 挪到停子进程之前 → `test_before_join_runs_after_children_are_stopped` 失败。
  2. 删掉 `state.cancel_event.set()` → **第一版测试没抓住**，因为假 worker 写了 `cancel_event.wait(5)`，5 秒后自己结束、join 照样成功。改成 `wait()` 无超时（只因取消才结束，并用 `addCleanup` 收尾）后，该变异稳定触发 2 项失败。这条如实记下来：自解的假 worker 会让关停断言变成空断言。
- 组件专项 123 项通过；全量 unittest **2,862 项通过**（39 条件跳过，189.442 秒；比上一轮 2,847 多 15 项 = 12 项共用件单测 + 3 项守卫）。`ruff check .`（排除既有未入库 `.codex-tmp`）与 `git diff --check` 通过。
- 未做：三个组件的真实网络安装、Windows 实机、打包。取消与关停改的是进程/线程收尾路径，真实安装与 Windows 文件占用行为需要实机才能确认。

## 顺带发现：`src/me_finder/components/__init__.py` 从未入库

提交时 `git add src/me_finder/components/install.py` 被拒：

```
The following paths are ignored by one of your .gitignore files:
src/me_finder/components
```

**事实**：`.gitignore` 有一条 `components/`，注释写明是"从源码运行 `serve` 时 ComponentCatalog 在工作目录生成的本地组件状态"，指仓库根的 `./components`。这条未锚定的规则顺带命中了源码子包 `src/me_finder/components/`。后果有两层：

1. 该目录下的文件无法用 `git add <path>` 暂存（已跟踪的文件也不行——git 拒绝 add 位于被忽略目录内的路径）。分子包那轮（`fce624e`）的 8 个模块能进去，是因为 `git mv` 绕过这道检查。
2. **`src/me_finder/components/__init__.py` 因此从未入库**。清洁检出会缺这个文件。它没被发现，是因为 Python 3 的命名空间包让 `import` 照常工作，冻结包也照常打得出来（上一轮我验过的那个包就缺它），CI 从未红过。

**修**：把规则改成显式放回源码包，仓库根与 `D:/runtime/components`（Windows 路径误操作留下的本地目录）仍然被忽略：

```gitignore
components/
!src/me_finder/components/
```

补提交 `src/me_finder/components/__init__.py`。

**守卫**（两条，变异检查均触发）：

- `test_every_package_module_is_tracked_in_git`：`src/me_finder` 下每个 `.py` 都必须在 `git ls-files` 里。把 `__init__.py` 退出索引 → 立即失败。非 Git 检出（源码导出副本）时跳过。
- `test_every_source_subpackage_declares_an_init`：包内任何含 `.py` 的目录都必须有 `__init__.py`。缺 `__init__` 时 Python 按命名空间包照常导入，不会自己报错，只能靠守卫发现。

这条和取消/关停收口无关，是提交时撞上的；一并修掉，因为它会让此后每一次改 `components/` 下的文件都踩同一个坑。

## 补记：macOS arm64 测试包（2026-10-31 22:50–22:55，本地，未上传未安装）

与上一轮不同，本轮改动落在 `components/` 下，冻结包里确实有份，因此验收能落到实处。

- 源码边界：`5cfa5bb`。工作区只有既有的 `.hallmark/log.json` 与三个未跟踪文件，都不进包。
- 构建：`MEFINDER_PYTHON="$PWD/.venv-macos312-arm64/bin/python" MEFINDER_TARGET_ARCH=arm64 ./build_macos.sh`，退出码 0；构建内全量 unittest 2,862 项通过（39 跳过，190.709 秒）。
- 产物（替换了 `e736a9c` 那一版：ZIP 96,295,494 字节 `e7fbc594…`、DMG 104,264,926 字节 `d03d7678…`）：

| 产物 | 字节 | SHA-256 |
|---|---:|---|
| `release/MEFinder-v0.6.1-macos-arm64.zip` | 96,298,815 | `32d5ba036440241a992ab2ff091dc523b953ed46f807ef38a68cb84a884596ec` |
| `release/MEFinder-v0.6.1-macos-arm64.dmg` | 104,392,240 | `6eaeb1c84f102e6fe881ce916d3abefdae8487a0ea6a7a0202469e67ede9c50f` |

独立复核（新空目录 `ditto -x -k` 解 ZIP 后）：

1. `codesign --verify --deep --strict` 通过；106 个 Mach-O 全为 arm64；无 `data_root.txt` / `*.local.json` / `._*`；`shasum -c` 两项 OK。
2. **组件模块在主程序里，不在侧车里**。侧车 PYZ 完全没有 `components.*` —— MCP 侧车是只读工具集，依赖分析本就不该把安装器拉进去；上一轮查 `search` 时在侧车里能查到，是因为 MCP 确实用检索。本轮的组件检查因此落在主程序（754 个模块），9 个条目齐全：`components`（即补入库的 `__init__.py`）、`alignment_runtime`、`catalog`、`contract`、`embedding_models`、`install`、`local_ocr_installer`、`mineru`、`transfer_rate`。
3. **本轮共用件确实进了包**：直接读冻结 `install` 模块的 code object，10 个新符号全在 —— `cancel_and_join_operations` / `request_cancel` / `check_cancelled` / `wait_unless_cancelled` / `stop_if_cancelled` / `resolve_manifest_path` / `remove_tree_if_present` / `CANCELLED_MESSAGE` / `CANCELLING_MESSAGE` / `OperationState`，两句取消文案也只存在于该模块。
4. **反向核对**：三个安装器的冻结 code object 里硬编码取消文案均为 **0 处**，且各自引用的共用件恰好是该有的子集 —— MinerU 与本地 OCR 七个/六个，`alignment_runtime` 只有 `check_cancelled` / `wait_unless_cancelled` / `stop_if_cancelled` / `resolve_manifest_path`，不含 `cancel_and_join_operations` / `request_cancel` / `remove_tree_if_present`，与"它的 `close()`、`_cancel` 与 strict 删目录登记为例外"一致。这同时也是导入兼容性检查：安装器 `from .install import` 的每个名字都在冻结的 `install` 模块里。
5. 冻结侧车端到端：一次性数据根（`ME_FINDER_APP_DATA_ROOT`，索引在 `<root>/runtime/data/index.sqlite3`）驱动包内 `MEFinderMCP` 走 STDIO，`initialize` 返回 `mefinder 0.6.1`，`tools/list` 17 个，`list_documents` / `search_passages` / `locate_quote` 均返回结果。不接触用户真实文献库。

**未验证**：取消与关停的**运行时行为**没有在冻结包里跑过 —— 那需要真实下载并安装一个组件，再中途取消 / 关闭应用。本轮只静态证明了代码进包与符号配对。三个组件的真实网络安装、Windows 实机（文件占用）、GUI 人工点击同样未做；未安装到 `/Applications`、未上传、未发布。
