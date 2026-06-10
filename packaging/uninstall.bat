@echo off
echo ============================================
echo   Periscribe Collector 제거
echo ============================================
echo.
echo [1/5] 웹에 제거 신호 전송(자동 revoke + 제거 표시)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Join-Path $env:LOCALAPPDATA 'Periscribe\config.json'; if (Test-Path $p) { $c = Get-Content $p -Raw | ConvertFrom-Json; if ($c.device_token -and $c.ingest_url) { Invoke-RestMethod -Method Post -Uri $c.ingest_url -ContentType 'application/json' -TimeoutSec 10 -Body (ConvertTo-Json @{device_token=$c.device_token; uninstall=$true}) | Out-Null } } } catch {}" >nul 2>&1
echo [2/5] 자동 시작 해제...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v PeriscribeCollector /f >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v PeriscribeGuardian /f >nul 2>&1
schtasks /Delete /TN PeriscribeCollector /F >nul 2>&1
echo [3/5] 실행 중인 수집기 종료(이름 기준)...
taskkill /IM periscribe.exe /F /T >nul 2>&1
taskkill /IM periscribe-proxy.exe /F /T >nul 2>&1
echo [4/5] 실행 중인 수집기 종료(실행명령 기준, 확실히)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$me=$PID; Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'periscribe\.exe.{0,60}\brun\b' -or $_.CommandLine -match '-m\s+periscribe\b') -and $_.ProcessId -ne $me } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo [5/5] 데이터(토큰/체크포인트/로그) 삭제...
rmdir /S /Q "%LOCALAPPDATA%\Periscribe" >nul 2>&1
echo.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'periscribe\.exe.{0,60}\brun\b' -or $_.CommandLine -match '-m\s+periscribe\b' }) { Write-Host '결과: 아직 실행 중인 수집기가 있습니다. 재부팅을 권장합니다.' } else { Write-Host '결과: 수집기 종료 + 자동시작 해제 + 웹 제거 표시 완료.' }"
echo.
echo  - 다운로드한 periscribe.exe 파일은 직접 삭제하세요.
echo  - 제거 시 오프라인이었다면 웹에 표시가 안 될 수 있습니다(관리자가 머신 관리에서 삭제).
echo.
pause
