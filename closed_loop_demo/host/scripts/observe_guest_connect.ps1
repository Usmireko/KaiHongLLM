# requires -Version 5.1
<#
  observe_guest_connect.ps1
  Reach Guest wifi password page, inject password, click Connect, and observe
  UI/system state changes for ~30 seconds.
#>

param(
  [string]$Target = "",
  [string]$OutDir = "",
  [int]$GuestWaitTimeoutSec = 180,
  [int]$PollIntervalSec = 3,
  [int]$ObserveDurationSec = 27
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
  $cmd.Source
}

function Write-Step([string]$Message) {
  $line = "[observe_guest_connect] $Message"
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

function Capture-Text([string]$SN, [string]$Command, [string]$StepName, [string]$Suffix, [string]$ArtifactDir) {
  $ret = Invoke-HdcShell -SN $SN -Command $Command
  if ($ret.ExitCode -ne 0) { throw "$Command failed: $($ret.Output)" }
  $local = Join-Path $ArtifactDir ($StepName + "_" + $Suffix + ".txt")
  $ret.Output | Set-Content -LiteralPath $local -Encoding UTF8
  @{ LocalPath = $local; Output = $ret.Output }
}

function Tap([string]$SN, [int]$X, [int]$Y) {
  $ret = Invoke-HdcShell -SN $SN -Command ("uinput -T -c {0} {1} 80" -f $X, $Y)
  if ($ret.ExitCode -ne 0) { throw "tap failed at ($X,$Y): $($ret.Output)" }
  Start-Sleep -Milliseconds 300
}

function LongPress([string]$SN, [int]$X, [int]$Y, [int]$Ms) {
  $down = Invoke-HdcShell -SN $SN -Command ("uinput -T -d {0} {1}" -f $X, $Y)
  if ($down.ExitCode -ne 0) { throw "long press down failed at ($X,$Y): $($down.Output)" }
  Start-Sleep -Milliseconds $Ms
  $up = Invoke-HdcShell -SN $SN -Command ("uinput -T -u {0} {1}" -f $X, $Y)
  if ($up.ExitCode -ne 0) { throw "long press up failed at ($X,$Y): $($up.Output)" }
  Start-Sleep -Milliseconds 400
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
      Focused = [string]$attrs.focused
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
  $guestText = $nodes | Where-Object {
    $_.BundleName -eq "com.ohos.settings" -and $_.PagePath -eq "pages/wifi" -and $_.Text -eq "Guest"
  } | Select-Object -First 1
  $guestRow = $null
  if ($null -ne $guestText) {
    $candidate = @($guestText.Ancestors | Where-Object {
      $_.BundleName -eq "com.ohos.settings" -and
      $_.PagePath -eq "pages/wifi" -and
      $_.Clickable -eq "true" -and
      -not [string]::IsNullOrWhiteSpace($_.Bounds)
    } | Select-Object -Last 1)
    if ($candidate.Count -gt 0) { $guestRow = $candidate[0] }
  }
  $passwordInput = $nodes | Where-Object {
    $_.BundleName -eq "com.ohos.settings" -and
    $_.PagePath -eq "pages/wifiPsd" -and
    $_.Type -eq "TextInput"
  } | Select-Object -First 1
  $connectText = $null
  $connectButton = $nodes | Where-Object {
    if ($_.BundleName -ne "com.ohos.settings") { return $false }
    if ($_.PagePath -ne "pages/wifiPsd") { return $false }
    if ($_.Clickable -ne "true") { return $false }
    if ([string]::IsNullOrWhiteSpace($_.Bounds)) { return $false }
    $m = [regex]::Match($_.Bounds, "^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
    if (-not $m.Success) { return $false }
    $x1 = [int]$m.Groups[1].Value
    $y1 = [int]$m.Groups[2].Value
    $x2 = [int]$m.Groups[3].Value
    $y2 = [int]$m.Groups[4].Value
    ($y1 -ge 1050) -and ($x1 -ge 300) -and (($x2 - $x1) -ge 200) -and (($y2 - $y1) -ge 40)
  } | Select-Object -First 1
  $titleNode = $nodes | Where-Object {
    $_.BundleName -eq "com.ohos.settings" -and
    $_.PagePath -eq "pages/wifiPsd" -and
    $_.Text -eq "Guest"
  } | Select-Object -First 1
  @{
    Nodes = $nodes
    PagePaths = $pagePaths
    HasWifiPage = ($pagePaths -contains "pages/wifi")
    HasWifiPsdPage = ($pagePaths -contains "pages/wifiPsd")
    GuestTextNode = $guestText
    GuestRowNode = $guestRow
    PasswordInputNode = $passwordInput
    ConnectTextNode = $connectText
    ConnectButtonNode = $connectButton
    TitleNode = $titleNode
  }
}

function Get-BoundsCenter([string]$Bounds) {
  $m = [regex]::Match($Bounds, "^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
  if (-not $m.Success) { throw "Invalid bounds: $Bounds" }
  $x1=[int]$m.Groups[1].Value
  $y1=[int]$m.Groups[2].Value
  $x2=[int]$m.Groups[3].Value
  $y2=[int]$m.Groups[4].Value
  @{ X=[int](($x1+$x2)/2); Y=[int](($y1+$y2)/2) }
}

function Click-ByBounds([string]$SN, [string]$Bounds, [string]$Label) {
  $c = Get-BoundsCenter -Bounds $Bounds
  Write-Step ("Clicking {0} at ({1},{2}) from bounds {3}" -f $Label, $c.X, $c.Y, $Bounds)
  Tap -SN $SN -X $c.X -Y $c.Y
  @{ X = $c.X; Y = $c.Y }
}

function Ensure-Connectivity([string]$SN) {
  $hdc = Get-HdcExe
  for ($i = 1; $i -le 2; $i++) {
    $lt = & $hdc list targets -v | Out-String
    Add-Content -LiteralPath $script:RunLogPath -Value $lt -Encoding UTF8
    if ($lt -match ([regex]::Escape($SN) + ".*Connected")) { return }
    Write-Step ("Target not connected on check " + $i + ", retrying.")
    Start-Sleep -Seconds 3
  }
  throw "HDC target is not connected after retries."
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
  for ($i = 1; $i -le 5; $i++) {
    $layout = Capture-Layout -SN $SN -StepName ("state_retry_" + $i) -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json
    if ($facts.HasWifiPage) { return $facts }
    if ($facts.HasWifiPsdPage) {
      Write-Step "Password page detected, clicking back."
      Tap -SN $SN -X 72 -Y 114
    } elseif ($facts.PagePaths -contains "pages/settingList") {
      Write-Step "Settings home detected, clicking WLAN row."
      Tap -SN $SN -X 180 -Y 306
    } else {
      Write-Step "Unknown page, trying WLAN row."
      Tap -SN $SN -X 180 -Y 306
    }
    Start-Sleep -Seconds 2
  }
  throw "Could not reach pages/wifi."
}

function Ensure-Activated([string]$SN) {
  for ($i = 1; $i -le 5; $i++) {
    $ret = Invoke-HdcShell -SN $SN -Command "hidumper -s WifiDevice"
    if ($ret.Output -match "WiFi active state:\s+activated") { return }
    Write-Step "Wifi not yet activated, waiting."
    Start-Sleep -Seconds 2
  }
  throw "WifiDevice did not reach activated."
}

function Wait-ForGuestAndOpenPasswordPage([string]$SN, [string]$ArtifactDir, [int]$TimeoutSec, [int]$IntervalSec) {
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $iter = 0
  while ((Get-Date) -lt $deadline) {
    $iter++
    Write-Step ("Waiting for Guest row iteration " + $iter)
    $layout = Capture-Layout -SN $SN -StepName ("guest_" + $iter.ToString("00")) -ArtifactDir $ArtifactDir
    $screen = Capture-Screen -SN $SN -StepName ("guest_" + $iter.ToString("00")) -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json
    if ($null -ne $facts.GuestTextNode -and $null -ne $facts.GuestRowNode) {
      $verifyLayout = Capture-Layout -SN $SN -StepName ("guest_" + $iter.ToString("00") + "_verify") -ArtifactDir $ArtifactDir
      $verifyScreen = Capture-Screen -SN $SN -StepName ("guest_" + $iter.ToString("00") + "_verify") -ArtifactDir $ArtifactDir
      $verifyFacts = Analyze-Layout -LayoutJson $verifyLayout.Json
      if ($null -ne $verifyFacts.GuestTextNode -and $null -ne $verifyFacts.GuestRowNode) {
        $click = Click-ByBounds -SN $SN -Bounds $verifyFacts.GuestRowNode.Bounds -Label "Guest parent row"
        Start-Sleep -Seconds 2
        return @{
          HitLayout = $layout
          HitScreen = $screen
          VerifyLayout = $verifyLayout
          VerifyScreen = $verifyScreen
          GuestTextBounds = $verifyFacts.GuestTextNode.Bounds
          GuestRowBounds = $verifyFacts.GuestRowNode.Bounds
          Click = $click
        }
      }
      Write-Step "Guest verify failed due to list refresh, continuing."
    }
    Start-Sleep -Seconds $IntervalSec
  }
  throw "Guest row was not found within timeout."
}

function Ensure-PasswordPage([string]$SN, [string]$ArtifactDir) {
  for ($i = 1; $i -le 5; $i++) {
    $layout = Capture-Layout -SN $SN -StepName ("password_" + $i.ToString("00")) -ArtifactDir $ArtifactDir
    $screen = Capture-Screen -SN $SN -StepName ("password_" + $i.ToString("00")) -ArtifactDir $ArtifactDir
    $facts = Analyze-Layout -LayoutJson $layout.Json
    if ($facts.HasWifiPsdPage -and $null -ne $facts.PasswordInputNode -and $null -ne $facts.ConnectButtonNode) {
      return @{
        Layout = $layout
        Screen = $screen
        Facts = $facts
      }
    }
    Start-Sleep -Seconds 1
  }
  throw "Could not confirm Guest password page."
}

function Type-Password([string]$SN) {
  Tap -SN $SN -X 360 -Y 204
  Start-Sleep -Milliseconds 600
  Tap -SN $SN -X 664 -Y 1034
  Tap -SN $SN -X 56  -Y 1034
  Tap -SN $SN -X 570 -Y 922
  Tap -SN $SN -X 80  -Y 922
  Tap -SN $SN -X 537 -Y 810
  Tap -SN $SN -X 56  -Y 1034
  Tap -SN $SN -X 430 -Y 922
  Tap -SN $SN -X 607 -Y 810
  Tap -SN $SN -X 505 -Y 1034
  Tap -SN $SN -X 360 -Y 922
  LongPress -SN $SN -X 220 -Y 922 -Ms 900
  LongPress -SN $SN -X 42  -Y 810 -Ms 900
  LongPress -SN $SN -X 395 -Y 810 -Ms 900
  LongPress -SN $SN -X 537 -Y 810 -Ms 900
  Tap -SN $SN -X 675 -Y 700
  Start-Sleep -Seconds 1
}

function Observe-Round([string]$SN, [string]$StepName, [string]$ArtifactDir) {
  $layout = Capture-Layout -SN $SN -StepName $StepName -ArtifactDir $ArtifactDir
  $screen = Capture-Screen -SN $SN -StepName $StepName -ArtifactDir $ArtifactDir
  $wifi = Capture-Text -SN $SN -Command "hidumper -s WifiDevice" -StepName $StepName -Suffix "WifiDevice" -ArtifactDir $ArtifactDir
  $netConn = Capture-Text -SN $SN -Command "hidumper -s NetConnManager" -StepName $StepName -Suffix "NetConnManager" -ArtifactDir $ArtifactDir
  $ifconfig = Capture-Text -SN $SN -Command "ifconfig -a" -StepName $StepName -Suffix "ifconfig" -ArtifactDir $ArtifactDir
  $route = Capture-Text -SN $SN -Command "cat /proc/net/route" -StepName $StepName -Suffix "route" -ArtifactDir $ArtifactDir
  $facts = Analyze-Layout -LayoutJson $layout.Json
  @{
    Step = $StepName
    Layout = $layout
    Screen = $screen
    Wifi = $wifi
    NetConn = $netConn
    Ifconfig = $ifconfig
    Route = $route
    Facts = $facts
  }
}

function Get-WifiActiveState([string]$Text) {
  $m = [regex]::Match($Text, "WiFi active state:\s+([^\r\n]+)")
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  ""
}

function Get-WifiConnStatus([string]$Text) {
  $m = [regex]::Match($Text, "WiFi connection status:\s+([^\r\n]+)")
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  ""
}

function Has-WlanIp([string]$Text) {
  $m = [regex]::Match($Text, "(?ms)^wlan0\b.*?inet\s")
  $m.Success
}

function Get-PageLabel([hashtable]$Facts) {
  if ($Facts.HasWifiPsdPage) { return "pages/wifiPsd" }
  if ($Facts.HasWifiPage) { return "pages/wifi" }
  if ($Facts.PagePaths.Count -gt 0) { return ($Facts.PagePaths -join ",") }
  "unknown"
}

$ResolvedTarget = Resolve-HdcTarget -Target $Target
$env:HDC_TARGET = $ResolvedTarget
if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot ("_tmp_guest_connect_" + (Get-Date).ToString("yyyyMMdd_HHmmss"))
}
Ensure-Dir $OutDir
$script:RunLogPath = Join-Path $OutDir "run.log"
"" | Set-Content -LiteralPath $script:RunLogPath -Encoding UTF8

Write-Step ("Target = " + $ResolvedTarget)
Write-Step ("Artifacts = " + $OutDir)
Ensure-Connectivity -SN $ResolvedTarget

$stateDir = Join-Path $OutDir "00_state"
Ensure-Dir $stateDir
Ensure-WifiPage -SN $ResolvedTarget -ArtifactDir $stateDir | Out-Null
Ensure-Activated -SN $ResolvedTarget

$guestDir = Join-Path $OutDir "01_guest_hit"
Ensure-Dir $guestDir
$guest = Wait-ForGuestAndOpenPasswordPage -SN $ResolvedTarget -ArtifactDir $guestDir -TimeoutSec $GuestWaitTimeoutSec -IntervalSec $PollIntervalSec

$passwordDir = Join-Path $OutDir "02_password_page"
Ensure-Dir $passwordDir
Ensure-PasswordPage -SN $ResolvedTarget -ArtifactDir $passwordDir | Out-Null

$preDir = Join-Path $OutDir "03_pre_connect"
Ensure-Dir $preDir
$before = Observe-Round -SN $ResolvedTarget -StepName "before_connect" -ArtifactDir $preDir

Write-Step "Typing KaiHong@168 into password field."
Type-Password -SN $ResolvedTarget
$preFilled = Observe-Round -SN $ResolvedTarget -StepName "before_click_filled" -ArtifactDir $preDir

if ($null -eq $preFilled.Facts.ConnectButtonNode) { throw "Connect button not found after password input." }
$click = Click-ByBounds -SN $ResolvedTarget -Bounds $preFilled.Facts.ConnectButtonNode.Bounds -Label "Connect button"
$clickTime = Get-Date

$postDir = Join-Path $OutDir "04_post_connect"
Ensure-Dir $postDir
$rounds = @()
$iterations = [Math]::Max(7, [int][Math]::Ceiling($ObserveDurationSec / [Math]::Max(1, $PollIntervalSec)))
for ($i = 1; $i -le $iterations; $i++) {
  Start-Sleep -Seconds $PollIntervalSec
  $rounds += Observe-Round -SN $ResolvedTarget -StepName ("post_" + $i.ToString("00")) -ArtifactDir $postDir
}

$summary = [ordered]@{
  Target = $ResolvedTarget
  GuestTextBounds = $guest.GuestTextBounds
  GuestRowBounds = $guest.GuestRowBounds
  ConnectButtonBounds = $preFilled.Facts.ConnectButtonNode.Bounds
  ConnectClick = [ordered]@{
    X = $click.X
    Y = $click.Y
    Time = $clickTime.ToString("yyyy-MM-dd HH:mm:ss")
  }
  PreConnect = [ordered]@{
    LayoutPath = $before.Layout.LocalPath
    ScreenPath = $before.Screen.LocalPath
    WifiDevicePath = $before.Wifi.LocalPath
    NetConnManagerPath = $before.NetConn.LocalPath
    IfconfigPath = $before.Ifconfig.LocalPath
    RoutePath = $before.Route.LocalPath
    Page = (Get-PageLabel -Facts $before.Facts)
    WifiActiveState = (Get-WifiActiveState -Text $before.Wifi.Output)
    WifiConnectionStatus = (Get-WifiConnStatus -Text $before.Wifi.Output)
    HasWlanIp = (Has-WlanIp -Text $before.Ifconfig.Output)
  }
  FilledBeforeClick = [ordered]@{
    LayoutPath = $preFilled.Layout.LocalPath
    ScreenPath = $preFilled.Screen.LocalPath
    WifiDevicePath = $preFilled.Wifi.LocalPath
    NetConnManagerPath = $preFilled.NetConn.LocalPath
    IfconfigPath = $preFilled.Ifconfig.LocalPath
    RoutePath = $preFilled.Route.LocalPath
    Page = (Get-PageLabel -Facts $preFilled.Facts)
    WifiActiveState = (Get-WifiActiveState -Text $preFilled.Wifi.Output)
    WifiConnectionStatus = (Get-WifiConnStatus -Text $preFilled.Wifi.Output)
    HasWlanIp = (Has-WlanIp -Text $preFilled.Ifconfig.Output)
  }
  PostRounds = @(
    $rounds | ForEach-Object {
      [ordered]@{
        Step = $_.Step
        LayoutPath = $_.Layout.LocalPath
        ScreenPath = $_.Screen.LocalPath
        WifiDevicePath = $_.Wifi.LocalPath
        NetConnManagerPath = $_.NetConn.LocalPath
        IfconfigPath = $_.Ifconfig.LocalPath
        RoutePath = $_.Route.LocalPath
        Page = (Get-PageLabel -Facts $_.Facts)
        HasGuestTitle = ($null -ne $_.Facts.TitleNode)
        WifiActiveState = (Get-WifiActiveState -Text $_.Wifi.Output)
        WifiConnectionStatus = (Get-WifiConnStatus -Text $_.Wifi.Output)
        HasWlanIp = (Has-WlanIp -Text $_.Ifconfig.Output)
      }
    }
  )
}

$summaryPath = Join-Path $OutDir "guest_connect_summary.json"
($summary | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $summaryPath -Encoding UTF8
Write-Step ("Summary = " + $summaryPath)
Write-Host ("SUCCESS summary=" + $summaryPath) -ForegroundColor Green
