# requires -Version 5.1
<#
  probe_wifi_guest.ps1
  Purpose:
    1. Reuse enter_wifi_settings.ps1 to reach Settings -> pages/wifi
    2. Parse the WLAN Toggle bounds from dumpLayout and click it
    3. Wait for WifiDevice.active state to become activated
    4. Poll layout/screenshot/WifiDevice for Guest visibility

  Notes:
    - This script does NOT input passwords or connect to an AP.
    - If Toggle bounds parsing fails, it falls back to the verified fixed
      coordinate (642,210) and logs that fallback explicitly.
#>

param(
  [string]$Target = "",
  [string]$OutDir = "",
  [int]$ActivateTimeoutSec = 24,
  [int]$PollDurationSec = 24,
  [int]$PollIntervalSec = 3,
  [ValidateSet("wait","light_scroll","toggle_reset")]
  [string]$Strategy = "wait"
)

$ErrorActionPreference = 'Stop'

$HdcTargetHelperPath = Join-Path $PSScriptRoot "tools\hdc_target.ps1"
if (-not (Test-Path -LiteralPath $HdcTargetHelperPath)) {
  throw "Missing HDC target helper: $HdcTargetHelperPath"
}
. $HdcTargetHelperPath

function Ensure-Dir([string]$Dir) {
  if ([string]::IsNullOrWhiteSpace($Dir)) { return }
  if (-not (Test-Path -LiteralPath $Dir)) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  }
}

function Get-TimestampString {
  return (Get-Date).ToString("yyyyMMdd_HHmmss")
}

function Get-HdcExe {
  $cmd = Get-Command hdc -ErrorAction SilentlyContinue
  if (-not $cmd) {
    throw "hdc not found in PATH."
  }
  return $cmd.Source
}

function Write-Step {
  param([string]$Message)
  $line = "[probe_wifi_guest] " + $Message
  Write-Host $line -ForegroundColor Cyan
  if ($script:RunLogPath) {
    Add-Content -LiteralPath $script:RunLogPath -Value $line -Encoding UTF8
  }
}

function Assert-True {
  param(
    [bool]$Condition,
    [string]$Message
  )

  if (-not $Condition) {
    throw $Message
  }
}

function Invoke-HdcShell {
  param(
    [string]$SN,
    [string]$Command
  )

  $hdc = Get-HdcExe
  $oldPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $lines = & $hdc -t $SN shell $Command 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) {
        $_.ToString()
      } else {
        [string]$_
      }
    }
  }
  finally {
    $ErrorActionPreference = $oldPreference
  }

  return @{
    ExitCode = $LASTEXITCODE
    Output = (($lines -join "`n").Trim())
  }
}

function Receive-RemoteFile {
  param(
    [string]$SN,
    [string]$RemotePath,
    [string]$LocalPath
  )

  Ensure-Dir (Split-Path -Parent $LocalPath)
  $hdc = Get-HdcExe
  $all = (& $hdc -t $SN file recv $RemotePath $LocalPath 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to recv $RemotePath -> $LocalPath`n$all"
  }
}

function Save-TextArtifact {
  param(
    [string]$Path,
    [string]$Content
  )
  $Content | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-RemoteSavedPath {
  param(
    [string]$Output,
    [string]$Extension
  )

  $pattern = [regex]::Escape("/data/local/tmp/") + "[^`r`n\s]+\." + [regex]::Escape($Extension)
  $m = [regex]::Match($Output, $pattern)
  if (-not $m.Success) {
    throw "Could not parse remote .$Extension path from output: $Output"
  }
  return $m.Value
}

function Capture-Layout {
  param(
    [string]$SN,
    [string]$StepName,
    [string]$ArtifactDir
  )

  $dump = Invoke-HdcShell -SN $SN -Command "uitest dumpLayout"
  if ($dump.ExitCode -ne 0) {
    throw "uitest dumpLayout failed: $($dump.Output)"
  }

  $remote = Get-RemoteSavedPath -Output $dump.Output -Extension "json"
  $local = Join-Path $ArtifactDir ($StepName + "_layout.json")
  Receive-RemoteFile -SN $SN -RemotePath $remote -LocalPath $local

  $raw = Get-Content -LiteralPath $local -Raw -Encoding UTF8
  return @{
    RemotePath = $remote
    LocalPath = $local
    Raw = $raw
    Json = ($raw | ConvertFrom-Json)
  }
}

function Capture-Screen {
  param(
    [string]$SN,
    [string]$StepName,
    [string]$ArtifactDir
  )

  $capture = Invoke-HdcShell -SN $SN -Command "uitest screenCap /data/local/tmp/$StepName.png"
  if ($capture.ExitCode -ne 0) {
    throw "uitest screenCap failed: $($capture.Output)"
  }

  $remote = Get-RemoteSavedPath -Output $capture.Output -Extension "png"
  $local = Join-Path $ArtifactDir ($StepName + "_screen.png")
  Receive-RemoteFile -SN $SN -RemotePath $remote -LocalPath $local

  return @{
    RemotePath = $remote
    LocalPath = $local
  }
}

function Capture-WifiDevice {
  param(
    [string]$SN,
    [string]$StepName,
    [string]$ArtifactDir
  )

  $result = Invoke-HdcShell -SN $SN -Command "hidumper -s WifiDevice"
  if ($result.ExitCode -ne 0) {
    throw "hidumper -s WifiDevice failed: $($result.Output)"
  }

  $local = Join-Path $ArtifactDir ($StepName + "_WifiDevice.txt")
  Save-TextArtifact -Path $local -Content $result.Output

  $state = ""
  $conn = ""
  $mState = [regex]::Match($result.Output, "WiFi active state:\s*(.+)")
  if ($mState.Success) { $state = $mState.Groups[1].Value.Trim() }
  $mConn = [regex]::Match($result.Output, "WiFi connection status:\s*(.+)")
  if ($mConn.Success) { $conn = $mConn.Groups[1].Value.Trim() }

  return @{
    Output = $result.Output
    ActiveState = $state
    ConnectionStatus = $conn
    LocalPath = $local
  }
}

function Get-NodeFacts {
  param(
    [object]$Node,
    [ref]$Collector,
    [string]$InheritedBundleName = "",
    [string]$InheritedAbilityName = "",
    [string]$InheritedPagePath = ""
  )

  if ($null -eq $Node) { return }

  $bundleName = $InheritedBundleName
  $abilityName = $InheritedAbilityName
  $pagePath = $InheritedPagePath
  $attrs = $Node.attributes
  if ($null -ne $attrs) {
    $bundleName = [string]$attrs.bundleName
    if ([string]::IsNullOrWhiteSpace($bundleName)) { $bundleName = $InheritedBundleName }
    $abilityName = [string]$attrs.abilityName
    if ([string]::IsNullOrWhiteSpace($abilityName)) { $abilityName = $InheritedAbilityName }
    $pagePath = [string]$attrs.pagePath
    if ([string]::IsNullOrWhiteSpace($pagePath)) { $pagePath = $InheritedPagePath }

    $Collector.Value += [pscustomobject]@{
      BundleName = $bundleName
      AbilityName = $abilityName
      PagePath = $pagePath
      Text = [string]$attrs.text
      Bounds = [string]$attrs.bounds
      Clickable = [string]$attrs.clickable
      Checkable = [string]$attrs.checkable
      Checked = [string]$attrs.checked
      Type = [string]$attrs.type
    }
  }

  if ($Node.children) {
    foreach ($child in $Node.children) {
      Get-NodeFacts -Node $child `
                    -Collector $Collector `
                    -InheritedBundleName $bundleName `
                    -InheritedAbilityName $abilityName `
                    -InheritedPagePath $pagePath
    }
  }
}

function Analyze-Layout {
  param([object]$LayoutJson)

  $nodes = @()
  Get-NodeFacts -Node $LayoutJson -Collector ([ref]$nodes)

  $texts = @($nodes | ForEach-Object { $_.Text } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
  $pagePaths = @($nodes | ForEach-Object { $_.PagePath } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)

  $toggle = $nodes | Where-Object {
    $_.BundleName -eq "com.ohos.settings" -and
    $_.Type -eq "Toggle" -and
    $_.Checkable -eq "true" -and
    $_.Clickable -eq "true"
  } | Select-Object -First 1

  return [pscustomobject]@{
    Nodes = $nodes
    Texts = $texts
    PagePaths = $pagePaths
    HasWifiPage = ($pagePaths -contains "pages/wifi")
    ToggleNode = $toggle
    HasGuest = ($texts -contains "Guest")
    HasGuestTest = ($texts -contains "Guest_test")
    GuestNode = ($nodes | Where-Object { $_.Text -eq "Guest" } | Select-Object -First 1)
  }
}

function Get-BoundsCenter {
  param([string]$Bounds)

  $m = [regex]::Match($Bounds, "^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
  if (-not $m.Success) {
    throw "Invalid bounds format: $Bounds"
  }

  $x1 = [int]$m.Groups[1].Value
  $y1 = [int]$m.Groups[2].Value
  $x2 = [int]$m.Groups[3].Value
  $y2 = [int]$m.Groups[4].Value

  return @{
    X = [int](($x1 + $x2) / 2)
    Y = [int](($y1 + $y2) / 2)
  }
}

function Click-ToggleNode {
  param(
    [string]$SN,
    [pscustomobject]$ToggleNode
  )

  if ($null -ne $ToggleNode -and -not [string]::IsNullOrWhiteSpace($ToggleNode.Bounds)) {
    $center = Get-BoundsCenter -Bounds $ToggleNode.Bounds
    Write-Step ("Clicking parsed Toggle center at ({0},{1}) from bounds {2}" -f $center.X, $center.Y, $ToggleNode.Bounds)
    $click = Invoke-HdcShell -SN $SN -Command ("uinput -T -c {0} {1} 80" -f $center.X, $center.Y)
    if ($click.ExitCode -ne 0) {
      throw "Parsed toggle click failed: $($click.Output)"
    }
    return @{
      UsedFallback = $false
      X = $center.X
      Y = $center.Y
      Bounds = $ToggleNode.Bounds
    }
  }

  Write-Step "Toggle bounds parse failed, falling back to fixed coordinate (642,210)."
  $fallback = Invoke-HdcShell -SN $SN -Command "uinput -T -c 642 210 80"
  if ($fallback.ExitCode -ne 0) {
    throw "Fallback toggle click failed: $($fallback.Output)"
  }
  return @{
    UsedFallback = $true
    X = 642
    Y = 210
    Bounds = ""
  }
}

function Wait-ForWifiState {
  param(
    [string]$SN,
    [string]$ArtifactDir,
    [string]$ExpectedState,
    [int]$TimeoutSec,
    [int]$PollSec
  )

  $attempt = 0
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $attempt++
    $wifi = Capture-WifiDevice -SN $SN -StepName (($ExpectedState + "_wait_") + $attempt.ToString("00")) -ArtifactDir $ArtifactDir
    Write-Step ("State poll {0}: expect={1} active={2} conn={3}" -f $attempt, $ExpectedState, $wifi.ActiveState, $wifi.ConnectionStatus)
    if ($wifi.ActiveState -eq $ExpectedState) {
      return $wifi
    }
    Start-Sleep -Seconds $PollSec
  }

  throw ("Timed out waiting for WiFi active state to become " + $ExpectedState + ".")
}

function Wait-ForWifiToggleNode {
  param(
    [string]$SN,
    [string]$ArtifactDir,
    [int]$RetryCount = 6,
    [int]$SleepMs = 1500
  )

  for ($i = 1; $i -le $RetryCount; $i++) {
    Write-Step ("Wifi page capture retry {0}/{1}" -f $i, $RetryCount)
    $tag = "01_baseline_try_" + $i.ToString("00")
    $layout = Capture-Layout -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $screen = Capture-Screen -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $wifi = Capture-WifiDevice -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json

    if ($facts.HasWifiPage -and $null -ne $facts.ToggleNode) {
      return @{
        Layout = $layout
        Screen = $screen
        Wifi = $wifi
        Facts = $facts
      }
    }

    Start-Sleep -Milliseconds $SleepMs
  }

  throw "Could not locate WLAN Toggle node on pages/wifi after retries."
}

function Invoke-LightScrollRefresh {
  param(
    [string]$SN,
    [int]$Iteration
  )

  # Alternate a small up/down swipe inside the AP list region to encourage list refresh
  # while avoiding large cumulative displacement.
  if (($Iteration % 2) -eq 1) {
    $cmd = "uinput -T -m 360 860 360 760 180"
    Write-Step ("Strategy light_scroll: upward nudge at iteration " + $Iteration)
  } else {
    $cmd = "uinput -T -m 360 760 360 860 180"
    Write-Step ("Strategy light_scroll: downward nudge at iteration " + $Iteration)
  }

  $ret = Invoke-HdcShell -SN $SN -Command $cmd
  if ($ret.ExitCode -ne 0) {
    throw "light_scroll refresh failed: $($ret.Output)"
  }
  Start-Sleep -Milliseconds 900
}

function Perform-ToggleReset {
  param(
    [string]$SN,
    [string]$ArtifactDir
  )

  Write-Step "Strategy toggle_reset: toggling WLAN off once."
  $before = Wait-ForWifiToggleNode -SN $SN -ArtifactDir $ArtifactDir -RetryCount 4 -SleepMs 1200
  if ($null -eq $before.Facts.ToggleNode) {
    throw "toggle_reset could not locate Toggle before reset."
  }
  Click-ToggleNode -SN $SN -ToggleNode $before.Facts.ToggleNode | Out-Null
  Start-Sleep -Seconds 2
  $inactive = Wait-ForWifiState -SN $SN -ArtifactDir $ArtifactDir -ExpectedState "inactive" -TimeoutSec 18 -PollSec 2

  Write-Step "Strategy toggle_reset: toggling WLAN on again."
  $afterOff = Wait-ForWifiToggleNode -SN $SN -ArtifactDir $ArtifactDir -RetryCount 6 -SleepMs 1500
  Click-ToggleNode -SN $SN -ToggleNode $afterOff.Facts.ToggleNode | Out-Null
  Start-Sleep -Seconds 2
}

$ResolvedTarget = Resolve-HdcTarget -Target $Target
$env:WK_DEVICE_TARGET = $ResolvedTarget
$env:HDC_TARGET = $ResolvedTarget

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot ("_tmp_wifi_probe_runs\" + (Get-TimestampString))
}
Ensure-Dir $OutDir

$script:RunLogPath = Join-Path $OutDir "run.log"
Save-TextArtifact -Path $script:RunLogPath -Content ""

try {
  Write-Step ("Target = " + $ResolvedTarget)
  Write-Step ("Artifacts = " + $OutDir)
  Write-Step ("Strategy = " + $Strategy)

  $enterDir = Join-Path $OutDir "00_enter_wifi_page"
  Write-Step "Reaching Settings -> pages/wifi via enter_wifi_settings.ps1"
  & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "enter_wifi_settings.ps1") -Target $ResolvedTarget -OutDir $enterDir
  if ($LASTEXITCODE -ne 0) {
    Write-Step "enter_wifi_settings.ps1 reported failure; continuing with direct baseline verification."
  }

  $baseline = Wait-ForWifiToggleNode -SN $ResolvedTarget -ArtifactDir $OutDir
  $baselineLayout = $baseline.Layout
  $baselineScreen = $baseline.Screen
  $baselineWifi = $baseline.Wifi
  $baselineFacts = $baseline.Facts

  $toggleClick = $null
  if ($baselineFacts.ToggleNode.Checked -ne "true") {
    $toggleClick = Click-ToggleNode -SN $ResolvedTarget -ToggleNode $baselineFacts.ToggleNode
    Start-Sleep -Seconds 2
  } else {
    Write-Step "WLAN Toggle is already checked=true, skipping click."
  }

  if ($Strategy -eq "toggle_reset") {
    Perform-ToggleReset -SN $ResolvedTarget -ArtifactDir $OutDir
  }

  $activatedWifi = Wait-ForWifiState -SN $ResolvedTarget -ArtifactDir $OutDir -ExpectedState "activated" -TimeoutSec $ActivateTimeoutSec -PollSec 2

  $afterLayout = Capture-Layout -SN $ResolvedTarget -StepName "02_after_activation" -ArtifactDir $OutDir
  $afterScreen = Capture-Screen -SN $ResolvedTarget -StepName "02_after_activation" -ArtifactDir $OutDir
  $afterFacts = Analyze-Layout -LayoutJson $afterLayout.Json

  $summaryRows = @()
  $guestDumpHits = 0
  $guestDumpHitIters = @()
  $guestTestHits = 0
  $toggleTrueHits = 0

  $iterations = [Math]::Max(1, [int][Math]::Floor($PollDurationSec / $PollIntervalSec))
  $pollDir = Join-Path $OutDir "03_guest_poll"
  Ensure-Dir $pollDir

  for ($i = 1; $i -le $iterations; $i++) {
    Write-Step ("Guest poll iteration {0}/{1}" -f $i, $iterations)
    if ($Strategy -eq "light_scroll") {
      Invoke-LightScrollRefresh -SN $ResolvedTarget -Iteration $i
    }
    $tag = "poll_" + $i.ToString("00")
    $layout = Capture-Layout -SN $ResolvedTarget -StepName $tag -ArtifactDir $pollDir
    $screen = Capture-Screen -SN $ResolvedTarget -StepName $tag -ArtifactDir $pollDir
    $wifi = Capture-WifiDevice -SN $ResolvedTarget -StepName $tag -ArtifactDir $pollDir
    $facts = Analyze-Layout -LayoutJson $layout.Json

    if ($facts.HasGuest) {
      $guestDumpHits++
      $guestDumpHitIters += $i
    }
    if ($facts.HasGuestTest) {
      $guestTestHits++
    }
    if ($null -ne $facts.ToggleNode -and $facts.ToggleNode.Checked -eq "true") {
      $toggleTrueHits++
    }

    $summaryRows += [pscustomobject]@{
      Iteration = $i
      HasWifiPage = $facts.HasWifiPage
      ToggleChecked = if ($null -ne $facts.ToggleNode) { $facts.ToggleNode.Checked } else { "" }
      WifiActiveState = $wifi.ActiveState
      WifiConnectionStatus = $wifi.ConnectionStatus
      HasGuestInDump = $facts.HasGuest
      HasGuestTestInDump = $facts.HasGuestTest
      GuestBounds = if ($null -ne $facts.GuestNode) { $facts.GuestNode.Bounds } else { "" }
      LayoutPath = $layout.LocalPath
      ScreenPath = $screen.LocalPath
      WifiDevicePath = $wifi.LocalPath
    }

    Start-Sleep -Seconds $PollIntervalSec
  }

  $summaryPath = Join-Path $OutDir "guest_poll_summary.json"
  ($summaryRows | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $summaryPath -Encoding UTF8

  Write-Step ("Guest dump hits = {0}" -f $guestDumpHits)
  Write-Step ("Guest_test dump hits = {0}" -f $guestTestHits)
  Write-Step ("Toggle checked=true iterations = {0}" -f $toggleTrueHits)
  Write-Step ("Summary = {0}" -f $summaryPath)

  Write-Host ("SUCCESS wifi_active={0} guest_dump_hits={1} guest_dump_iters={2}" -f $activatedWifi.ActiveState, $guestDumpHits, ($guestDumpHitIters -join ",")) -ForegroundColor Green
}
catch {
  $msg = $_.Exception.Message
  Write-Host ("FAILED: " + $msg) -ForegroundColor Red
  Write-Host ("Artifacts: " + $OutDir) -ForegroundColor Yellow
  if ($script:RunLogPath) {
    Add-Content -LiteralPath $script:RunLogPath -Value ("FAILED: " + $msg) -Encoding UTF8
    Add-Content -LiteralPath $script:RunLogPath -Value ("Artifacts: " + $OutDir) -Encoding UTF8
  }
  throw
}
