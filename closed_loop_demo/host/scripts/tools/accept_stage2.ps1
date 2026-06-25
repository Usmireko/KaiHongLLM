param(
  [string]$Target = "",
  [string]$Server = "",
  [string]$RepoDir = "/home/xrh/qwen3_os_fault"
)

$serverConfigPath = Join-Path $PSScriptRoot "server_config.ps1"
if (-not (Test-Path -LiteralPath $serverConfigPath)) {
  throw "Missing server config helper: $serverConfigPath"
}
. $serverConfigPath
if (-not $Server -or $Server.Trim() -eq "") {
  $Server = Get-Qwen3SshHost
}

$hdcTargetHelperPath = Join-Path $PSScriptRoot "hdc_target.ps1"
if (-not (Test-Path -LiteralPath $hdcTargetHelperPath)) {
  throw "Missing HDC target helper: $hdcTargetHelperPath"
}
. $hdcTargetHelperPath
$Target = Resolve-HdcTarget -Target $Target
$HdcTargetArgs = Get-HdcTargetArgs -Target $Target

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$boardScript = Join-Path $here "accept_board_stage2.sh"
$serverScript = Join-Path $here "accept_server_stage2.sh"

$boardRemote = "/data/local/tmp/accept_board_stage2.sh"
$serverRemote = "$RepoDir/tools/accept_server_stage2.sh"

Write-Host "[board] push $boardScript -> $boardRemote"
& hdc @HdcTargetArgs file send $boardScript $boardRemote | Out-Null
& hdc @HdcTargetArgs shell "chmod 755 $boardRemote" | Out-Null

Write-Host "[board] run accept_board_stage2.sh"
$boardOut = & hdc @HdcTargetArgs shell "sh $boardRemote"
$boardText = ($boardOut -join "`n")
Write-Host $boardText

$runId = ""
$mRun = [regex]::Match($boardText, "RUN_ID=([A-Za-z0-9_]+)")
if ($mRun.Success) {
  $runId = $mRun.Groups[1].Value.Trim()
}

Write-Host "[server] push $serverScript -> $serverRemote"
& scp $serverScript "${Server}:$serverRemote" | Out-Null

Write-Host "[server] run accept_server_stage2.sh"
if ($runId) {
  $serverOut = & ssh $Server "cd $RepoDir && sh tools/accept_server_stage2.sh $runId"
} else {
  $serverOut = & ssh $Server "cd $RepoDir && sh tools/accept_server_stage2.sh"
}
$serverText = ($serverOut -join "`n")
Write-Host $serverText

$boardResult = ""
$serverResult = ""
$mBoard = [regex]::Match($boardText, "RESULT=([A-Z]+)")
if ($mBoard.Success) { $boardResult = $mBoard.Groups[1].Value }
$mServer = [regex]::Match($serverText, "RESULT=([A-Z]+)")
if ($mServer.Success) { $serverResult = $mServer.Groups[1].Value }

if ($boardResult -eq "PASS" -and $serverResult -eq "PASS") {
  Write-Host "OVERALL=PASS"
} else {
  Write-Host "OVERALL=FAIL"
}
