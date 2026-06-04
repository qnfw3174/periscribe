<#
.SYNOPSIS
  Periscribe Collector를 이 Windows PC에 설치하고 작업 스케줄러에 등록한다.
  - 로그온 시 자동 시작 + 실패 시 자동 재시작
  - pythonw.exe로 무콘솔(백그라운드) 실행
  - config.json 생성(service_role 키 입력), 레닥션 ON, 파일 로그

.NOTES
  관리자 권한 불필요(현재 사용자 작업으로 등록). Collector는 사용자 세션에서 돌아야
  %USERPROFILE%\.claude\projects 의 transcript를 읽을 수 있으므로 "로그온 시" 트리거를 쓴다.

.EXAMPLE
  .\install-collector.ps1 -SupabaseUrl "https://xxx.supabase.co" -ServiceRoleKey "ey..."
  # 인자를 생략하면 프롬프트로 입력받는다.
#>
param(
  [string]$SupabaseUrl = "",
  [string]$ServiceRoleKey = "",
  [string]$MachineId = "",      # 비우면 hostname 사용
  [string]$TaskName = "PeriscribeCollector",
  [switch]$Force                # config.json 덮어쓰기
)

$ErrorActionPreference = "Stop"

# ---- 경로 ----
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collectorDir = Resolve-Path (Join-Path $here "..\..\collector")
$configPath = Join-Path $collectorDir "config.json"
$logPath = Join-Path $collectorDir "logs\collector.log"

Write-Host "Collector 경로: $collectorDir"

# ---- pythonw 찾기 ----
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) {
  $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
  if ($py) { $pyw = Join-Path (Split-Path $py) "pythonw.exe" }
}
if (-not $pyw -or -not (Test-Path $pyw)) {
  throw "pythonw.exe를 찾을 수 없습니다. Python 3.8+ 설치 후 PATH에 추가하세요."
}
Write-Host "pythonw: $pyw"

# ---- config.json 생성 ----
if ((Test-Path $configPath) -and -not $Force) {
  Write-Host "기존 config.json 유지(-Force로 덮어쓰기). 키 입력 생략." -ForegroundColor Yellow
} else {
  if (-not $SupabaseUrl) { $SupabaseUrl = Read-Host "Supabase URL (예: https://xxx.supabase.co)" }
  if (-not $ServiceRoleKey) {
    $sec = Read-Host "service_role 키 (로컬에만 저장됨)" -AsSecureString
    $ServiceRoleKey = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
      [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  }
  $cfg = [ordered]@{
    watch_dir = ""              # 비우면 ~/.claude/projects 자동
    machine_id = $MachineId     # 비우면 hostname
    poll_interval = 0.4
    supabase_url = $SupabaseUrl
    supabase_key = $ServiceRoleKey
    table = "events"
    batch_size = 500
    checkpoint_path = "checkpoints/offsets.json"
    backfill = 0
    store_raw = $false
    store_thinking = $false
    redact = $true              # 멀티 PC/클라우드 저장 → 레닥션 ON
    heartbeat_interval = 30
    log_file = "logs/collector.log"
    log_max_bytes = 5000000
    log_backups = 3
  }
  New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null
  # BOM 없이 UTF-8로 기록(Windows PowerShell의 Set-Content -Encoding utf8은 BOM을 붙여 Python json이 거부함).
  $json = $cfg | ConvertTo-Json
  [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding $false))
  Write-Host "config.json 생성됨: $configPath" -ForegroundColor Green
}

# ---- 작업 스케줄러 등록 ----
# 작업 XML은 도메인 한정 계정명(예: PC\user)을 요구한다. 전체 계정명을 구한다.
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $pyw -Argument "-m periscribe -c `"$configPath`"" -WorkingDirectory $collectorDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)   # 무제한

# 현재 사용자의 인터랙티브 작업으로 등록(관리자/비밀번호 불필요). Collector는 사용자 세션에서 실행.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -User $me -RunLevel Limited -Force | Out-Null
Write-Host "작업 등록됨: $TaskName (로그온 시 자동 시작 + 실패 시 1분마다 재시작)" -ForegroundColor Green

# ---- 즉시 시작 + 확인 ----
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host "상태: LastTaskResult=$($info.LastTaskResult) (0이면 정상 실행 중)"
Write-Host ""
Write-Host "확인:"
Write-Host "  - 로그: $logPath"
Write-Host "  - Supabase machines 테이블에 이 PC가 잠시 후 표시됩니다(하트비트)."
Write-Host "  - 중지/제거: .\uninstall-collector.ps1"
