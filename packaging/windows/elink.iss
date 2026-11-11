; Elink Windows 安装包（Inno Setup 6）
;
; 由 scripts/package_windows.py 调用：
;   ISCC.exe /DMyAppVersion=0.6.2 /DSourceDir=dist\Elink /DOutputDir=dist packaging\windows\elink.iss
;
; 使用与旧版 E:\elink 相同的 AppId 和默认目录，安装新版会覆盖旧版程序文件，
; 用户配置仍在 %LOCALAPPDATA%\Elink（不随卸载删除）。

#ifndef MyAppVersion
  #define MyAppVersion "0.6.2"
#endif
#ifndef MyAppId
  #define MyAppId "A4735807-684B-4757-9EE5-88AFA2E6E791"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\Elink"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

#define MyAppName "Elink"
#define MyAppPublisher "Elink"
#define MyAppExeName "Elink.exe"
#define MyRepoURL "https://github.com/yuyuyu501/elink"

[Setup]
AppId={{{#MyAppId}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyRepoURL}
AppSupportURL={#MyRepoURL}/issues
AppUpdatesURL={#MyRepoURL}/releases
DefaultDirName={localappdata}\Programs\Elink
DisableDirPage=no
DirExistsWarning=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Elink-{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\..\frontend\desktop\resources\elink.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#MyAppVersion}.1
VersionInfoDescription=Elink offline installer
VersionInfoCompany={#MyAppPublisher}

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; 新版本运行时只替换程序文件，旧 _internal 残留会导致 Qt 插件冲突
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\docs"
Type: filesandordirs; Name: "{app}\web"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command, Executable: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Executable := ExpandConstant('{app}\{#MyAppExeName}');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Elink', Command) then
      if (CompareText(Command, '"' + Executable + '" --ui') = 0) or
         (CompareText(Command, Executable + ' --ui') = 0) then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Elink');
  end;
end;
