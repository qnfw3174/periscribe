@echo off
chcp 65001 >nul
setlocal
echo ============================================
echo   Periscribe Collector 제거
echo ============================================
echo.

echo [1/3] 자동 시작 해제...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v PeriscribeCollector /f >nul 2>&1
schtasks /Delete /TN PeriscribeCollector /F >nul 2>&1

echo [2/3] 실행 중인 수집기 종료...
taskkill /IM periscribe.exe /F >nul 2>&1

echo [3/3] 데이터(토큰/체크포인트/로그) 삭제...
rmdir /S /Q "%LOCALAPPDATA%\Periscribe" >nul 2>&1

echo.
echo ✓ 제거 완료.
echo   - 다운로드한 periscribe.exe 파일은 직접 삭제하세요.
echo   - 관리자: 웹 [머신 관리]에서 이 머신을 revoke 하는 것도 권장합니다.
echo.
pause
endlocal
