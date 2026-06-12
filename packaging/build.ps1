<#
.SYNOPSIS
  개별 onefile exe 3종 빌드(PyInstaller onefile). 타깃 PC엔 Python 불필요. 파일을 따로따로 받는다.
.NOTES
  결과: packaging\dist\periscribe.exe(컬렉터/프록시/guardian, GUI 설치) + periscribe-agent.exe(컨테이너 런처)
        + periscribe-proxy.exe(프록시 ON/OFF GUI 토글).
  컬렉터·프록시는 분리됐고(컬렉터가 프록시를 안 띄움) 가디언도 없어 onefile 동시추출 경쟁이 사라짐 →
  단일 exe 배포로 충분하다(예전엔 proxy on 이 3개를 동시에 띄워 _MEI 추출 경쟁 크래시가 났음).
.EXAMPLE
  .\build.ps1
#>
# 주의: pip·PyInstaller 는 진행 로그를 stderr 에 뱉는다. $ErrorActionPreference=Stop 이면
# Windows PowerShell 5.1 이 native stderr 한 줄까지 종료 오류(NativeCommandError)로 취급해
# exit 0 인데도 빌드가 중단된다(간헐적). 그래서 Continue 로 두고 실패는 $LASTEXITCODE 로 판정한다.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\collector")
$dist = Join-Path $here "dist"

python -m pip install --quiet --upgrade pyinstaller customtkinter cryptography pystray pillow
# pip 실패는 치명적이지 않다(이미 설치돼 있으면 OK, 진짜 없으면 PyInstaller 가 실패한다).
# pystray/pillow: 컬렉터 트레이 아이콘용. cryptography: 프록시 서버 인증서용.

# 옛 onedir 산출물(폴더 번들 + zip) 정리.
if (Test-Path (Join-Path $dist "periscribe") -PathType Container) { Remove-Item (Join-Path $dist "periscribe") -Recurse -Force }
if (Test-Path (Join-Path $dist "periscribe-win.zip")) { Remove-Item (Join-Path $dist "periscribe-win.zip") -Force }

python -m PyInstaller --noconfirm --onefile --windowed --name periscribe `
  --paths "$collector" `
  --collect-all customtkinter `
  --collect-all pystray `
  --collect-all PIL `
  --distpath $dist `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_periscribe.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(periscribe), exit=$LASTEXITCODE" }

$exe = Join-Path $dist "periscribe.exe"
if (Test-Path $exe) { Write-Host "빌드 완료: $exe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe.exe 없음" }

# periscribe-agent.exe — VS Code 없이 컨테이너에서 Claude Code 실행하는 런처.
# 대화형 docker run -it 가 필요하므로 console exe(--console). 표준 라이브러리만 써서 작다
# (customtkinter/cryptography 미수집).
python -m PyInstaller --noconfirm --onefile --console --name periscribe-agent `
  --paths "$collector" `
  --distpath $dist `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_agent.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(periscribe-agent), exit=$LASTEXITCODE" }

$agentExe = Join-Path $dist "periscribe-agent.exe"
if (Test-Path $agentExe) { Write-Host "빌드 완료: $agentExe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe-agent.exe 없음" }

# periscribe-proxy.exe — 프록시 '서버' 본체(독립 실행). 더블클릭/CLI 로 실행, 트래픽 가로채·차단·게이팅.
# 머신 라우팅(on/off)은 컬렉터가 한다. GUI(customtkinter) 상태창 + cryptography(인증서) 포함.
python -m PyInstaller --noconfirm --onefile --windowed --name periscribe-proxy `
  --paths "$collector" `
  --collect-all customtkinter `
  --distpath $dist `
  --workpath (Join-Path $here "build") `
  --specpath $here `
  (Join-Path $here "run_proxyserver.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(periscribe-proxy), exit=$LASTEXITCODE" }

$proxyExe = Join-Path $dist "periscribe-proxy.exe"
if (Test-Path $proxyExe) { Write-Host "빌드 완료: $proxyExe" -ForegroundColor Green }
else { throw "빌드 실패: periscribe-proxy.exe 없음" }
