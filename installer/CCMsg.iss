; CC Msg (agent-bridge, repo CCMessage) Windows installer - Inno Setup 6.5+
; Build: tools\build_desktop.ps1 calls  ISCC /DAppVersion=x.y.z /DSourceDir=<dist\CCMsg> /DOutputDir=<dist> installer\CCMsg.iss
; - per-user install to %LOCALAPPDATA%\Programs\AgentBridge, no admin / UAC
;   (the folder keeps its old name on purpose: project hooks already point at .../AgentBridge/bus/bus.exe)
; - 0.3.0 renamed AgentBridge -> CC Msg (CCMsg.exe); upgrading removes the old shortcuts and AgentBridge.exe
; - same AppId => upgrades in place; uninstaller registered under the current user
; - files that are still in use (bus.exe hooks waiting in the background, after-quit helpers) are renamed
;   out of the way instead of blocking the upgrade; the leftovers are removed on the next upgrade / uninstall
; - silent update from the app:  Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RELAUNCH=1 [/RELAUNCHARGS="--port N"]

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\CCMsg"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
; /DTestBuild=1 builds an isolated "CC Msg TEST" (own AppId, folder, mutex, tray, autostart value) for tests
#ifdef TestBuild
  #define MyAppId "{{0D5A1C2B-7E31-4F7A-9C00-7E57AB13C0DE}"
  #define MyName "CC Msg TEST"
  #define MyDir "AgentBridgeTEST"
  #define MyMutex "Local\AgentBridgeDesktop-TEST"
  #define MyRun "CC Msg TEST"
  #define MyOut "CCMsg-TEST-Setup-"
#else
  #define MyAppId "{{6F3C1B2A-8E4D-4C7A-9B1E-2A5D7C9E0F13}"
  #define MyName "CC Msg"
  #define MyDir "AgentBridge"
  #define MyMutex "Local\AgentBridgeDesktop"
  #define MyRun "CC Msg"
  #define MyOut "CCMsg-Setup-"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyName}
AppVersion={#AppVersion}
AppVerName={#MyName} {#AppVersion}
AppPublisher=CCMessage
AppPublisherURL=https://github.com/helefee/CCMessage
AppSupportURL=https://github.com/helefee/CCMessage/issues
AppUpdatesURL=https://github.com/helefee/CCMessage/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={userpf}\{#MyDir}
DefaultGroupName={#MyName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#MyOut}{#AppVersion}
SetupIconFile=..\agent_bridge\icon.ico
UninstallDisplayIcon={app}\CCMsg.exe
UninstallDisplayName={#MyName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=CCMsg.exe,AgentBridge.exe
RestartApplications=no
SetupMutex=AgentBridgeSetupMutex{#MyDir}
ShowLanguageDialog=no
LanguageDetectionMethod=none

[Languages]
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl,CCMsg.zh.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[InstallDelete]
; 0.2.x and earlier: the old Chinese-named shortcuts (OldAppTitle) pointing at AgentBridge.exe
Type: files; Name: "{autoprograms}\{cm:OldAppTitle}.lnk"
Type: files; Name: "{autoprograms}\{cm:OldUninstall}.lnk"
Type: files; Name: "{autodesktop}\{cm:OldAppTitle}.lnk"
Type: files; Name: "{app}\AgentBridge.exe"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{cm:AppTitle}"; Filename: "{app}\CCMsg.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\{cm:Uninstall}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{cm:AppTitle}"; Filename: "{app}\CCMsg.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\CCMsg.exe"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\CCMsg.exe"; Parameters: "{param:RELAUNCHARGS}"; Flags: nowait; Check: ShouldRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

{ wait up to ~20 s for a running CC Msg / AgentBridge window (it holds this mutex) to quit by itself }
procedure WaitForApp;
var
  i: Integer;
begin
  i := 0;
  while CheckForMutexes('{#MyMutex}') and (i < 80) do
  begin
    Sleep(250);
    i := i + 1;
  end;
end;

{ move every old program file out of the way: delete it, or if it is in use (a bus.exe hook waiting in
  the background, an after-quit helper) rename it - Windows allows renaming a running exe / loaded dll }
procedure ClearDir(const Dir: String);
var
  FR: TFindRec;
  P: String;
begin
  if FindFirst(Dir + '\*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        P := Dir + '\' + FR.Name;
        if FR.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          ClearDir(P)
        else if (CompareText(Copy(FR.Name, 1, 5), 'unins') <> 0) then
        begin
          if not DeleteFile(P) then
            RenameFile(P, P + '.old-' + GetDateTimeString('yyyymmddhhnnsszzz', #0, #0));
        end;
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
  RemoveDir(Dir);
end;

{ 0.4.2+: CC Msg may be hidden in the tray (closing the window only hides it). Ask the running one to quit:
  CCMsg.exe --quit posts a quit message to its tray window and waits for its mutex to go away.
  (an older CCMsg.exe ignores --quit with a usage error; it has no tray, Restart Manager closes it) }
procedure QuitRunningApp;
var
  rc: Integer;
begin
  if CheckForMutexes('{#MyMutex}') and FileExists(ExpandConstant('{app}\CCMsg.exe')) then
    Exec(ExpandConstant('{app}\CCMsg.exe'), '--quit', '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    QuitRunningApp;
    WaitForApp;
    if DirExists(ExpandConstant('{app}')) then
      ClearDir(ExpandConstant('{app}'));
  end;
#ifdef TestBuild
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\instance.txt'), 'TEST', False);
#endif
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    QuitRunningApp;
    WaitForApp;
    { autostart entry written by the app (tray menu / settings): remove it with the app }
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyRun}');
  end;
end;
