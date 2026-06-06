<#
.SYNOPSIS
  단일 periscribe.exe 빌드(PyInstaller onefile, console). 타깃 PC엔 Python 불필요.
.NOTES
  수집(run) 런타임은 표준 라이브러리만 쓴다. customtkinter 는 GUI 설치 창에서만 쓰이고
  exe 에 번들된다(--collect-all). 결과: packaging\dist\periscribe.exe
  사용: periscribe.exe (더블클릭 → GUI 설치)  /  periscribe.exe run
.EXAMPLE
  .\build.ps1
#>
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\collector")

python -m pip install --quiet --upgrade pyinstaller customtkinter cryptography
python -m PyInstaller --noconfirm --onefile --windowed --name periscribe `
  --paths "$collector" `
  --collect-all customtkinter `
  --distpath (Join-Path $here "dist") `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_periscribe.py")

$exe = Join-Path $here "dist\periscribe.exe"
if (Test-Path $exe) { Write-Host "빌드 완료: $exe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe.exe 없음" }
