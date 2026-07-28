; MEFinder per-user installer. Build through build_windows_installer.ps1 so the
; version is always sourced from src/me_finder/__init__.py.

#define AppName "MEFinder 文献原句定位器"
#define AppExeName "文献原句定位器.exe"
#define AppPublisher "sabercomo"
#define AppURL "https://github.com/sabercomo/MEFinder"

#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=x.y.z
#endif

[Setup]
AppId={{D4AD2090-2021-4851-A12F-FF70F8B63871}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\MEFinder
DefaultGroupName=MEFinder
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\release
OutputBaseFilename=MEFinder-v{#AppVersion}-windows-setup
SetupIconFile=..\assets\app_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
RestartIfNeededByRun=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#AppPublisher}
MinVersion=10.0.17763

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\MEFinder\*"; DestDir: "{app}"; Excludes: "portable.flag"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installed.flag"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; This is deliberately not skipifsilent: an in-app silent update closes the old
; process first and must relaunch the newly installed version on completion.
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait skipifdoesntexist runascurrentuser
