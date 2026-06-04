<#
.SYNOPSIS
  Collector를 현재 콘솔에서 직접 실행한다(작업 스케줄러 없이 테스트/디버그용).
  로그가 콘솔에 바로 보인다. 종료는 Ctrl+C.
.EXAMPLE
  .\run-collector.ps1
  .\run-collector.ps1 -Backfill 100
#>
param(
  [int]$Backfill = 0,
  [double]$PollInterval = 0.4
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collectorDir = Resolve-Path (Join-Path $here "..\..\collector")
Set-Location $collectorDir

if (-not (Test-Path (Join-Path $collectorDir "config.json"))) {
  throw "config.json 없음. 먼저 install-collector.ps1 실행 또는 config.example.json 복사."
}
python -m periscribe --backfill $Backfill --poll-interval $PollInterval
