# Markdown 指定页码导出

## 2026-09-08：需求与实现

用户希望从已定位文献中只导出原书某几页，并将正文的超链接脚注一起带出。此前繁简检索 PR 不包含此功能，本次从 main 1bed83f 独立实施。

事实：原 PDF 导出先在完整文档建立章边界和脚注关系，再将注释置于章末。若直接按最后生成的 Markdown 截断，容易留下没有定义的脚注引用。现实现保留完整规范化结果，为正文块增加仅用于导出的物理页来源，再按选页正文的 note_id 集合保留定义。整书路径及 EPUB renderer 不改变。

事实：EPUB 解析器会在出版方 pagebreak 处切分文本，但未保存一般超链接和脚注目标。因此可以按已有出版方标签选正文，不能据现有入库数据可靠补齐跨页超链接脚注；明确提示此限制，未重解析源 EPUB。

保守策略：无映射、不唯一的原书标签、未能重建的已知跨页正文与合页半页裁剪均不猜测。支持用户切换 PDF 物理页码；未配对脚注保留原状并提示。

实现位置：markdown_page_selection.py、export_footnotes.py、document_export_service.py、archive_transfer_controller.py 与文献库导出菜单。契约见 `../contracts/v0.5.3-markdown-page-selection.md`；测试证据见 `../../reports/markdown-page-export-validation-2026-09-08.md`。
