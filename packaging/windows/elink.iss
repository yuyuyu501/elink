#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef InstallerRevision
  #define InstallerRevision 1
#endif

[Setup]
AppId={{A4735807-684B-4757-9EE5-88AFA2E6E791}
AppName=Elink
AppVersion={#AppVersion}
AppVerName=Elink {#AppVersion} Preview
AppPublisher=Elink
AppPublisherURL=https://github.com/yuyuyu501/elink
AppSupportURL=https://github.com/yuyuyu501/elink/issues
AppUpdatesURL=https://github.com/yuyuyu501/elink/releases
DefaultDirName={localappdata}\Programs\Elink
DisableDirPage=no
DirExistsWarning=no
AlwaysShowDirOnReadyPage=yes
DefaultGroupName=Elink
DisableProgramGroupPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\app\Elink.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}.{#InstallerRevision}
VersionInfoDescription=Elink offline installer

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.UnsafeDirectory=Choose a new or empty application folder, or the folder of an existing Elink installation. Windows normally treats names that differ only in letter case as the same path.
chinesesimplified.UnsafeDirectory=请选择新的或空的应用文件夹，或现有 Elink 安装目录。Windows 通常不区分路径大小写，例如 Elink 和 elink 是同一路径。
english.SourceDirectory=Cannot install into a source code directory or its subfolders.
chinesesimplified.SourceDirectory=不能安装到源码目录或其子目录中。
english.DataDirectory=Cannot install into the Elink user configuration directory.
chinesesimplified.DataDirectory=不能安装到 Elink 用户配置目录中。
english.RootDirectory=Cannot install directly into a drive root.
chinesesimplified.RootDirectory=不能直接安装到磁盘根目录中。
english.OccupiedDirectory=This folder contains unrelated files or another application.
chinesesimplified.OccupiedDirectory=此文件夹已有无关文件或其他应用。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The updater replaces only app, leaving the installer and shortcuts intact.
Source: "{#PayloadDir}\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\licenses\InnoSetup-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Elink"; Filename: "{app}\app\Elink.exe"; WorkingDir: "{app}\app"
Name: "{group}\{cm:UninstallProgram,Elink}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Elink"; Filename: "{app}\app\Elink.exe"; WorkingDir: "{app}\app"; Tasks: desktopicon

[Run]
Filename: "{app}\app\Elink.exe"; Description: "{cm:LaunchProgram,Elink}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Also remove program files added by ZIP updates, never user configuration.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\.Elink-update-*"

[Code]
function NormalizedPath(Value: String): String;
begin
  Result := RemoveBackslashUnlessRoot(ExpandFileName(Value));
end;

function DirectoryProblem(Directory: String): String;
var
  Parent, NextParent, Config, Installed: String;
  Entry: TFindRec;
begin
  Result := '';
  Directory := NormalizedPath(Directory);
  if CompareText(AddBackslash(Directory), AddBackslash(ExtractFileDrive(Directory))) = 0 then
    Result := CustomMessage('RootDirectory');
  Config := AddBackslash(NormalizedPath(ExpandConstant('{localappdata}\Elink')));
  if Pos(Lowercase(Config), Lowercase(AddBackslash(Directory))) = 1 then
    Result := CustomMessage('DataDirectory');
  Parent := Directory;
  while Parent <> '' do
  begin
    if DirExists(Parent + '\.git') or FileExists(Parent + '\.git') or
       (FileExists(Parent + '\pyproject.toml') and DirExists(Parent + '\elink')) then
    begin
      Result := CustomMessage('SourceDirectory');
      Exit;
    end;
    NextParent := ExtractFileDir(Parent);
    if CompareText(Parent, NextParent) = 0 then Break;
    Parent := NextParent;
  end;
  if Result <> '' then Exit;
  if RegQueryStringValue(HKCU64,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{A4735807-684B-4757-9EE5-88AFA2E6E791}_is1',
      'InstallLocation', Installed) then
    if (CompareText(Directory, NormalizedPath(Installed)) = 0) and
       FileExists(Directory + '\unins000.exe') then Exit;
  if FindFirst(AddBackslash(Directory) + '*', Entry) then
  begin
    try
      repeat
        if (Entry.Name <> '.') and (Entry.Name <> '..') then
        begin
          Result := CustomMessage('OccupiedDirectory');
          Break;
        end;
      until not FindNext(Entry);
    finally
      FindClose(Entry);
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Problem: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Problem := DirectoryProblem(WizardDirValue);
    if Problem <> '' then
    begin
      Log('Unsafe install directory: ' + Problem);
      SuppressibleMsgBox(Problem + #13#10#13#10 + CustomMessage('UnsafeDirectory'), mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Silent installation must enforce the same check before copying any files. }
  Result := DirectoryProblem(ExpandConstant('{app}'));
  if Result <> '' then
  begin
    Log('Unsafe install directory: ' + Result);
    Result := Result + #13#10#13#10 + CustomMessage('UnsafeDirectory');
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Command, Executable: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Executable := ExpandConstant('{app}\app\Elink.exe');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Elink', Command) then
      if (CompareText(Command, '"' + Executable + '" --startup') = 0) or
         (CompareText(Command, Executable + ' --startup') = 0) then
        RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Elink');
  end;
end;
