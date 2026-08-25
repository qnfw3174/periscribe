; Periscribe 프록시 '서버' 설치 프로그램 (Inno Setup) — per-user(무관리자), 단독.
; 컬렉터와 별개 프로그램. 지금은 각 머신에서, 나중엔 중앙 서버 1대에 이것만 설치한다.
; onedir(dist\periscribe-proxy)을 %LOCALAPPDATA%\Programs\Periscribe Proxy 에 설치. 임시추출 없음.
; 데이터(인증서 ca.pem 등)는 %LOCALAPPDATA%\Periscribe 공유 — 제거해도 이 데이터는 안 지운다(컬렉터와 공유).

#define MyAppName "Periscribe Proxy"
#define MyAppVersion "0.2.2"
; 경로는 이 .iss 파일 위치 기준(SourcePath)으로 잡는다 — 빌드하는 사람의 체크아웃 위치에 무관해야 한다.
#define PkgDir RemoveBackslash(SourcePath)
#define DistDir PkgDir + "\dist\periscribe-proxy"

[Setup]
AppId={{2A8F1C5B-7E3D-4A96-B0C4-D5E60FE71A2B}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Periscribe
DefaultDirName={localappdata}\Programs\Periscribe Proxy
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#PkgDir}\dist
OutputBaseFilename=periscribe-proxy-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\periscribe-proxy.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "kr"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Periscribe 프록시 서버"; Filename: "{app}\periscribe-proxy.exe"

[Run]
Filename: "{app}\periscribe-proxy.exe"; Description: "프록시 서버 실행"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var rc: Integer;
begin
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process periscribe-proxy -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"',
    '', SW_HIDE, ewWaitUntilTerminated, rc);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var rc: Integer;
begin
  if CurUninstallStep = usUninstall then
    Exec('powershell.exe',
      '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process periscribe-proxy -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"',
      '', SW_HIDE, ewWaitUntilTerminated, rc);
end;
