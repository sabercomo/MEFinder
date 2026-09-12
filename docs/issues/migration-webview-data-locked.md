# 迁移失败：WebView 运行时数据被锁定

## 问题描述

用户在应用运行时尝试迁移数据位置到 OneDrive 时失败，错误信息：

```
迁移数据失败：
[('D:\\ME_Finder\\dist\\MEFinderData\\runtime\\webview-data\\EBWebView\\Default\\Network\\Cookies',
'E:\\OneDrive\\.MeFinder.migration-ba1ded03b28d4b79a7a9037a8453673d\\runtime\\webview-data\\EBWebView\\Default\\Network\\Cookies',
'[WinError 32] 另一个程序正在使用此文件，进程无法访问。')]
```

**根本原因**：EdgeWebView2 在应用运行时锁定了 `runtime/webview-data/` 下的文件（Cookies、Cache、LocalStorage 等），`shutil.copytree` 无法复制这些文件。

## 设计决策

WebView 运行时数据**不应该被迁移**：

1. **运行时锁定**：应用运行时它们被 EdgeWebView2 独占，无法复制
2. **可重建性**：这些是浏览器缓存和会话数据，应用在新位置启动时会自动重新创建
3. **非用户数据**：不包含用户的文献、索引或偏好设置

迁移的真正目标是：
- SQLite 索引数据库（`runtime/data/index.sqlite3`）
- 文献语料（`runtime/corpus/`）
- 用户偏好（`preferences.json`）
- 配置文件（`runtime/config/`）

## 修复方案

**2026-09-10**：在 `data_location.py` 的 `ignore` 函数中跳过 `runtime/webview-data/` 整个目录。

修改位置：[data_location.py:178-195](../../src/me_finder/data_location.py#L178-L195)

```python
def ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    directory_path = Path(directory).resolve()
    if directory_path == current and DATA_ROOT_MARKER in names:
        ignored.add(DATA_ROOT_MARKER)
    if directory_path == database_source.parent:
        for database_name in (
            database_source.name,
            database_source.name + "-wal",
            database_source.name + "-shm",
        ):
            if database_name in names:
                ignored.add(database_name)
    # Skip webview-data: locked by EdgeWebView2 at runtime, auto-recreated.
    runtime_dir = current / "runtime"
    if directory_path == runtime_dir and "webview-data" in names:
        ignored.add("webview-data")
    return ignored
```

**测试覆盖**：更新 `tests/test_data_location.py`：
- `_create_current_data_root` 创建模拟的 webview-data 结构
- `test_migration_copies_all_data_validates_sqlite_and_retains_old_data` 断言目标位置不存在 webview-data

## 影响范围

- ✅ 迁移现在可以在应用运行时成功执行
- ✅ 用户数据（索引、语料、偏好）完整迁移
- ✅ WebView 在新位置启动时自动重建运行时数据
- ✅ 不影响现有的 SQLite 事务复制逻辑
- ✅ 向后兼容：旧版本创建的数据目录没有 webview-data 时，ignore 函数无操作

## 验证步骤

1. 在应用运行时打开「设置 → 数据位置」
2. 选择目标位置（如 OneDrive 文件夹）
3. 点击「正在迁移...」按钮
4. 迁移应成功完成，提示重启
5. 重启应用，验证新位置的索引和文献可用
6. 验证 WebView 功能正常（搜索、阅读器、对照阅读）

## 相关文件

- [src/me_finder/data_location.py](../../src/me_finder/data_location.py)
- [tests/test_data_location.py](../../tests/test_data_location.py)
- [src/me_finder/desktop_shell_controller.py](../../src/me_finder/desktop_shell_controller.py)
