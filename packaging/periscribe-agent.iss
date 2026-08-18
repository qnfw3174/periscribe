; Periscribe 에이전트 런처 설치 프로그램 (Inno Setup) — per-user(무관리자), 단독.
; VS Code 없이 컨테이너에서 Claude Code 를 돌리는 런처. Docker 필요한 PC에만 따로 설치.
; onedir(dist\periscribe-agent)을 %LOCALAPPDATA%\Programs\Periscribe Agent 에 설치.

#define MyAppName "Periscribe Agent"
#define MyAppVersion "0.2.1"
; 경로는 이 .iss 파일 위치 기준(SourcePath)으로 잡는다 — 빌드하는 사람의 체크아웃 위치에 무관해야 한다.
#define PkgDir RemoveBackslash(SourcePath)
#define DistDir PkgDir + "\dist\periscribe-agent"

[Setup]
AppId={{3B9E2D6C-8F4E-4BA7-C1D5-E6F70FF82B3C}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Periscribe
DefaultDirName={localappdata}\Programs\Periscribe Agent
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#PkgDir}\dist
OutputBaseFilename=periscribe-agent-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\periscribe-agent.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "kr"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Periscribe Agent"; Filename: "{app}\periscribe-agent.exe"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var rc: Integer;
begin
  Exec('powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process periscribe-agent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"',
    '', SW_HIDE, ewWaitUntilTerminated, rc);
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var rc: Integer;
begin
  if CurUninstallStep = usUninstall then
    Exec('powershell.exe',
      '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process periscribe-agent -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"',
      '', SW_HIDE, ewWaitUntilTerminated, rc);
end;
