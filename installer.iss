[Setup]
AppId={{FA63F85D-822B-4A4F-B2D1-5817445DFC1C}
AppName=VPN-X
AppVersion={#AppVersion}
AppPublisher=VPN-X
DefaultDirName={autopf}\VPN-X
DisableDirPage=no
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\VPN-X.exe
VersionInfoVersion={#AppVersion}.0.0
OutputDir={#OutputFolder}
OutputBaseFilename=VPN-X-V{#AppVersion}-Setup
Compression=lzma2/fast
CloseApplications=no
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "{#ChineseMessages}"

[CustomMessages]
english.RunVPNX=Run VPN-X
english.UnsafeDirectory=Choose an empty folder on a local fixed drive with administrator-only write access (for example, a new folder under Program Files). The parent folder must already exist.
english.AlreadyInstalled=VPN-X is already installed. To upgrade it, open Settings and check for updates. To reinstall, exit and uninstall the current version first.
english.AppBusy=Quit VPN-X from its tray menu before uninstalling. If a proxy was left active, reopen VPN-X to restore the network and then quit.
english.SelfCheckFailed=VPN-X failed its installation checks. Setup will not activate this installation.
chinesesimp.RunVPNX=运行 VPN-X
chinesesimp.UnsafeDirectory=请选择本地固定磁盘上的空目录，且只有管理员可写。例如 Program Files 下的新目录。上一级目录必须已存在。
chinesesimp.AlreadyInstalled=VPN-X 已安装。升级请在设置中检查更新。若要重装，请先从托盘退出，再卸载当前安装版。
chinesesimp.AppBusy=请先在托盘菜单中选择「关闭代理并退出」。如代理仍未恢复，请打开 VPN-X 恢复网络后再退出。
chinesesimp.SelfCheckFailed=VPN-X 安装自检失败，本次安装不会启用。

[Files]
Source: "{#Launcher}"; Flags: dontcopy; DestName: "VPN-X-launcher.exe"
Source: "{#Launcher}"; DestDir: "{app}"; DestName: "VPN-X.exe"; Flags: ignoreversion
Source: "{#Payload}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#CatalogSource}"; DestDir: "{app}"; DestName: "catalog-source.json"; Flags: onlyifdoesntexist

[Icons]
Name: "{commonprograms}\VPN-X"; Filename: "{app}\VPN-X.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\VPN-X.exe"; Description: "{cm:RunVPNX}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\current-version.txt"
Type: files; Name: "{app}\vpn-x-installed.txt"
Type: files; Name: "{app}\update.lock"
Type: filesandordirs; Name: "{app}\versions"
Type: filesandordirs; Name: "{app}\updates"

[Code]
const
  InstallKey = 'SOFTWARE\VPN-X';

var
  UpdateGuard: THandle;

function CreateFileW(Name: String; Access, Share: LongWord; Security: LongWord; Creation, Flags: LongWord; TemplateFile: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function InitializeSetup: Boolean;
var
  Existing: String;
begin
  Result := not RegQueryStringValue(HKLM64, InstallKey, 'InstallDir', Existing);
  if not Result then MsgBox(CustomMessage('AlreadyInstalled'), mbInformation, MB_OK);
end;

function ValidateDirectory(Mode: String): Boolean;
var
  Status: Integer;
begin
  Result := Exec(ExpandConstant('{tmp}\VPN-X-launcher.exe'),
    Mode + ' "' + RemoveBackslashUnlessRoot(WizardDirValue) + '"',
    '', SW_HIDE, ewWaitUntilTerminated, Status) and (Status = 0);
end;

procedure InitializeWizard;
begin
  ExtractTemporaryFile('VPN-X-launcher.exe');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Result := ValidateDirectory('--check-new-directory');
    if not Result then MsgBox(CustomMessage('UnsafeDirectory'), mbError, MB_OK);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not ValidateDirectory('--check-new-directory') then Result := CustomMessage('UnsafeDirectory');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Status: Integer;
  Runtime, Exe, Root: String;
begin
  if CurStep <> ssPostInstall then Exit;
  Root := ExpandConstant('{app}');
  Runtime := Root + '\versions\{#AppVersion}';
  Exe := Runtime + '\VPN-X.exe';
  if not Exec(Exe, '--check-startup', Runtime, SW_HIDE, ewWaitUntilTerminated, Status) or (Status <> 0) or
     not Exec(Exe, '--check-clash', Runtime, SW_HIDE, ewWaitUntilTerminated, Status) or (Status <> 0) then
    RaiseException(CustomMessage('SelfCheckFailed'));
  if not SaveStringToFile(Root + '\current-version.txt', '{#AppVersion}' + #13#10, False) or
     not SaveStringToFile(Root + '\vpn-x-installed.txt', 'VPN-X' + #13#10, False) or
     not RegWriteStringValue(HKLM64, InstallKey, 'InstallDir', Root) then
    RaiseException(CustomMessage('SelfCheckFailed'));
end;

function InitializeUninstall: Boolean;
begin
  UpdateGuard := CreateFileW(ExpandConstant('{app}\update.lock'), $40000000, 0, 0, 4, $80, 0);
  Result := UpdateGuard <> THandle(-1);
  if Result then begin
    Result := not FileExists(ExpandConstant('{localappdata}\VPN-X\vpn-x.lock')) and
              not FileExists(ExpandConstant('{localappdata}\VPN-X\restore.json.dpapi'));
    if not Result then CloseHandle(UpdateGuard);
  end;
  if not Result then MsgBox(CustomMessage('AppBusy'), mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    RegDeleteValue(HKLM64, InstallKey, 'InstallDir');
    CloseHandle(UpdateGuard);
    DeleteFile(ExpandConstant('{app}\update.lock'));
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
