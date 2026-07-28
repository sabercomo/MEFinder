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

[UninstallDelete]
Type: files; Name: "{app}\data_root.txt"

[Run]
; This is deliberately not skipifsilent: an in-app silent update closes the old
; process first and must relaunch the newly installed version on completion.
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait skipifdoesntexist runascurrentuser

[Code]
var
  DataDirPage: TInputDirWizardPage;

function DefaultDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\MEFinder');
end;

// A prior install (pre-v0.1.9, or a repair/upgrade that already recorded a
// choice) already has real data sitting at the historical default location.
// Never re-prompt in that case: doing so risks the user picking a different
// folder and the app opening to an apparently empty library.
function ExistingDataPresent(): Boolean;
begin
  Result := DirExists(DefaultDataDir() + '\runtime') or FileExists(DefaultDataDir() + '\preferences.json');
end;

procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    '选择数据存储位置',
    '文献索引、语料和设置的保存位置',
    '导入的 PDF/Word 原文、搜索索引和个人设置会保存在下面选择的目录中，与程序安装目录分开，后续静默更新不会影响这些数据。' + #13#10#13#10 +
    '如果语料库较大，建议选择空间充足的磁盘。',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := DefaultDataDir();
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (DataDirPage <> nil) and (PageID = DataDirPage.ID) then
    Result := FileExists(ExpandConstant('{app}\data_root.txt')) or ExistingDataPresent();
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  MarkerPath, ChosenDir: String;
begin
  if CurStep = ssPostInstall then
  begin
    MarkerPath := ExpandConstant('{app}\data_root.txt');
    if not FileExists(MarkerPath) then
    begin
      ChosenDir := DataDirPage.Values[0];
      if ChosenDir = '' then
        ChosenDir := DefaultDataDir();
      if not ForceDirectories(ChosenDir) then
        ChosenDir := DefaultDataDir();
      SaveStringToFile(MarkerPath, ChosenDir, False);
    end;
  end;
end;
