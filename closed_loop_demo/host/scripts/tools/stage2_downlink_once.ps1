param(
  [string]$Target = "",
  [string]$Server = "",
  [string]$RepoDir = "/home/xrh/qwen3_os_fault",
  [string]$DeviceId = "dev1"
)

$serverConfigPath = Join-Path $PSScriptRoot "server_config.ps1"
if (-not (Test-Path -LiteralPath $serverConfigPath)) {
  throw "Missing server config helper: $serverConfigPath"
}
. $serverConfigPath
$cfg = Get-Qwen3ServerConfig
if (-not $Server -or $Server.Trim() -eq "") {
  $Server = $cfg.ssh_target
} elseif ($Server -notmatch "@" -and $cfg.ssh_user) {
  $Server = ($cfg.ssh_user + "@" + $Server)
}
$ServerPort = [int]$cfg.ssh_port

$hdcTargetHelperPath = Join-Path $PSScriptRoot "hdc_target.ps1"
if (-not (Test-Path -LiteralPath $hdcTargetHelperPath)) {
  throw "Missing HDC target helper: $hdcTargetHelperPath"
}
. $hdcTargetHelperPath
$Target = Resolve-HdcTarget -Target $Target
$HdcTargetArgs = Get-HdcTargetArgs -Target $Target

$bridgeHelperPath = Join-Path $PSScriptRoot "stage2_host_bridge.ps1"
if (-not (Test-Path -LiteralPath $bridgeHelperPath)) {
  throw "Missing Stage2 bridge helper: $bridgeHelperPath"
}
. $bridgeHelperPath

function Escape-BashSingleQuote([string]$s) {
  if ($null -eq $s) { return "" }
  return ($s -replace "'", "'\''")
}

$script:LastSshTimedOut = $false

function Invoke-Ssh([string]$Cmd, [int]$TimeoutSec = 20) {
  $script:LastSshTimedOut = $false
  $escaped = Escape-BashSingleQuote ($Cmd -replace "`r", "")
  $full = "bash -lc '$escaped'"
  $sshExe = "C:\Windows\System32\OpenSSH\ssh.exe"
  $args = @(
    "-p", [string]$ServerPort,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "StrictHostKeyChecking=no",
    "-T",
    $Server,
    $full
  )

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $sshExe
  $psi.Arguments = (($args | ForEach-Object {
    if ($_ -match "\s") { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
  }) -join " ")
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  $null = $proc.Start()
  $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
  $stderrTask = $proc.StandardError.ReadToEndAsync()
  if (-not $proc.WaitForExit([int]([math]::Max(1, $TimeoutSec) * 1000))) {
    try { $proc.Kill() } catch {}
    $script:LastSshTimedOut = $true
    return ""
  }
  $proc.WaitForExit()
  return (($stdoutTask.Result + $stderrTask.Result).Trim())
}

$items = Invoke-Stage2BoardDownlinkOnce -HdcArgs $HdcTargetArgs -Server $Server -ServerPort $ServerPort -RepoDir $RepoDir -DeviceId $DeviceId
if (-not $items -or $items.Count -eq 0) {
  Write-Host "DOWNLINK_IDLE"
  exit 0
}

$bad = $false
foreach ($item in $items) {
  if ($item.ok) {
    Write-Host ("DOWNLINK_OK device={0} run={1}" -f $item.device, $item.run_id)
  } else {
    Write-Host ("DOWNLINK_FAIL step={0} device={1} run={2} msg={3}" -f $item.step, $item.device, $item.run_id, $item.msg)
    $bad = $true
  }
}

if ($bad) { exit 1 }
