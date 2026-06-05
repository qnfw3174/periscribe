<#
.SYNOPSIS
  Collector 작업 스케줄러 등록 제거(소스 설치분). exe 설치분은 `periscribe.exe uninstall`.
#>
$ErrorActionPreference = "SilentlyContinue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Resolve-Path (Join-Path $here "..\..\collector")
Push-Location $collector
python -m periscribe uninstall
Pop-Location
