@echo off
echo ============================================
echo   Periscribe Collector 제거
echo ============================================
echo.
echo [1/4] 자동 시작 해제...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v PeriscribeCollector /f >nul 2>&1
schtasks /Delete /TN PeriscribeCollector /F >nul 2>&1
echo [2/4] 실행 중인 수집기 종료(이름 기준)...
taskkill /IM periscribe.exe /F /T >nul 2>&1
echo [3/4] 실행 중인 수집기 종료(실행명령 기준, 확실히)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$me=$PID; Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'periscribe\.exe.{0,60}\brun\b' -or $_.CommandLine -match '-m\s+periscribe\b') -and $_.ProcessId -ne $me } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo [4/4] 데이터(토큰/체크포인트/로그) 삭제...
rmdir /S /Q "%LOCALAPPDATA%\Periscribe" >nul 2>&1
echo.
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'periscribe\.exe.{0,60}\brun\b' -or $_.CommandLine -match '-m\s+periscribe\b' }) { Write-Host '결과: 아직 실행 중인 수집기가 있습니다. 재부팅을 권장합니다.' } else { Write-Host '결과: 수집기 완전 종료 + 자동시작 해제 완료.' }"
echo.
echo  - 다운로드한 periscribe.exe 파일은 직접 삭제하세요.
echo  - 관리자: 웹 [머신 관리]에서 이 머신을 revoke 하면 토큰도 무효화됩니다.
echo.
pause
