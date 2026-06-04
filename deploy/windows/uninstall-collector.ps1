<#
.SYNOPSIS
  Periscribe Collector 작업 스케줄러 등록을 제거하고 실행 중 프로세스를 정지한다.
  (config.json / checkpoints / logs 는 남겨둔다. -Purge로 함께 삭제.)
.EXAMPLE
  .\uninstall-collector.ps1
  .\uninstall-collector.ps1 -Purge
#>
param(
  [string]$TaskName = "PeriscribeCollector",
  [switch]$Purge
)

$ErrorActionPreference = "SilentlyContinue"

# 작업 제거
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
  Stop-ScheduledTask -TaskName $TaskName
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "작업 제거됨: $TaskName" -ForegroundColor Green
} else {
  Write-Host "등록된 작업 없음: $TaskName" -ForegroundColor Yellow
}

# 실행 중 collector 프로세스 정지
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*periscribe*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "프로세스 정지 PID $($_.ProcessId)" }

if ($Purge) {
  $here = Split-Path -Parent $MyInvocation.MyCommand.Path
  $collectorDir = Resolve-Path (Join-Path $here "..\..\collector")
  foreach ($p in @("config.json", "checkpoints", "logs")) {
    $target = Join-Path $collectorDir $p
    if (Test-Path $target) { Remove-Item -Recurse -Force $target; Write-Host "삭제: $target" }
  }
}
Write-Host "완료."
