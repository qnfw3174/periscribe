<#
.SYNOPSIS
  [deprecated] 소스(Python)에서 Collector를 설치한다.
  비대화형 `install` CLI 는 제거됐다 — 설치는 periscribe.exe 더블클릭(GUI) 또는
  `python -m periscribe setup`(콘솔 대화형)을 사용한다. 이 스크립트는 그 안내/위임만 한다.
.EXAMPLE
  .\install-collector.ps1
#>
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\..\collector")

Write-Host "[deprecated] 설치는 periscribe.exe 더블클릭(GUI)이 기본입니다."
Write-Host "소스 설치는 콘솔 대화형 setup 으로 진행합니다 (토큰은 웹 머신 관리에서 발급)…`n"

Push-Location $collector
python -m periscribe setup
Pop-Location
