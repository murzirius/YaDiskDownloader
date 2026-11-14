; Inno Setup script. Built by tools/build_installer.py, which passes
; AppVersion, StageDir (an unpacked Windows ZIP), OutputDir and OutputBase.
; Per-user install: no administrator rights, nothing outside the user profile.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef StageDir
  #error StageDir is required
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\release"
#endif
#ifndef OutputBase
  #define OutputBase "YaDiskDownloader-Setup"
#endif

[Setup]
AppId={{B7C1F7A4-3E52-4C8E-9A0B-6D5F0E2C41A9}
AppName=YaDiskDownloader
AppVersion={#AppVersion}
AppPublisher=murzirius
AppPublisherURL=https://github.com/murzirius/YaDiskDownloader
AppSupportURL=https://github.com/murzirius/YaDiskDownloader/issues
VersionInfoVersion={#AppVersion}.0
DefaultDirName={localappdata}\Programs\YaDiskDownloader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#StageDir}\LICENSE
UninstallDisplayIcon={app}\YaDiskDownloader.exe
UninstallDisplayName=YaDiskDownloader
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
; The program updates itself in place, so never trust file dates or versions.
; Never sign: the project has no certificate (see docs/windows-installer.md).

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительно:"; Flags: unchecked

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\YaDiskDownloader"; Filename: "{app}\YaDiskDownloader.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\YaDiskDownloader"; Filename: "{app}\YaDiskDownloader.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\YaDiskDownloader.exe"; Description: "Запустить YaDiskDownloader"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Files created after installation by the in-app updater or the old in-app installer.
Type: files; Name: "{app}\install-files.json"
Type: files; Name: "{app}\.update-*.exe"
Type: filesandordirs; Name: "{app}\source"
Type: filesandordirs; Name: "{app}\third_party"
Type: filesandordirs; Name: "{app}\docs"

[Code]
function StateFolder: String;
begin
  { /StateDir=... exists for automated tests only. }
  Result := RemoveBackslash(ExpandConstant('{param:StateDir|{localappdata}\YaDiskDownloader}'));
end;

function WantsHistoryRemoved: Boolean;
begin
  if UninstallSilent then
    { Silent removal keeps history unless /DeleteHistory=yes is given. }
    Result := ExpandConstant('{param:DeleteHistory|no}') = 'yes'
  else
    Result := MsgBox('Программа удалена. Удалить также историю и настройки?' + #13#10 + #13#10 +
              StateFolder + #13#10 + #13#10 +
              'Скачанные файлы не будут затронуты. Нажмите «Нет», чтобы сохранить историю на случай повторной установки.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { The history and settings folder is separate from the program folder.
    Downloaded files (Downloads\YaDiskDownloader) are never touched. }
  if (CurUninstallStep = usPostUninstall) and DirExists(StateFolder) and
     (CompareText(ExtractFileName(StateFolder), 'YaDiskDownloader') = 0) and WantsHistoryRemoved then
    DelTree(StateFolder, True, True, True);
end;
