# requires -Version 5.1
<#
  enter_wifi_settings.ps1
  Purpose:
    Drive a headless KaiHongOS/OpenHarmony board into Settings -> WLAN page
    using only hdc + aa + uitest + uinput.

  Notes:
    - This script does NOT perform Wi-Fi pairing/configuration.
    - WLAN entry click currently uses a fixed coordinate validated on the
      current board layout. It may need adjustment if DPI/layout changes.
#>

param(
  [string]$Target = "",
  [string]$OutDir = ""
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

function Invoke-HdcCapture {
  param(
    [string]$SN,
    [string[]]$Args
  )

  $hdc = Get-HdcExe
  $argList = @("-t", $SN) + $Args
  $oldPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $lines = & $hdc @argList 2>&1 | ForEach-Object {
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

  $all = (($lines -join "`n").Trim())
  return @{
    ExitCode = $LASTEXITCODE
    Output = $all.Trim()
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

function Assert-True {
  param(
    [bool]$Condition,
    [string]$Message
  )

  if (-not $Condition) {
    throw $Message
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

function Get-NodeFacts {
  param(
    [object]$Node,
    [ref]$Collector
  )

  if ($null -eq $Node) { return }

  $attrs = $Node.attributes
  if ($null -ne $attrs) {
    $Collector.Value += [pscustomobject]@{
      BundleName = [string]$attrs.bundleName
      AbilityName = [string]$attrs.abilityName
      PagePath = [string]$attrs.pagePath
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
      Get-NodeFacts -Node $child -Collector $Collector
    }
  }
}

function Analyze-Layout {
  param(
    [object]$LayoutJson
  )

  $nodes = @()
  Get-NodeFacts -Node $LayoutJson -Collector ([ref]$nodes)

  $texts = @($nodes | ForEach-Object { $_.Text } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
  $pagePaths = @($nodes | ForEach-Object { $_.PagePath } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
  $settingNodes = @($nodes | Where-Object { $_.BundleName -eq "com.ohos.settings" })

  $wlanNode = $nodes | Where-Object { $_.Text -eq "WLAN" -and $_.BundleName -eq "com.ohos.settings" } | Select-Object -First 1
  $hasLockScreenText = $texts -contains "上滑解锁"
  $hasLockScreenText = $hasLockScreenText -or ($texts -contains "涓婃粦瑙ｉ攣")

  return [pscustomobject]@{
    Nodes = $nodes
    Texts = $texts
    PagePaths = $pagePaths
    HasSettings = ($settingNodes.Count -gt 0)
    HasSettingList = ($pagePaths -contains "pages/settingList")
    HasWifiPage = ($pagePaths -contains "pages/wifi")
    HasLockScreenText = $hasLockScreenText
    HasSystemUi = (($nodes | Where-Object { $_.BundleName -eq "com.ohos.systemui" }).Count -gt 0)
    WlanNode = $wlanNode
  }
}

function Write-Step {
  param([string]$Message)
  $line = "[enter_wifi_settings] " + $Message
  Write-Host $line -ForegroundColor Cyan
  if ($script:RunLogPath) {
    Add-Content -LiteralPath $script:RunLogPath -Value $line -Encoding UTF8
  }
}

function Start-Settings {
  param([string]$SN)

  $start = Invoke-HdcShell -SN $SN -Command "aa start -b com.ohos.settings -m phone -a com.ohos.settings.MainAbility"
  if ($start.ExitCode -ne 0) {
    throw "Failed to start Settings: $($start.Output)"
  }
  Assert-True ($start.Output -match "start ability successfully") "Settings start command did not report success: $($start.Output)"
}

function Assert-SettingsForeground {
  param([string]$SN)

  $dump = Invoke-HdcShell -SN $SN -Command "aa dump -a"
  if ($dump.ExitCode -ne 0) {
    throw "aa dump -a failed: $($dump.Output)"
  }

  $ok = ($dump.Output -match "bundle name \[com\.ohos\.settings\]") -and
        ($dump.Output -match "main name \[com\.ohos\.settings\.MainAbility\]") -and
        ($dump.Output -match "state #FOREGROUND")

  Assert-True $ok "Settings MainAbility is not foreground according to aa dump."
  return $dump.Output
}

function Unlock-IfNeeded {
  param(
    [string]$SN,
    [pscustomobject]$Facts
  )

  if ($Facts.HasSettingList -or $Facts.HasWifiPage) {
    Write-Step "Settings UI is already visible."
    return
  }

  Write-Step "Settings is foreground but UI is covered, sending unlock swipe."
  $swipe = Invoke-HdcShell -SN $SN -Command "uinput -T -m 360 1100 360 260 700"
  if ($swipe.ExitCode -ne 0) {
    throw "Unlock swipe failed: $($swipe.Output)"
  }

  Start-Sleep -Milliseconds 1200
}

function Click-WlanEntry {
  param([string]$SN)

  # Fixed coordinate validated on the current 720x1280 layout.
  # Revisit if DPI/resolution/home page layout changes.
  $click = Invoke-HdcShell -SN $SN -Command "uinput -T -c 180 306 80"
  if ($click.ExitCode -ne 0) {
    throw "WLAN click failed: $($click.Output)"
  }
  Start-Sleep -Milliseconds 1200
}

$ResolvedTarget = Resolve-HdcTarget -Target $Target
$env:WK_DEVICE_TARGET = $ResolvedTarget
$env:HDC_TARGET = $ResolvedTarget

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot ("_tmp_wifi_ui_runs\" + (Get-TimestampString))
}
Ensure-Dir $OutDir

$logPath = Join-Path $OutDir "run.log"
$script:RunLogPath = $logPath
Save-TextArtifact -Path $logPath -Content ""

try {
  Write-Step ("Target = " + $ResolvedTarget)
  Write-Step ("Artifacts = " + $OutDir)

  Write-Step "Starting Settings."
  Start-Settings -SN $ResolvedTarget
  $aaDumpStart = Assert-SettingsForeground -SN $ResolvedTarget
  Save-TextArtifact -Path (Join-Path $OutDir "01_aa_dump_after_start.txt") -Content $aaDumpStart

  Write-Step "Capturing initial UI state."
  $layoutStart = Capture-Layout -SN $ResolvedTarget -StepName "02_after_start" -ArtifactDir $OutDir
  $screenStart = Capture-Screen -SN $ResolvedTarget -StepName "02_after_start" -ArtifactDir $OutDir
  $factsStart = Analyze-Layout -LayoutJson $layoutStart.Json
  Assert-True ($factsStart.HasLockScreenText -or $factsStart.HasSettingList -or $factsStart.HasWifiPage) "Initial layout is neither lock screen nor Settings page."

  Unlock-IfNeeded -SN $ResolvedTarget -Facts $factsStart

  Write-Step "Capturing UI state after unlock handling."
  $layoutUnlocked = Capture-Layout -SN $ResolvedTarget -StepName "03_after_unlock" -ArtifactDir $OutDir
  $screenUnlocked = Capture-Screen -SN $ResolvedTarget -StepName "03_after_unlock" -ArtifactDir $OutDir
  $factsUnlocked = Analyze-Layout -LayoutJson $layoutUnlocked.Json

  Assert-True ($factsUnlocked.HasSettingList -or $factsUnlocked.HasWifiPage) "Expected Settings settingList or wifi page after unlock, but neither was found."

  if (-not $factsUnlocked.HasWifiPage) {
    Assert-True ($null -ne $factsUnlocked.WlanNode) "Could not locate WLAN entry in Settings home page."
    Write-Step "Clicking WLAN entry."
    Click-WlanEntry -SN $ResolvedTarget
  } else {
    Write-Step "Already on Wi-Fi page, skipping WLAN click."
  }

  Write-Step "Capturing final UI state."
  $layoutFinal = Capture-Layout -SN $ResolvedTarget -StepName "04_final" -ArtifactDir $OutDir
  $screenFinal = Capture-Screen -SN $ResolvedTarget -StepName "04_final" -ArtifactDir $OutDir
  $factsFinal = Analyze-Layout -LayoutJson $layoutFinal.Json

  Assert-True $factsFinal.HasWifiPage "Final layout is not pages/wifi."

  Write-Step "Success: reached Settings WLAN page."
  Write-Host ("SUCCESS pagePath=pages/wifi artifacts=" + $OutDir) -ForegroundColor Green
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
