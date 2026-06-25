param(
  [string]$ServerUser = "xrh",
  [string]$RepoDir = "/home/xrh/qwen3_os_fault",
  [string]$DeviceId = "dev1"
)

$cfgHelper = Join-Path $PSScriptRoot "server_config.ps1"
if (-not (Test-Path -LiteralPath $cfgHelper)) {
  throw "missing helper: $cfgHelper"
}
. $cfgHelper

$cfg = Get-Qwen3ServerConfig

Write-Host "effective_config:"
Write-Host ("  host        : {0}" -f $cfg.host)
Write-Host ("  ingest_port : {0}" -f $cfg.ingest_port)
Write-Host ("  actions_port: {0}" -f $cfg.actions_port)
Write-Host ("  ssh_host    : {0}" -f $cfg.ssh_host)
Write-Host ""

$sshCmd = ("ssh {0}@{1} ""cd '{2}' && ls -la storage""" -f $ServerUser, $cfg.ssh_host, $RepoDir)
$curlCmd = ("curl -v http://{0}:{1}/healthz" -f $cfg.host, $cfg.ingest_port)
$ncCmd = ("printf ""DEVICE={0}\n\n"" | nc {1} {2}" -f $DeviceId, $cfg.host, $cfg.actions_port)

Write-Host "dry_run_examples:"
Write-Host ("  ssh : {0}" -f $sshCmd)
Write-Host ("  curl: {0}" -f $curlCmd)
Write-Host ("  nc  : {0}" -f $ncCmd)

