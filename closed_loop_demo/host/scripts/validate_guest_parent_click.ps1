# requires -Version 5.1
param(
  [string]$Target = "",
  [string]$OutDir = "",
  [int]$GuestWaitTimeoutSec = 180,
  [int]$PollIntervalSec = 3
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot "tools\hdc_target.ps1")

function Ensure-Dir([string]$Dir) {
  if ([string]::IsNullOrWhiteSpace($Dir)) { return }
  if (-not (Test-Path -LiteralPath $Dir)) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  }
}

function Get-HdcExe {
  $cmd = Get-Command hdc -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "hdc not found in PATH." }
  return $cmd.Source
}

function Write-Step([string]$Message) {
  $line = "[validate_guest_parent_click] $Message"
  Write-Host $line -ForegroundColor Cyan
  if ($script:RunLogPath) {
    Add-Content -LiteralPath $script:RunLogPath -Value $line -Encoding UTF8
  }
}

function Invoke-HdcShell([string]$SN, [string]$Command) {
  $hdc = Get-HdcExe
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $lines = & $hdc -t $SN shell $Command 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { [string]$_ }
    }
  } finally {
    $ErrorActionPreference = $old
  }
  @{
    ExitCode = $LASTEXITCODE
    Output = (($lines -join "`n").Trim())
  }
}

function Receive-RemoteFile([string]$SN, [string]$RemotePath, [string]$LocalPath) {
  Ensure-Dir (Split-Path -Parent $LocalPath)
  $hdc = Get-HdcExe
  $all = (& $hdc -t $SN file recv $RemotePath $LocalPath 2>&1 | Out-String)
  if ($LASTEXITCODE -ne 0) { throw "recv failed: $RemotePath -> $LocalPath`n$all" }
}

function Get-RemoteSavedPath([string]$Output, [string]$Extension) {
  $m = [regex]::Match($Output, [regex]::Escape("/data/local/tmp/") + "[^`r`n\s]+\." + [regex]::Escape($Extension))
  if (-not $m.Success) { throw "Could not parse remote .$Extension path from: $Output" }
  $m.Value
}

function Capture-Layout([string]$SN, [string]$StepName, [string]$ArtifactDir) {
  $dump = Invoke-HdcShell -SN $SN -Command "uitest dumpLayout"
  if ($dump.ExitCode -ne 0) { throw "dumpLayout failed: $($dump.Output)" }
  $remote = Get-RemoteSavedPath -Output $dump.Output -Extension "json"
  $local = Join-Path $ArtifactDir ($StepName + "_layout.json")
  Receive-RemoteFile -SN $SN -RemotePath $remote -LocalPath $local
  $raw = Get-Content -LiteralPath $local -Raw -Encoding UTF8
  @{
    LocalPath = $local
    Raw = $raw
    Json = ($raw | ConvertFrom-Json)
  }
}

function Capture-Screen([string]$SN, [string]$StepName, [string]$ArtifactDir) {
  $cap = Invoke-HdcShell -SN $SN -Command "uitest screenCap /data/local/tmp/$StepName.png"
  if ($cap.ExitCode -ne 0) { throw "screenCap failed: $($cap.Output)" }
  $remote = Get-RemoteSavedPath -Output $cap.Output -Extension "png"
  $local = Join-Path $ArtifactDir ($StepName + "_screen.png")
  Receive-RemoteFile -SN $SN -RemotePath $remote -LocalPath $local
  @{ LocalPath = $local }
}

function Capture-AaDump([string]$SN, [string]$StepName, [string]$ArtifactDir) {
  $ret = Invoke-HdcShell -SN $SN -Command "aa dump -a"
  if ($ret.ExitCode -ne 0) { throw "aa dump failed: $($ret.Output)" }
  $local = Join-Path $ArtifactDir ($StepName + "_aa_dump.txt")
  $ret.Output | Set-Content -LiteralPath $local -Encoding UTF8
  @{ LocalPath = $local; Output = $ret.Output }
}

function Capture-WifiDevice([string]$SN, [string]$StepName, [string]$ArtifactDir) {
  $ret = Invoke-HdcShell -SN $SN -Command "hidumper -s WifiDevice"
  if ($ret.ExitCode -ne 0) { throw "WifiDevice dump failed: $($ret.Output)" }
  $local = Join-Path $ArtifactDir ($StepName + "_WifiDevice.txt")
  $ret.Output | Set-Content -LiteralPath $local -Encoding UTF8
  @{ LocalPath = $local; Output = $ret.Output }
}

function Get-NodeFacts {
  param(
    [object]$Node,
    [ref]$Collector,
    [string]$InheritedBundleName = "",
    [string]$InheritedPagePath = "",
    [object[]]$AncestorStack = @()
  )
  if ($null -eq $Node) { return }
  $bundleName = $InheritedBundleName
  $pagePath = $InheritedPagePath
  $attrs = $Node.attributes
  if ($null -ne $attrs) {
    $bundleName = [string]$attrs.bundleName
    if ([string]::IsNullOrWhiteSpace($bundleName)) { $bundleName = $InheritedBundleName }
    $pagePath = [string]$attrs.pagePath
    if ([string]::IsNullOrWhiteSpace($pagePath)) { $pagePath = $InheritedPagePath }
    $fact = [pscustomobject]@{
      BundleName = $bundleName
      PagePath = $pagePath
      Text = [string]$attrs.text
      Bounds = [string]$attrs.bounds
      Clickable = [string]$attrs.clickable
      Type = [string]$attrs.type
      Ancestors = $AncestorStack
    }
    $Collector.Value += $fact
    $next = @($AncestorStack + $fact)
  } else {
    $next = $AncestorStack
  }
  if ($Node.children) {
    foreach ($child in $Node.children) {
      Get-NodeFacts -Node $child -Collector $Collector -InheritedBundleName $bundleName -InheritedPagePath $pagePath -AncestorStack $next
    }
  }
}

function Analyze-Layout([object]$LayoutJson) {
  $nodes = @()
  Get-NodeFacts -Node $LayoutJson -Collector ([ref]$nodes)
  $pagePaths = @($nodes | ForEach-Object { $_.PagePath } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
  $guestText = $nodes | Where-Object { $_.BundleName -eq "com.ohos.settings" -and $_.PagePath -eq "pages/wifi" -and $_.Text -eq "Guest" } | Select-Object -First 1
  $guestRow = $null
  if ($null -ne $guestText) {
    $guestRow = @($guestText.Ancestors | Where-Object {
      $_.BundleName -eq "com.ohos.settings" -and
      $_.PagePath -eq "pages/wifi" -and
      $_.Clickable -eq "true" -and
      -not [string]::IsNullOrWhiteSpace($_.Bounds)
    } | Select-Object -Last 1)
    if ($guestRow.Count -gt 0) { $guestRow = $guestRow[0] } else { $guestRow = $null }
  }
  $pageTitle = @(
    $nodes | Where-Object {
      $_.PagePath -eq "pages/wifiPsd" -and
      -not [string]::IsNullOrWhiteSpace($_.Text) -and
      $_.Text -notin @("密码","取消","连接")
    } | Select-Object -ExpandProperty Text -First 1
  ) -join ""
  @{
    Nodes = $nodes
    PagePaths = $pagePaths
    HasWifiPage = ($pagePaths -contains "pages/wifi")
    GuestTextNode = $guestText
    GuestRowNode = $guestRow
    PageTitle = $pageTitle
  }
}

function Get-BoundsCenter([string]$Bounds) {
  $m = [regex]::Match($Bounds, "^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
  if (-not $m.Success) { throw "Invalid bounds: $Bounds" }
  $x1=[int]$m.Groups[1].Value; $y1=[int]$m.Groups[2].Value; $x2=[int]$m.Groups[3].Value; $y2=[int]$m.Groups[4].Value
  @{ X=[int](($x1+$x2)/2); Y=[int](($y1+$y2)/2) }
}

function Click-ByBounds([string]$SN, [string]$Bounds, [string]$Label) {
  $c = Get-BoundsCenter -Bounds $Bounds
  Write-Step ("Clicking {0} at ({1},{2}) from bounds {3}" -f $Label, $c.X, $c.Y, $Bounds)
  $ret = Invoke-HdcShell -SN $SN -Command ("uinput -T -c {0} {1} 80" -f $c.X, $c.Y)
  if ($ret.ExitCode -ne 0) { throw ("Click failed for " + $Label + ": " + $ret.Output) }
}

function Ensure-WifiPage([string]$SN, [string]$ArtifactDir) {
  $start = Invoke-HdcShell -SN $SN -Command "aa start -b com.ohos.settings -m phone -a com.ohos.settings.MainAbility"
  Write-Step ("aa start: " + $start.Output)
  Start-Sleep -Seconds 1
  $first = Capture-Layout -SN $SN -StepName "state_00" -ArtifactDir $ArtifactDir
  $facts = Analyze-Layout -LayoutJson $first.Json
  if ($facts.HasWifiPage) { return $facts }
  if ($first.Raw -match "上滑解锁|涓婃粦瑙ｉ攣") {
    Write-Step "Lock screen detected, swiping up."
    Invoke-HdcShell -SN $SN -Command "uinput -T -m 360 1100 360 260 700" | Out-Null
    Start-Sleep -Seconds 2
  }
  for ($i=1; $i -le 4; $i++) {
    $layout = Capture-Layout -SN $SN -StepName ("state_retry_" + $i) -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json
    if ($facts.HasWifiPage) { return $facts }
    if ($facts.PagePaths -contains "pages/wifiPsd") {
      Write-Step "Password page detected, clicking back."
      Invoke-HdcShell -SN $SN -Command "uinput -T -c 72 114 80" | Out-Null
    } elseif ($facts.PagePaths -contains "pages/settingList") {
      Write-Step "Settings home detected, clicking WLAN row."
      Invoke-HdcShell -SN $SN -Command "uinput -T -c 180 306 80" | Out-Null
    } else {
      Write-Step "Unknown page, trying WLAN row on settings home."
      Invoke-HdcShell -SN $SN -Command "uinput -T -c 180 306 80" | Out-Null
    }
    Start-Sleep -Seconds 2
  }
  throw "Could not return to pages/wifi."
}

$ResolvedTarget = Resolve-HdcTarget -Target $Target
$env:HDC_TARGET = $ResolvedTarget

if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot ("_tmp_guest_parent_click_" + (Get-Date).ToString("yyyyMMdd_HHmmss"))
}
Ensure-Dir $OutDir
$script:RunLogPath = Join-Path $OutDir "run.log"
"" | Set-Content -LiteralPath $script:RunLogPath -Encoding UTF8

Write-Step ("Target = " + $ResolvedTarget)
Write-Step ("Artifacts = " + $OutDir)

# connectivity check + one retry
$lt = & (Get-HdcExe) list targets -v
Add-Content -LiteralPath $script:RunLogPath -Value ($lt | Out-String) -Encoding UTF8
if (-not (($lt | Out-String) -match [regex]::Escape($ResolvedTarget) + ".*Connected")) {
  Write-Step "Target not connected on first check, retrying once."
  Start-Sleep -Seconds 3
  $lt2 = & (Get-HdcExe) list targets -v
  Add-Content -LiteralPath $script:RunLogPath -Value ($lt2 | Out-String) -Encoding UTF8
  if (-not (($lt2 | Out-String) -match [regex]::Escape($ResolvedTarget) + ".*Connected")) {
    throw "HDC target is not connected after one retry."
  }
}

$stateDir = Join-Path $OutDir "00_state"
Ensure-Dir $stateDir
$facts0 = Ensure-WifiPage -SN $ResolvedTarget -ArtifactDir $stateDir

$hitDir = Join-Path $OutDir "01_hit"
Ensure-Dir $hitDir
$hit = $null
$deadline = (Get-Date).AddSeconds($GuestWaitTimeoutSec)
$iter = 0
while ((Get-Date) -lt $deadline) {
  $iter++
  Write-Step ("Waiting for Guest hit iteration " + $iter)
  $layout = Capture-Layout -SN $ResolvedTarget -StepName ("hit_" + $iter.ToString("00")) -ArtifactDir $hitDir
  $screen = Capture-Screen -SN $ResolvedTarget -StepName ("hit_" + $iter.ToString("00")) -ArtifactDir $hitDir
  $wifi = Capture-WifiDevice -SN $ResolvedTarget -StepName ("hit_" + $iter.ToString("00")) -ArtifactDir $hitDir
  $facts = Analyze-Layout -LayoutJson $layout.Json
  if ($null -ne $facts.GuestTextNode -and $null -ne $facts.GuestRowNode) {
    $hit = @{
      Iteration = $iter
      Layout = $layout
      Screen = $screen
      Wifi = $wifi
      Facts = $facts
    }
    break
  }
  Start-Sleep -Seconds $PollIntervalSec
}

if ($null -eq $hit) { throw "Guest row was not found within timeout." }

$summary = [ordered]@{
  HitIteration = $hit.Iteration
  HitLayoutPath = $hit.Layout.LocalPath
  HitScreenPath = $hit.Screen.LocalPath
  GuestTextBounds = $hit.Facts.GuestTextNode.Bounds
  GuestRowBounds = $hit.Facts.GuestRowNode.Bounds
  ParentRowClick = $null
}

$clickDir = Join-Path $OutDir "02_parent_click"
Ensure-Dir $clickDir
Click-ByBounds -SN $ResolvedTarget -Bounds $hit.Facts.GuestRowNode.Bounds -Label "Guest parent row"

$post = @()
for ($i=1; $i -le 3; $i++) {
  Start-Sleep -Milliseconds 1200
  $layout = Capture-Layout -SN $ResolvedTarget -StepName ("parent_post_" + $i.ToString("00")) -ArtifactDir $clickDir
  $screen = Capture-Screen -SN $ResolvedTarget -StepName ("parent_post_" + $i.ToString("00")) -ArtifactDir $clickDir
  $aa = Capture-AaDump -SN $ResolvedTarget -StepName ("parent_post_" + $i.ToString("00")) -ArtifactDir $clickDir
  $facts = Analyze-Layout -LayoutJson $layout.Json
  $post += [ordered]@{
    Round = $i
    LayoutPath = $layout.LocalPath
    ScreenPath = $screen.LocalPath
    AaDumpPath = $aa.LocalPath
    PagePaths = @($facts.PagePaths)
    HasWifiPage = $facts.HasWifiPage
    PageTitle = $facts.PageTitle
  }
}

$summary.ParentRowClick = [ordered]@{
  Bounds = $hit.Facts.GuestRowNode.Bounds
  OutcomePage = if (@($post[0].PagePaths) -contains "pages/wifiPsd") { "pages/wifiPsd" } elseif ($post[0].HasWifiPage) { "pages/wifi" } else { "unknown" }
  PageTitle = @($post | Where-Object { -not [string]::IsNullOrWhiteSpace($_.PageTitle) } | Select-Object -ExpandProperty PageTitle -First 1) -join ""
  PostArtifacts = $post
}

$summaryPath = Join-Path $OutDir "guest_parent_click_summary.json"
($summary | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $summaryPath -Encoding UTF8
Write-Step ("Summary = " + $summaryPath)
Write-Host ("SUCCESS summary=" + $summaryPath) -ForegroundColor Green
