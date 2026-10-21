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
#if Int(InstallerRevision) > 1
  #define InstallerSuffix "-r" + Str(Int(InstallerRevision))
#else
  #define InstallerSuffix ""
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
AlwaysShowDirOnReadyPage=yes
DefaultGroupName=Elink
DisableProgramGroupPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Elink-{#AppVersion}-windows-x64-Setup{#InstallerSuffix}
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
