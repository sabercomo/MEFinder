# U 盘文档包核验（2026-10-06）

## 方法与事实

只读检查用户本次挂载的 I 盘根目录文件，使用 Python 3.12 的 `zipfile.is_zipfile` / `ZipFile.testzip`、文件头和成员信息；没有读取其他目录里的书籍内容。

| 文件 | 大小 | 文件头 / 校验 |
| --- | ---: | --- |
| `西方马克思主义探讨-20261006-153656-f0b855.mefinder.zip` | 41,396,568 字节 | `504b0304`；ZIP CRC 全通过 |
| `._西方马克思主义探讨-20261006-153656-f0b855.mefinder.zip` | 4,096 字节 | `00051607000200004d6163204f532058`；非 ZIP，AppleDouble |

真正文档包成员：`pages.ndjson` 1,283,124 字节、`source/original.pdf` 41,165,875 字节、`manifest.json` 3,100 字节。三个成员卷号均为 0；正文成员需要 ZIP 4.5（ZIP64），原 PDF 和 manifest 为 ZIP 2.0。CRC 无坏成员。

在本机临时验证目录复制该真实包为 `.mefinder`（U 盘上的真包保持不变）：新旧后缀读取的 174 页文本、元数据与映射逐字段相等；从新后缀包提取原 PDF，SHA-256 与 manifest 相等。整个包 SHA-256 为 `9b983301d7742f5030a41e9ff7618920cce62b69fa9f167ea7ebd4c43e58b89e`。本机原始核验结果保存于 `.codex-tmp/windows-v059-20261006/actual-usb-verification.json`，原书与其正文副本不提交仓库。

## 现场处置与观察

将上述 `._` 文件改名为原名追加 `.appledouble`，保留全部字节。真实 ZIP 未改动。使用 `computer-use` 读取弹窗文本并取消；随后两次列出窗口，均不存在「压缩(zipped)文件夹」。该观察不等同于所有 Windows 版本 / 所有 U 盘的完整验收。

## 代码验证

新的 `.mefinder` 后缀不会匹配 `.zip`；内部格式不变，旧 `.mefinder.zip` 可恢复。新旧后缀读取等价、分块上传接受、新后缀带原 PDF 恢复、前端忽略 AppleDouble 均有回归测试。全量门禁和 Windows 构建结果在本轮版本说明中补齐。
