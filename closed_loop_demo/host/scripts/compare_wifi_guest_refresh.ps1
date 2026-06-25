# requires -Version 5.1
<#
  compare_wifi_guest_refresh.ps1
  Purpose:
    Run Guest discovery experiments for multiple refresh strategies and
    summarize hit-rate comparisons.
#>

param(
  [string]$Target = "",
  [string]$OutDir = "",
  [int]$PollDurationSec = 24,
  [int]$PollIntervalSec = 3
)

$ErrorActionPreference = 'Stop'

function Ensure-Dir([string]$Dir) {
  if ([string]::IsNullOrWhiteSpace($Dir)) { return }
  if (-not (Test-Path -LiteralPath $Dir)) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  }
}

function Get-TimestampString {
  return (Get-Date).ToString("yyyyMMdd_HHmmss")
}

function Get-StrategySummary {
  param(
    [string]$SummaryPath,
    [string]$Strategy
  )

  $rows = Get-Content -LiteralPath $SummaryPath -Raw | ConvertFrom-Json
  $guestRows = @($rows | Where-Object { $_.HasGuestInDump -eq $true })
  $guestBounds = @($guestRows | ForEach-Object { $_.GuestBounds } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
  return [pscustomobject]@{
    Strategy = $Strategy
    TotalPolls = @($rows).Count
    GuestDumpHits = $guestRows.Count
    GuestDumpHitIterations = @($guestRows | ForEach-Object { $_.Iteration })
    GuestBounds = $guestBounds
    DistinctGuestBounds = $guestBounds.Count
    ToggleCheckedIterations = @($rows | Where-Object { $_.ToggleChecked -eq "true" }).Count
    WifiActivatedIterations = @($rows | Where-Object { $_.WifiActiveState -eq "activated" }).Count
    SummaryPath = $SummaryPath
  }
}

$strategies = @("wait","light_scroll","toggle_reset")

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot ("_tmp_wifi_refresh_compare\" + (Get-TimestampString))
}
Ensure-Dir $OutDir

$compare = @()
foreach ($strategy in $strategies) {
  $strategyDir = Join-Path $OutDir $strategy
  Ensure-Dir $strategyDir
  Write-Host ("[compare_wifi_guest_refresh] Running strategy=" + $strategy) -ForegroundColor Cyan

  & powershell -ExecutionPolicy Bypass `
      -File (Join-Path $PSScriptRoot "probe_wifi_guest.ps1") `
      -Target $Target `
      -OutDir $strategyDir `
      -PollDurationSec $PollDurationSec `
      -PollIntervalSec $PollIntervalSec `
      -Strategy $strategy

  if ($LASTEXITCODE -ne 0) {
    throw ("Strategy failed: " + $strategy)
  }

  $summaryPath = Join-Path $strategyDir "guest_poll_summary.json"
  if (-not (Test-Path -LiteralPath $summaryPath)) {
    throw ("Missing strategy summary: " + $summaryPath)
  }

  $compare += Get-StrategySummary -SummaryPath $summaryPath -Strategy $strategy
}

$comparePath = Join-Path $OutDir "compare_summary.json"
($compare | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $comparePath -Encoding UTF8

Write-Host ("SUCCESS compare_summary=" + $comparePath) -ForegroundColor Green
