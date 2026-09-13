cask "mefinder" do
  arch arm: "arm64", intel: "x86_64"

  version "0.5.3"
  sha256 arm:   "b9773adcd3683176b6ac9205e3c1a24629809665a6156238d2638ed748782fdc",
         intel: "de058f8ffb4c8bb5dd79653b4b1e7d690fcb941c553746a3dae986cd89ba9a3b"

  url "https://github.com/sabercomo/MEFinder/releases/download/v#{version}/MEFinder-v#{version}-macos-#{arch}.dmg"
  name "MEFinder"
  desc "Local-first literature passage locator for PDF, DOCX and EPUB"
  homepage "https://github.com/sabercomo/MEFinder"

  livecheck do
    url "https://github.com/sabercomo/MEFinder/releases/latest"
    strategy :github_latest
  end

  app "MEFinder.app"
end
