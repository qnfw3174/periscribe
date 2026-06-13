<#
.SYNOPSIS
  업계 표준 패키징: onedir 번들(PyInstaller) → Inno Setup per-user 설치 프로그램.
.NOTES
  결과: packaging\dist\periscribe-setup.exe (설치 프로그램 하나).
  설치 시 %LOCALAPPDATA%\Programs\Periscribe 에 3 프로그램(periscribe.exe 컬렉터 / periscribe-proxy.exe
  서버 / periscribe-agent.exe 런처)이 _internal 런타임 공유로 깔린다. 실행 시 임시추출(_MEI) 없음 →
  형제 _MEI 삭제 race 부류 원천 소멸(예전 onefile 의 고질병 제거).
  데이터(config/certs/logs)는 %LOCALAPPDATA%\Periscribe 에 별도 보존.
.EXAMPLE
  .\build.ps1
#>
$ErrorActionPreference = "Continue"   # pip/PyInstaller stderr 로그를 종료오류로 취급하지 않게(5.1)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $here "dist"

python -m pip install --quiet --upgrade pyinstaller customtkinter cryptography pystray pillow
# pystray/pillow: 컬렉터 트레이. cryptography: 프록시 서버 인증서. (import 분석으로 자동 포함)

# 1) onedir 번들 빌드 (periscribe.spec: 3 exe 한 폴더에 런타임 공유)
if (Test-Path (Join-Path $dist "Periscribe") -PathType Container) { Remove-Item (Join-Path $dist "Periscribe") -Recurse -Force }
python -m PyInstaller --noconfirm --clean `
  --distpath $dist --workpath (Join-Path $here "build") `
  (Join-Path $here "periscribe.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller(onedir) 실패, exit=$LASTEXITCODE" }
if (-not (Test-Path (Join-Path $dist "Periscribe\periscribe.exe"))) { throw "빌드 실패: dist\Periscribe 없음" }
Write-Host "onedir 번들 완료: $dist\Periscribe" -ForegroundColor Green

# 2) Inno Setup 으로 설치 프로그램 빌드
$iscc = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup(ISCC.exe) 미설치 — winget install JRSoftware.InnoSetup 후 재시도" }
& $iscc (Join-Path $here "periscribe.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 컴파일 실패, exit=$LASTEXITCODE" }

$setup = Join-Path $dist "periscribe-setup.exe"
if (Test-Path $setup) { Write-Host "설치 프로그램 완료: $setup" -ForegroundColor Green }
else { throw "빌드 실패: periscribe-setup.exe 없음" }
