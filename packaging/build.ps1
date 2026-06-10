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
# 주의: pip·PyInstaller 는 진행 로그를 stderr 에 뱉는다. $ErrorActionPreference=Stop 이면
# Windows PowerShell 5.1 이 native stderr 한 줄까지 종료 오류(NativeCommandError)로 취급해
# exit 0 인데도 빌드가 중단된다(간헐적). 그래서 Continue 로 두고 실패는 $LASTEXITCODE 로 판정한다.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\collector")

python -m pip install --quiet --upgrade pyinstaller customtkinter cryptography
# pip 실패는 치명적이지 않다(이미 설치돼 있으면 OK, 진짜 없으면 PyInstaller 가 실패한다).

python -m PyInstaller --noconfirm --onefile --windowed --name periscribe `
  --paths "$collector" `
  --collect-all customtkinter `
  --distpath (Join-Path $here "dist") `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_periscribe.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(periscribe), exit=$LASTEXITCODE" }

$exe = Join-Path $here "dist\periscribe.exe"
if (Test-Path $exe) { Write-Host "빌드 완료: $exe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe.exe 없음" }

# periscribe-agent.exe — VS Code 없이 컨테이너에서 Claude Code 실행하는 런처.
# 대화형 docker run -it 가 필요하므로 console exe(--console). 표준 라이브러리만 써서 작다
# (customtkinter/cryptography 미수집).
python -m PyInstaller --noconfirm --onefile --console --name periscribe-agent `
  --paths "$collector" `
  --distpath (Join-Path $here "dist") `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_agent.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(periscribe-agent), exit=$LASTEXITCODE" }

$agentExe = Join-Path $here "dist\periscribe-agent.exe"
if (Test-Path $agentExe) { Write-Host "빌드 완료: $agentExe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe-agent.exe 없음" }

# periscribe-proxy.exe — 프록시 ON/OFF GUI 토글 단독 배포본(더블클릭 → proxy-gui).
# GUI(customtkinter) 번들, proxycert 의 cryptography 는 import 분석으로 자동 포함.
python -m PyInstaller --noconfirm --onefile --windowed --name periscribe-proxy `
  --paths "$collector" `
  --collect-all customtkinter `
  --distpath (Join-Path $here "dist") `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_proxy.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(periscribe-proxy), exit=$LASTEXITCODE" }

$proxyExe = Join-Path $here "dist\periscribe-proxy.exe"
if (Test-Path $proxyExe) { Write-Host "빌드 완료: $proxyExe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe-proxy.exe 없음" }
