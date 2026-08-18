; Periscribe 설치 프로그램 (Inno Setup) — per-user(무관리자) 설치.
; onedir 번들(dist\Periscribe)을 %LOCALAPPDATA%\Programs\Periscribe 에 설치한다.
; 실행 시 임시추출(_MEI) 없음 → 형제 _MEI 삭제 race 부류 원천 소멸.
; 데이터(config/certs/logs)는 %LOCALAPPDATA%\Periscribe 에 별도(재설치에도 보존, 제거 시 삭제).

#define MyAppName "Periscribe"
#define MyAppVersion "0.2.1"
#define MyAppPublisher "Periscribe"
; 경로는 이 .iss 파일 위치 기준(SourcePath)으로 잡는다 — 빌드하는 사람의 체크아웃 위치에 무관해야 한다.
#define PkgDir RemoveBackslash(SourcePath)
#define DistDir PkgDir + "\dist\periscribe"

[Setup]
AppId={{6F3C9A2E-1B4D-4E7A-9C21-A1B2C3D4E5F6}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Periscribe
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#PkgDir}\dist
OutputBaseFilename=periscribe-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\periscribe.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "kr"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#PkgDir}\uninstall-cleanup.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Periscribe"; Filename: "{app}\periscribe.exe"
Name: "{userdesktop}\Periscribe"; Filename: "{app}\periscribe.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\periscribe.exe"; Description: "Periscribe 시작(토큰 입력)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Periscribe"

[Code]
// 설치 전: 실행 중인 인스턴스 종료(업데이트 시 파일 잠금 해제)
function PrepareToInstall(var NeedsRestart: Boolean): String;
var rc: Integer;
begin
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process periscribe,periscribe-proxy,periscribe-agent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"',
    '', SW_HIDE, ewWaitUntilTerminated, rc);
  Result := '';
end;

// 제거: 파일 삭제 직전에 revoke 신호 + 자동시작 해제 + 프로세스 종료
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var rc: Integer;
begin
  if CurUninstallStep = usUninstall then
    Exec('powershell.exe',
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\uninstall-cleanup.ps1') + '"',
      '', SW_HIDE, ewWaitUntilTerminated, rc);
end;
