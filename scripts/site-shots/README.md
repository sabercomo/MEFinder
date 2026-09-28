# 官网截图(site/assets)

`site/assets/` 下的界面截图都用这里的脚本从真实运行的 MEFinder 拍摄:独立演示库 + 无头 serve + Chrome DevTools 协议,1440×900 视口、2 倍像素,产出 2880×1800 PNG。Windows 与 macOS 通用,只需 Node 18+ 与 Chrome。

## 1. 准备演示库(一次性)

演示库不入库(几 GB,含运行时组件)。在一台已有文献库的机器上:

```bash
python scripts/site-shots/make_demo_library.py --corpus <数据根>/runtime/corpus
```

它把三本书以干净文件名复制到 `tmp/site-demo/incoming`(原文件名里的下载站水印不能上官网),再用 `tmp/site-demo/root` 作数据根启动 serve 并导入这三本,由真实解析管线生成数据。译本对照截图需要作品组与对齐结果:

```bash
python scripts/site-shots/run_alignment.py --api http://127.0.0.1:8766
```

换机器时直接拷 `tmp/site-demo/root/data/` 与 `root/corpus/` 即可,不必重建;`runtime/` 下的模型按需在本机下载。

## 2. 启动演示服务与 Chrome

```bash
# 数据根 = 当前目录;按要截的版本设 PYTHONPATH 指向对应源码
cd tmp/site-demo/root
PYTHONPATH=<仓库>/src python -m me_finder serve --host 127.0.0.1 --port 8766
```

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/mef-shots --hide-scrollbars about:blank
# Windows
"C:/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --remote-debugging-port=9222 --user-data-dir=tmp/site-demo/chrome-profile --hide-scrollbars about:blank
```

## 3. 拍摄

`node shoot.mjs <url> <输出png> <等待ms> [动作脚本] [依次点击的文字,逗号分隔]`

| 资源 | 命令 |
|---|---|
| `30-search.png` | `node scripts/site-shots/shoot.mjs http://127.0.0.1:8766/ site/assets/30-search.png 1500 scripts/site-shots/act_search.js`（动作脚本自动选中「序言第15—16页」那条并把高亮句滚到中间） |
| `50-compare.png` | `node scripts/site-shots/shoot.mjs http://127.0.0.1:8766/ site/assets/50-compare.png 1500 scripts/site-shots/act_compare.js` |
| `60-cite.png` | `node scripts/site-shots/shoot.mjs http://127.0.0.1:8766/ site/assets/60-cite.png 1000 scripts/site-shots/act_settings.js 引文格式` |
| `70-settings.png` | `node scripts/site-shots/shoot.mjs http://127.0.0.1:8766/ site/assets/70-settings.png 1000 scripts/site-shots/act_settings.js Zotero` |
| 其余(导入、文献库、阅读) | 不带动作脚本,用点击文字导航,如 `... 1500 "" 文献库` |

注意:

- **Zotero 页不得出现本机真实分类。** serve 会连本机 Zotero,`act_settings.js` 在页面内把 `/api/zotero/overview` 的分类换成演示分类;拍完检查截图里只有演示分类。
- 截图左下角带版本号,换版本后所有含侧栏的图都应重拍。
- 更换截图后同步 `site/index.html` 里对应 `<img>` 的 `alt` 描述。
