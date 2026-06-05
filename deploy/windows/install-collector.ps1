<#
.SYNOPSIS
  소스(Python)에서 Collector를 설치한다 — config 작성 + 작업 스케줄러 등록(부팅 자동실행).
  단일 exe로 배포할 거면 packaging\build.ps1 로 periscribe.exe 를 만들어
  `periscribe.exe install --token <T> --url <U>` 를 쓰는 게 더 간단하다.
.EXAMPLE
  .\install-collector.ps1 -Url "https://xxx.supabase.co/functions/v1/ingest" -Token "pscb_..."
#>
param(
  [string]$Url = "",
  [string]$Token = "",
  [string]$Name = ""      # machine_id(비우면 hostname)
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\..\collector")

if (-not $Url)   { $Url   = Read-Host "ingest URL (.../functions/v1/ingest)" }
if (-not $Token) { $Token = Read-Host "디바이스 토큰(웹 머신 관리에서 발급)" }

Push-Location $collector
python -m periscribe install --token $Token --url $Url --name $Name
Pop-Location
