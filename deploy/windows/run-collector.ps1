<#
.SYNOPSIS
  Collector를 현재 콘솔에서 직접 실행(작업 스케줄러 없이 테스트/디버그). 종료 Ctrl+C.
.EXAMPLE
  .\run-collector.ps1 -Url "https://xxx.supabase.co/functions/v1/ingest" -Token "pscb_..." -Backfill 100
#>
param(
  [string]$Url = "",
  [string]$Token = "",
  [int]$Backfill = 0,
  [double]$PollInterval = 0.4
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\..\collector")
Set-Location $collector

$argsList = @("-m", "periscribe", "run", "--backfill", $Backfill, "--poll-interval", $PollInterval)
if ($Url)   { $argsList += @("--ingest-url", $Url) }
if ($Token) { $argsList += @("--device-token", $Token) }
python @argsList
