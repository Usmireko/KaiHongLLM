# requires -Version 5.1
<#
  validate_guest_click.ps1
  Purpose:
    Validate whether tapping the Guest hotspot node causes a page transition.
    This script does NOT input passwords or connect to Wi-Fi.
#>

param(
  [string]$Target = "",
  [string]$OutDir = "",
  [int]$GuestWaitTimeoutSec = 60,
  [int]$GuestPollIntervalSec = 3
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
  $line = "[validate_guest_click] " + $Message
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

function Capture-AaDump {
  param(
    [string]$SN,
    [string]$StepName,
    [string]$ArtifactDir
  )

  $result = Invoke-HdcShell -SN $SN -Command "aa dump -a"
  if ($result.ExitCode -ne 0) {
    throw "aa dump -a failed: $($result.Output)"
  }

  $local = Join-Path $ArtifactDir ($StepName + "_aa_dump.txt")
  Save-TextArtifact -Path $local -Content $result.Output
  return @{
    Output = $result.Output
    LocalPath = $local
  }
}

function Get-NodeFacts {
  param(
    [object]$Node,
    [ref]$Collector,
    [string]$InheritedBundleName = "",
    [string]$InheritedAbilityName = "",
    [string]$InheritedPagePath = "",
    [object[]]$AncestorStack = @()
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

    $fact = [pscustomobject]@{
      BundleName = $bundleName
      AbilityName = $abilityName
      PagePath = $pagePath
      Text = [string]$attrs.text
      Bounds = [string]$attrs.bounds
      Clickable = [string]$attrs.clickable
      Checkable = [string]$attrs.checkable
      Checked = [string]$attrs.checked
      Type = [string]$attrs.type
      Ancestors = $AncestorStack
    }
    $Collector.Value += $fact
    $nextStack = @($AncestorStack + $fact)
  } else {
    $nextStack = $AncestorStack
  }

  if ($Node.children) {
    foreach ($child in $Node.children) {
      Get-NodeFacts -Node $child `
        -Collector $Collector `
        -InheritedBundleName $bundleName `
        -InheritedAbilityName $abilityName `
        -InheritedPagePath $pagePath `
        -AncestorStack $nextStack
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
    $_.PagePath -eq "pages/wifi" -and
    $_.Type -eq "Toggle" -and
    $_.Checkable -eq "true" -and
    $_.Clickable -eq "true"
  } | Select-Object -First 1

  $guestTextNode = $nodes | Where-Object {
    $_.BundleName -eq "com.ohos.settings" -and
    $_.PagePath -eq "pages/wifi" -and
    $_.Text -eq "Guest"
  } | Select-Object -First 1

  $guestClickableAncestor = $null
  if ($null -ne $guestTextNode) {
    $guestClickableAncestor = @($guestTextNode.Ancestors | Where-Object {
      $_.BundleName -eq "com.ohos.settings" -and
      $_.PagePath -eq "pages/wifi" -and
      $_.Clickable -eq "true" -and
      -not [string]::IsNullOrWhiteSpace($_.Bounds)
    } | Select-Object -Last 1)
    if ($guestClickableAncestor.Count -gt 0) {
      $guestClickableAncestor = $guestClickableAncestor[0]
    } else {
      $guestClickableAncestor = $null
    }
  }

  $transitionHint = ""
  if ($pagePaths -contains "pages/wifiPsd") {
    $transitionHint = "pages/wifiPsd"
  } elseif ($texts -contains "密码") {
    $transitionHint = "password_text"
  } elseif ($texts -contains "连接") {
    $transitionHint = "connect_text"
  } elseif ($texts -contains "取消") {
    $transitionHint = "cancel_text"
  }

  $pageTitle = ""
  if ($pagePaths -contains "pages/wifiPsd") {
    $pageTitle = @(
      $nodes |
      Where-Object {
        $_.PagePath -eq "pages/wifiPsd" -and
        -not [string]::IsNullOrWhiteSpace($_.Text) -and
        $_.Text -notin @("密码","取消","连接")
      } |
      Select-Object -ExpandProperty Text -First 1
    ) -join ""
  }

  return [pscustomobject]@{
    Nodes = $nodes
    Texts = $texts
    PagePaths = $pagePaths
    HasWifiPage = ($pagePaths -contains "pages/wifi")
    ToggleNode = $toggle
    GuestTextNode = $guestTextNode
    GuestClickableAncestor = $guestClickableAncestor
    TransitionHint = $transitionHint
    PageTitle = $pageTitle
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

function Wait-ForWifiPage {
  param(
    [string]$SN,
    [string]$ArtifactDir,
    [int]$RetryCount = 8,
    [int]$SleepMs = 1500
  )

  for ($i = 1; $i -le $RetryCount; $i++) {
    Write-Step ("Wi-Fi page capture retry {0}/{1}" -f $i, $RetryCount)
    $tag = "wifi_page_try_" + $i.ToString("00")
    $layout = Capture-Layout -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $screen = Capture-Screen -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $wifi = Capture-WifiDevice -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json
    if ($facts.HasWifiPage) {
      return @{
        Layout = $layout
        Screen = $screen
        Wifi = $wifi
        Facts = $facts
      }
    }
    Start-Sleep -Milliseconds $SleepMs
  }

  throw "Could not confirm pages/wifi after retries."
}

function Click-NodeByBounds {
  param(
    [string]$SN,
    [string]$Bounds,
    [string]$Label
  )

  $center = Get-BoundsCenter -Bounds $Bounds
  Write-Step ("Clicking {0} at ({1},{2}) from bounds {3}" -f $Label, $center.X, $center.Y, $Bounds)
  $ret = Invoke-HdcShell -SN $SN -Command ("uinput -T -c {0} {1} 80" -f $center.X, $center.Y)
  if ($ret.ExitCode -ne 0) {
    throw ("Click failed for " + $Label + ": " + $ret.Output)
  }
  return $center
}

function Capture-PostClickSeries {
  param(
    [string]$SN,
    [string]$ArtifactDir,
    [string]$Prefix,
    [int]$Rounds = 3
  )

  $rows = @()
  for ($i = 1; $i -le $Rounds; $i++) {
    Start-Sleep -Milliseconds 1200
    $tag = "{0}_post_{1}" -f $Prefix, $i.ToString("00")
    $layout = Capture-Layout -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $screen = Capture-Screen -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $aa = Capture-AaDump -SN $SN -StepName $tag -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json

    $rows += [pscustomobject]@{
      Round = $i
      LayoutPath = $layout.LocalPath
      ScreenPath = $screen.LocalPath
      AaDumpPath = $aa.LocalPath
      PagePaths = @($facts.PagePaths)
      HasWifiPage = $facts.HasWifiPage
      TransitionHint = $facts.TransitionHint
      PageTitle = $facts.PageTitle
      HasGuest = ($null -ne $facts.GuestTextNode)
    }
  }
  return $rows
}

function Get-ClickOutcome {
  param(
    [object[]]$PostRows
  )

  foreach ($row in $PostRows) {
    if ($row.PagePaths -contains "pages/wifiPsd") {
      return "pages/wifiPsd"
    }
    if (-not $row.HasWifiPage -and $row.TransitionHint) {
      return $row.TransitionHint
    }
  }

  return "no_jump"
}

$ResolvedTarget = Resolve-HdcTarget -Target $Target
$env:WK_DEVICE_TARGET = $ResolvedTarget
$env:HDC_TARGET = $ResolvedTarget

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot ("_tmp_guest_click_runs\" + (Get-TimestampString))
}
Ensure-Dir $OutDir

$script:RunLogPath = Join-Path $OutDir "run.log"
Save-TextArtifact -Path $script:RunLogPath -Content ""

try {
  Write-Step ("Target = " + $ResolvedTarget)
  Write-Step ("Artifacts = " + $OutDir)

  $enterDir = Join-Path $OutDir "00_enter_wifi_page"
  Write-Step "Reaching Settings -> pages/wifi via enter_wifi_settings.ps1"
  & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "enter_wifi_settings.ps1") -Target $ResolvedTarget -OutDir $enterDir
  if ($LASTEXITCODE -ne 0) {
    Write-Step "enter_wifi_settings.ps1 reported failure; continuing with direct Wi-Fi page verification."
  }

  $wifiPage = Wait-ForWifiPage -SN $ResolvedTarget -ArtifactDir $OutDir
  if ($null -ne $wifiPage.Facts.ToggleNode -and $wifiPage.Facts.ToggleNode.Checked -ne "true") {
    Click-NodeByBounds -SN $ResolvedTarget -Bounds $wifiPage.Facts.ToggleNode.Bounds -Label "WLAN Toggle" | Out-Null
    Start-Sleep -Seconds 2
    $activated = Wait-ForWifiState -SN $ResolvedTarget -ArtifactDir $OutDir -ExpectedState "activated" -TimeoutSec 24 -PollSec 2
    $wifiPage = Wait-ForWifiPage -SN $ResolvedTarget -ArtifactDir $OutDir
  }

  $hitDir = Join-Path $OutDir "01_guest_hit"
  Ensure-Dir $hitDir
  $deadline = (Get-Date).AddSeconds($GuestWaitTimeoutSec)
  $hit = $null
  $iter = 0
  while ((Get-Date) -lt $deadline) {
    $iter++
    Write-Step ("Waiting for Guest hit iteration " + $iter)
    $tag = "guest_hit_" + $iter.ToString("00")
    $layout = Capture-Layout -SN $ResolvedTarget -StepName $tag -ArtifactDir $hitDir
    $screen = Capture-Screen -SN $ResolvedTarget -StepName $tag -ArtifactDir $hitDir
    $wifi = Capture-WifiDevice -SN $ResolvedTarget -StepName $tag -ArtifactDir $hitDir
    $facts = Analyze-Layout -LayoutJson $layout.Json
    if ($null -ne $facts.GuestTextNode) {
      $hit = @{
        Iteration = $iter
        Layout = $layout
        Screen = $screen
        Wifi = $wifi
        Facts = $facts
      }
      break
    }
    Start-Sleep -Seconds $GuestPollIntervalSec
  }

  Assert-True ($null -ne $hit) "Guest was not found within timeout."

  $summary = [ordered]@{
    HitIteration = $hit.Iteration
    HitLayoutPath = $hit.Layout.LocalPath
    HitScreenPath = $hit.Screen.LocalPath
    HitWifiDevicePath = $hit.Wifi.LocalPath
    GuestTextBounds = $hit.Facts.GuestTextNode.Bounds
    GuestParentBounds = if ($null -ne $hit.Facts.GuestClickableAncestor) { $hit.Facts.GuestClickableAncestor.Bounds } else { "" }
    TextNodeClick = $null
    ParentClick = $null
  }

  $textDir = Join-Path $OutDir "02_text_click"
  Ensure-Dir $textDir
  Click-NodeByBounds -SN $ResolvedTarget -Bounds $hit.Facts.GuestTextNode.Bounds -Label "Guest text node" | Out-Null
  $textPost = Capture-PostClickSeries -SN $ResolvedTarget -ArtifactDir $textDir -Prefix "text_click" -Rounds 3
  $textOutcome = Get-ClickOutcome -PostRows $textPost
  $summary.TextNodeClick = [ordered]@{
    Bounds = $hit.Facts.GuestTextNode.Bounds
    Outcome = $textOutcome
    PageTitle = @($textPost | Where-Object { -not [string]::IsNullOrWhiteSpace($_.PageTitle) } | Select-Object -ExpandProperty PageTitle -First 1) -join ""
    PostArtifacts = $textPost
  }

  $needParentClick = ($textOutcome -eq "no_jump") -or
                     (($textOutcome -eq "pages/wifiPsd") -and ($summary.TextNodeClick.PageTitle -ne "Guest"))

  if ($needParentClick -and $null -ne $hit.Facts.GuestClickableAncestor) {
    $parentDir = Join-Path $OutDir "03_parent_click"
    Ensure-Dir $parentDir

    Write-Step "Re-entering Wi-Fi page before parent-row click validation."
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "enter_wifi_settings.ps1") -Target $ResolvedTarget -OutDir (Join-Path $OutDir "03_reenter")
    if ($LASTEXITCODE -ne 0) {
      Write-Step "Re-enter helper reported failure; continuing with direct Wi-Fi page verification."
    }
    $wifiPage2 = Wait-ForWifiPage -SN $ResolvedTarget -ArtifactDir $parentDir
    $hit2 = $null
    $deadline2 = (Get-Date).AddSeconds($GuestWaitTimeoutSec)
    while ((Get-Date) -lt $deadline2) {
      $iter++
      $tag = "guest_parent_hit_" + $iter.ToString("00")
      $layout = Capture-Layout -SN $ResolvedTarget -StepName $tag -ArtifactDir $parentDir
      $screen = Capture-Screen -SN $ResolvedTarget -StepName $tag -ArtifactDir $parentDir
      $wifi = Capture-WifiDevice -SN $ResolvedTarget -StepName $tag -ArtifactDir $parentDir
      $facts = Analyze-Layout -LayoutJson $layout.Json
      if ($null -ne $facts.GuestTextNode -and $null -ne $facts.GuestClickableAncestor) {
        $hit2 = @{
          Layout = $layout
          Screen = $screen
          Wifi = $wifi
          Facts = $facts
        }
        break
      }
      Start-Sleep -Seconds $GuestPollIntervalSec
    }

    Assert-True ($null -ne $hit2) "Could not reacquire Guest with clickable ancestor for parent-row click."
    Click-NodeByBounds -SN $ResolvedTarget -Bounds $hit2.Facts.GuestClickableAncestor.Bounds -Label "Guest clickable ancestor" | Out-Null
    $parentPost = Capture-PostClickSeries -SN $ResolvedTarget -ArtifactDir $parentDir -Prefix "parent_click" -Rounds 3
    $parentOutcome = Get-ClickOutcome -PostRows $parentPost
    $summary.ParentClick = [ordered]@{
      Bounds = $hit2.Facts.GuestClickableAncestor.Bounds
      Outcome = $parentOutcome
      PageTitle = @($parentPost | Where-Object { -not [string]::IsNullOrWhiteSpace($_.PageTitle) } | Select-Object -ExpandProperty PageTitle -First 1) -join ""
      HitLayoutPath = $hit2.Layout.LocalPath
      HitScreenPath = $hit2.Screen.LocalPath
      PostArtifacts = $parentPost
    }
  }

  $summaryPath = Join-Path $OutDir "guest_click_summary.json"
  ($summary | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $summaryPath -Encoding UTF8
  Write-Step ("Summary = " + $summaryPath)
  Write-Host ("SUCCESS guest_summary=" + $summaryPath) -ForegroundColor Green
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
