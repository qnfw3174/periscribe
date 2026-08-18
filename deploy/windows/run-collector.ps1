<#
.SYNOPSIS
  Collector를 현재 콘솔에서 직접 실행(자동시작 등록 없이 테스트/디버그). 종료 Ctrl+C.
  모든 설정(ingest_url, device_token, backfill, poll_interval 등)은 collector\config.json 이 담당한다
  (CLI 옵션 없음 — 값을 바꾸려면 config.json 을 편집).
.EXAMPLE
  .\run-collector.ps1
#>
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\..\collector")
Set-Location $collector

python -m periscribe run
