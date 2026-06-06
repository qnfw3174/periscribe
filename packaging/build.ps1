<#
.SYNOPSIS
  단일 periscribe.exe 빌드(PyInstaller onefile, console). 타깃 PC엔 Python 불필요.
.NOTES
  Collector는 표준 라이브러리만 쓰므로 추가 hidden-import가 없다.
  결과: packaging\dist\periscribe.exe
  사용: periscribe.exe install --token <T> --url <U>   /   periscribe.exe run
.EXAMPLE
  .\build.ps1
#>
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\collector")

python -m pip install --quiet --upgrade pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name periscribe `
  --paths "$collector" `
  --distpath (Join-Path $here "dist") `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_periscribe.py")

$exe = Join-Path $here "dist\periscribe.exe"
if (Test-Path $exe) { Write-Host "빌드 완료: $exe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe.exe 없음" }
