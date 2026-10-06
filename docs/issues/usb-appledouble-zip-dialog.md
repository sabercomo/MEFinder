# U 盘文档包触发 Windows 压缩文件夹弹窗

## 2026-10-06 — 实际 U 盘核验与修复

### 事实

- 用户报告：通过文献库勾选导出后插入 U 盘，Windows 持续提示「请插入多卷集的最后一张磁盘」；单篇详情导出未观察到此现象。用户于 2026-10-07 进一步确认：这次在 Mac 导出，再插到 Windows 后持续弹窗。
- 本次挂载为 I 盘。原文档包 41,396,568 字节，ZIP CRC 全部通过、三个成员的卷号均为 0，含正文、原 PDF 和 manifest。旁边同名 `._` 文件仅 4,096 字节，AppleDouble 魔数 `00051607`，不是 ZIP，却使用 `.mefinder.zip` 后缀。证据见 [核验报告](../../reports/usb-document-package-2026-10-06.md)。
- 对该辅助文件追加 `.appledouble` 后缀保存，不删除；取消现有弹窗后，两次重新列出 Windows 窗口均未见该弹窗。真实文档包未修改。
- 单篇和批量文档包导出均调用 `/api/document/export`、共用 `export_indexed_pdf`，不是两套写 ZIP 的实现。

### 推断与修复

- 推断：Windows 压缩文件夹在浏览 / 枚举 U 盘时把带 ZIP 后缀的 AppleDouble 当成压缩文件，导致反复请求多卷磁盘。本次辅助文件改名后的窗口观察支持该解释；未声称逐个跟踪 Explorer 内部打开文件调用。
- 新导出改为 `.mefinder`，保持内部 ZIP 与全部原有数据，避免触发 Windows 的 ZIP Shell 处理。新后缀的 Mac 辅助文件也不以 `.zip` 结尾。
- 导入文件选择、前端队列、后端分块上传和包读取同步支持新后缀，兼容旧后缀；前端忽略 `._` 文件。
- 先红后绿：`test_indexed_pdf_exports_streaming_protocol_with_metadata` 新后缀断言、`test_new_package_suffix_restores_same_records_as_legacy_zip`、`test_chunked_upload_accepts_current_and_legacy_package_suffixes`。`test_file_picker_accepts_both_packages_and_skips_mac_metadata` 通过 Node 执行队列入口。带原 PDF 的分块恢复测试改用新后缀，其他旧后缀用例保留。
- 初始 ZIP64 兼容性试验已撤回：实际正常包也使用 ZIP64，不能把合法 ZIP64 当作损坏或多卷文件的证据。
