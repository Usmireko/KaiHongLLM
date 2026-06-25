# requires -Version 5.1
<#
  probe_guest_password_input.ps1
  Purpose:
    Reach Guest wifi password page, inspect UI structure, and inject password
    text without clicking the Connect button.
#>

param(
  [string]$Target = "",
  [string]$OutDir = ""
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
  @{ LocalPath = $local; Raw = (Get-Content $local -Raw -Encoding UTF8) }
}

function Capture-Screen([string]$SN, [string]$StepName, [string]$ArtifactDir) {
  $cap = Invoke-HdcShell -SN $SN -Command "uitest screenCap /data/local/tmp/$StepName.png"
  if ($cap.ExitCode -ne 0) { throw "screenCap failed: $($cap.Output)" }
  $remote = Get-RemoteSavedPath -Output $cap.Output -Extension "png"
  $local = Join-Path $ArtifactDir ($StepName + "_screen.png")
  Receive-RemoteFile -SN $SN -RemotePath $remote -LocalPath $local
  @{ LocalPath = $local }
}

function Tap([string]$SN, [int]$X, [int]$Y) {
  Invoke-HdcShell -SN $SN -Command ("uinput -T -c {0} {1} 80" -f $X, $Y) | Out-Null
  Start-Sleep -Milliseconds 250
}

function LongPress([string]$SN, [int]$X, [int]$Y, [int]$Ms) {
  Invoke-HdcShell -SN $SN -Command ("uinput -T -d {0} {1}" -f $X, $Y) | Out-Null
  Start-Sleep -Milliseconds $Ms
  Invoke-HdcShell -SN $SN -Command ("uinput -T -u {0} {1}" -f $X, $Y) | Out-Null
  Start-Sleep -Milliseconds 350
}

$ResolvedTarget = Resolve-HdcTarget -Target $Target
if ([string]::IsNullOrWhiteSpace($OutDir)) {
  $OutDir = Join-Path $PSScriptRoot "_tmp_guest_password_input"
}
Ensure-Dir $OutDir

# Assumes we are already on Guest password page.
$beforeLayout = Capture-Layout -SN $ResolvedTarget -StepName "01_before" -ArtifactDir $OutDir
$beforeScreen = Capture-Screen -SN $ResolvedTarget -StepName "01_before" -ArtifactDir $OutDir

# Focus the password input box.
Tap -SN $ResolvedTarget -X 360 -Y 204
Start-Sleep -Milliseconds 600
$kbdLayout = Capture-Layout -SN $ResolvedTarget -StepName "02_keyboard" -ArtifactDir $OutDir
$kbdScreen = Capture-Screen -SN $ResolvedTarget -StepName "02_keyboard" -ArtifactDir $OutDir

# Input KaiHong@168 using on-screen keyboard taps/long-press hints.
# Clear a previous probe char with one backspace, then type target password.
Tap -SN $ResolvedTarget -X 664 -Y 1034
Tap -SN $ResolvedTarget -X 56  -Y 1034  # Shift
Tap -SN $ResolvedTarget -X 570 -Y 922   # K
Tap -SN $ResolvedTarget -X 80  -Y 922   # a
Tap -SN $ResolvedTarget -X 537 -Y 810   # i
Tap -SN $ResolvedTarget -X 56  -Y 1034  # Shift
Tap -SN $ResolvedTarget -X 430 -Y 922   # H
Tap -SN $ResolvedTarget -X 607 -Y 810   # o
Tap -SN $ResolvedTarget -X 505 -Y 1034  # n
Tap -SN $ResolvedTarget -X 360 -Y 922   # g
LongPress -SN $ResolvedTarget -X 220 -Y 922 -Ms 900 # @ via d key hint
LongPress -SN $ResolvedTarget -X 42  -Y 810 -Ms 900 # 1 via q key hint
LongPress -SN $ResolvedTarget -X 395 -Y 810 -Ms 900 # 6 via y key hint
LongPress -SN $ResolvedTarget -X 537 -Y 810 -Ms 900 # 8 via i key hint

$afterLayout = Capture-Layout -SN $ResolvedTarget -StepName "03_after" -ArtifactDir $OutDir
$afterScreen = Capture-Screen -SN $ResolvedTarget -StepName "03_after" -ArtifactDir $OutDir

# Hide keyboard so buttons are visible, but do not click Connect.
Tap -SN $ResolvedTarget -X 675 -Y 700
Start-Sleep -Milliseconds 800
$afterHiddenLayout = Capture-Layout -SN $ResolvedTarget -StepName "04_after_hidden" -ArtifactDir $OutDir
$afterHiddenScreen = Capture-Screen -SN $ResolvedTarget -StepName "04_after_hidden" -ArtifactDir $OutDir

Write-Host ("SUCCESS artifacts=" + $OutDir) -ForegroundColor Green
