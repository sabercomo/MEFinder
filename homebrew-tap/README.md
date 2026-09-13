# MEFinder Homebrew Tap

通过 [Homebrew](https://brew.sh) 安装和更新 MEFinder（macOS）。

## 安装

```bash
brew tap sabercomo/mefinder
brew install --cask mefinder
```

## 更新

```bash
brew update && brew upgrade --cask mefinder
```

## 说明

- 同时提供 arm64（Apple Silicon）与 x86_64（Intel）两个架构的 DMG，安装时自动选择。
- **首次启动**：当前安装包为 ad-hoc 签名、未经 Apple 公证。首次打开若被拦截，请在
  「系统设置 → 隐私与安全性 → 仍要打开」批准一次；之后的 `brew upgrade` 会延续已批准
  状态，无需重复操作。
- 应用数据写入 `~/Library/Application Support/MEFinder/`，升级与卸载
  （`brew uninstall --cask mefinder`）都不会删除文献库、索引和偏好设置。
- cask 通过 livecheck 跟踪 GitHub Releases；每个正式版本发布后由主仓库的
  `scripts/update_homebrew_tap.py` 更新本仓库的 `Casks/mefinder.rb`。
