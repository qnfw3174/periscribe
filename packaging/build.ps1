<#
.SYNOPSIS
  업계 표준 패키징: 역할별 onedir 번들 + 역할별 Inno Setup 설치 프로그램(3개 분리).
.NOTES
  결과(packaging\dist):
    periscribe-setup.exe        - 컬렉터(상주 에이전트 + 트레이 + 프록시 라우팅)
    periscribe-proxy-setup.exe  - 프록시 서버(단독; 중앙 서버에도 이것만 설치)
    periscribe-agent-setup.exe  - 에이전트 런처(Docker 필요한 PC만)
  각 프로그램은 onedir(파일 디스크 상주) -> 실행 시 임시추출(_MEI) 없음. 역할이 달라 따로 설치/제거.
  데이터(config/certs/logs)는 %LOCALAPPDATA%\Periscribe 공유.
.EXAMPLE
  .\build.ps1
#>
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\collector")
$dist = Join-Path $here "dist"
$work = Join-Path $here "build"

python -m pip install --quiet --upgrade pyinstaller customtkinter cryptography pystray pillow

# 옛 산출 폴더 정리
foreach ($n in @("periscribe", "periscribe-proxy", "periscribe-agent", "Periscribe")) {
  $p = Join-Path $dist $n
  if (Test-Path $p -PathType Container) { Remove-Item $p -Recurse -Force }
}

# ---- 1) 역할별 onedir 빌드 ----
# 컬렉터(GUI+트레이): customtkinter/pystray/PIL
python -m PyInstaller --noconfirm --clean --onedir --windowed --name periscribe `
  --paths "$collector" --collect-all customtkinter --collect-all pystray --collect-all PIL `
  --hidden-import pystray._win32 --hidden-import PIL._tkinter_finder `
  --distpath $dist --workpath $work --specpath $here (Join-Path $here "run_periscribe.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: periscribe (exit $LASTEXITCODE)" }

# 프록시 서버(상태창): customtkinter, cryptography(자동)
python -m PyInstaller --noconfirm --clean --onedir --windowed --name periscribe-proxy `
  --paths "$collector" --collect-all customtkinter `
  --distpath $dist --workpath $work --specpath $here (Join-Path $here "run_proxyserver.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: periscribe-proxy (exit $LASTEXITCODE)" }

# 에이전트(콘솔): 표준 라이브러리만
python -m PyInstaller --noconfirm --clean --onedir --console --name periscribe-agent `
  --paths "$collector" `
  --distpath $dist --workpath $work --specpath $here (Join-Path $here "run_agent.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: periscribe-agent (exit $LASTEXITCODE)" }

foreach ($n in @("periscribe", "periscribe-proxy", "periscribe-agent")) {
  if (-not (Test-Path (Join-Path $dist "$n\$n.exe"))) { throw "onedir build missing: dist\$n" }
}
Write-Host "onedir x3 done" -ForegroundColor Green

# ---- 2) 역할별 설치 프로그램 빌드 ----
$iscc = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup ISCC.exe not found - run: winget install JRSoftware.InnoSetup" }

foreach ($iss in @("periscribe.iss", "periscribe-proxy.iss", "periscribe-agent.iss")) {
  & $iscc (Join-Path $here $iss)
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup compile failed: $iss (exit $LASTEXITCODE)" }
}

foreach ($s in @("periscribe-setup.exe", "periscribe-proxy-setup.exe", "periscribe-agent-setup.exe")) {
  $sp = Join-Path $dist $s
  if (Test-Path $sp) { Write-Host "installer done: $sp" -ForegroundColor Green }
  else { throw "build missing: $s" }
}
