; ClipFactory installer (Inno Setup 6). Built by packaging/build_windows.py:
;   ISCC /DAppVersion=2.0.0 /DAppVersionNumeric=2.0.0.0 /DSourceDir=dist\ClipFactory /DOutputDir=dist /DIconFile=... installer.iss
;
; Per-user install (no administrator rights): program in %LOCALAPPDATA%\Programs\ClipFactory,
; user data in %LOCALAPPDATA%\ClipFactory — installs, updates and uninstalls never touch the data
; (the uninstaller only removes it if the user explicitly asks).
;
; Silent update (started by the app): ClipFactory-Setup-X.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /UPDATE
; waits until the running app has exited, replaces the program files and starts the new version.

#ifndef AppVersion
  #error AppVersion must be defined
#endif

[Setup]
; Never change AppId: it is how an update finds the existing installation.
AppId={{8F3C5E2A-6B1D-4C7E-9A42-3D5B7E1F0C21}
AppName=ClipFactory
AppVersion={#AppVersion}
AppVerName=ClipFactory {#AppVersion}
AppPublisher=ClipFactory
AppPublisherURL=https://github.com/cursedba1ls/ClipFactory
AppSupportURL=https://github.com/cursedba1ls/ClipFactory/issues
AppUpdatesURL=https://github.com/cursedba1ls/ClipFactory/releases
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductVersion={#AppVersionNumeric}
VersionInfoDescription=ClipFactory Setup
DefaultDirName={localappdata}\Programs\ClipFactory
DefaultGroupName=ClipFactory
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ClipFactory-Setup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\ClipFactory.exe
UninstallDisplayName=ClipFactory
Compression=lzma2/max
SolidCompression=yes
; The 64-bit compressor runs in its own process (the compiler itself is 32-bit and runs out of memory).
LZMAUseSeparateProcess=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
CloseApplications=no
RestartApplications=no
ShowLanguageDialog=no
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Program code of the previous version: removed so no stale module survives an update.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\ffmpeg"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\ClipFactory"; Filename: "{app}\ClipFactory.exe"; Comment: "Серии вертикальных видео для TikTok"
Name: "{autodesktop}\ClipFactory"; Filename: "{app}\ClipFactory.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ClipFactory.exe"; Description: "{cm:LaunchProgram,ClipFactory}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\ClipFactory.exe"; Parameters: "--updated"; Flags: nowait; Check: IsUpdate

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\ffmpeg"

[Code]
const
  AppMutex = 'ClipFactoryAppMutex';

function CmdLineHas(const S: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), S) = 0 then
      Result := True;
end;

function IsUpdate: Boolean;
begin
  Result := CmdLineHas('/UPDATE');
end;

{ Wait until ClipFactory has closed: silently (update started by the app) or by asking the user. }
function WaitForAppClosed(Silent: Boolean): Boolean;
var
  Tries: Integer;
begin
  Result := True;
  Tries := 0;
  while CheckForMutexes(AppMutex) do
  begin
    if Silent then
    begin
      if Tries >= 240 then
      begin
        Log('ClipFactory is still running after 2 minutes; aborting.');
        Result := False;
        Exit;
      end;
      Sleep(500);
      Tries := Tries + 1;
    end
    else if MsgBox('ClipFactory сейчас открыт. Закройте программу и нажмите «ОК».', mbInformation, MB_OKCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function InitializeSetup: Boolean;
begin
  Result := WaitForAppClosed(WizardSilent or IsUpdate);
end;

function InitializeUninstall: Boolean;
begin
  Result := WaitForAppClosed(UninstallSilent);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
  begin
    DataDir := ExpandConstant('{localappdata}\ClipFactory');
    if DirExists(DataDir) then
      if MsgBox('Удалить также все ваши проекты, видео и настройки?' + #13#10#13#10 +
                'Папка: ' + DataDir + #13#10#13#10 +
                'Если вы собираетесь установить ClipFactory снова, нажмите «Нет» — всё сохранится.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
