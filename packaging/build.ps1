<#
.SYNOPSIS
  Periscribe onedir 번들 빌드. periscribe.exe(컬렉터/프록시/guardian) + periscribe-proxy.exe(프록시 GUI)
  + periscribe-agent.exe(컨테이너 런처)가 하나의 _internal 을 공유하는 onedir 폴더로 빌드되고,
  periscribe-win.zip 으로 압축된다. 타깃 PC엔 Python 불필요.
.NOTES
  onedir(폴더 배포)이라 실행 시 _MEI 추출이 없다 → proxy on 이 컬렉터+프록시+guardian 을 동시에 띄워도
  onefile 동시추출 경쟁(_rust/base_library.zip/_socket 누락 크래시)이 발생하지 않는다.
  설치: zip 풀고 periscribe.exe 실행 → %LOCALAPPDATA%\Periscribe\app 로 복사 후 자동시작 등록.
  결과물: packaging\dist\periscribe\  +  packaging\dist\periscribe-win.zip
.EXAMPLE
  .\build.ps1
#>
# 주의: pip·PyInstaller 는 진행 로그를 stderr 에 뱉는다. $ErrorActionPreference=Stop 이면
# Windows PowerShell 5.1 이 native stderr 한 줄까지 종료 오류로 취급해 빌드가 중단된다(간헐적).
# 그래서 Continue 로 두고 실패는 $LASTEXITCODE 로 판정한다.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = Join-Path $here "dist"

python -m pip install --quiet --upgrade pyinstaller customtkinter cryptography
# pip 실패는 치명적이지 않다(이미 설치돼 있으면 OK, 진짜 없으면 PyInstaller 가 실패한다).

# 기존 산출물 정리(이전 onefile exe/폴더 혼재 방지)
if (Test-Path (Join-Path $dist "periscribe")) { Remove-Item (Join-Path $dist "periscribe") -Recurse -Force }
if (Test-Path (Join-Path $dist "periscribe-win.zip")) { Remove-Item (Join-Path $dist "periscribe-win.zip") -Force }

python -m PyInstaller --noconfirm `
  --distpath $dist `
  --workpath (Join-Path $here "build") `
  (Join-Path $here "periscribe-bundle.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 실패(bundle), exit=$LASTEXITCODE" }

$appDir = Join-Path $dist "periscribe"
foreach ($exe in @("periscribe.exe","periscribe-proxy.exe","periscribe-agent.exe")) {
  if (-not (Test-Path (Join-Path $appDir $exe))) { throw "빌드 실패: $exe 없음" }
}
Write-Host "번들 빌드 완료: $appDir" -ForegroundColor Green

# onedir 폴더를 zip 으로 압축(웹/릴리스 배포물). zip 루트에 'periscribe\' 폴더가 들어간다.
$zip = Join-Path $dist "periscribe-win.zip"
Compress-Archive -Path $appDir -DestinationPath $zip -CompressionLevel Optimal -Force
if (-not (Test-Path $zip)) { throw "zip 생성 실패" }
$mb = [math]::Round((Get-Item $zip).Length/1MB,1)
Write-Host "배포물 생성: $zip ($mb MB)" -ForegroundColor Green
