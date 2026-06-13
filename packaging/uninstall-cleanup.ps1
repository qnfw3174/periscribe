# Periscribe 제거 정리 — Inno Setup 제거기가 파일 삭제 직전에 호출(usUninstall).
# revoke 신호(서버에 제거 통지 → 자동 revoke·제거됨 표시) + 자동시작 해제 + 실행 중 프로세스 종료.
# (데이터 폴더 %LOCALAPPDATA%\Periscribe 삭제는 .iss 의 [UninstallDelete] 가 담당.)

# 1) 서버에 제거 신호(오프라인이면 조용히 skip → 관리자가 웹에서 수동 삭제)
try {
  $p = Join-Path $env:LOCALAPPDATA 'Periscribe\config.json'
  if (Test-Path $p) {
    $c = Get-Content $p -Raw | ConvertFrom-Json
    if ($c.device_token -and $c.ingest_url) {
      try {
        Invoke-RestMethod -Method Post -Uri $c.ingest_url -ContentType 'application/json' -TimeoutSec 10 `
          -Body (ConvertTo-Json @{ device_token = $c.device_token; uninstall = $true }) | Out-Null
      } catch {}
    }
  }
} catch {}

# 2) 자동시작(HKCU Run) 해제
foreach ($n in 'PeriscribeCollector', 'PeriscribeGuardian') {
  try { Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $n -ErrorAction SilentlyContinue } catch {}
}
try { schtasks /Delete /TN PeriscribeCollector /F 2>$null | Out-Null } catch {}

# 3) 실행 중 프로세스 종료(설치 폴더 파일 삭제 가능하도록)
foreach ($n in 'periscribe', 'periscribe-proxy', 'periscribe-agent') {
  try { Get-Process $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
}
