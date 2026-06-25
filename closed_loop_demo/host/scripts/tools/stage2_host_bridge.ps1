param()

function Ensure-Stage2BridgeDir([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
  }
}

function Get-Stage2BridgeLocalRoot {
  $repoRoot = Split-Path -Parent $PSScriptRoot
  return (Join-Path $repoRoot "_stage2_bridge")
}

function Read-Stage2MetaFile([string]$Path) {
  $map = @{}
  if (-not (Test-Path -LiteralPath $Path)) { return $map }
  foreach ($line in (Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
    if ($line -notmatch "=") { continue }
    $parts = $line.Split("=", 2)
    if ($parts.Length -ne 2) { continue }
    $map[$parts[0].Trim().ToUpperInvariant()] = $parts[1].Trim()
  }
  return $map
}

function Invoke-Stage2ScpUploadFile {
  param(
    [Parameter(Mandatory=$true)][string]$LocalPath,
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$RemotePath
  )

  $scpExe = "C:\Windows\System32\OpenSSH\scp.exe"
  $args = @(
    "-P", [string]$Port,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=no",
    $LocalPath,
    ($Server + ":" + $RemotePath)
  )
  & $scpExe @args 2>&1 | Out-Null
  return $LASTEXITCODE
}

function Invoke-Stage2ScpDownloadFile {
  param(
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$RemotePath,
    [Parameter(Mandatory=$true)][string]$LocalPath
  )

  $scpExe = "C:\Windows\System32\OpenSSH\scp.exe"
  $args = @(
    "-P", [string]$Port,
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=no",
    ($Server + ":" + $RemotePath),
    $LocalPath
  )
  & $scpExe @args 2>&1 | Out-Null
  return $LASTEXITCODE
}

function Publish-Stage2InboxItem {
  param(
    [Parameter(Mandatory=$true)][string]$LocalFile,
    [Parameter(Mandatory=$true)][string]$DeviceId,
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$RepoDir
  )

  $baseName = Split-Path -Leaf $LocalFile
  $remoteDir = "$RepoDir/storage/tcp_inbox/$DeviceId"
  $remoteTmp = "$remoteDir/$baseName.host_tmp"
  $remoteFile = "$remoteDir/$baseName"
  $remoteDone = "$remoteFile.done"

  $mkdirCmd = "mkdir -p '$remoteDir'; printf '__BRIDGE_MK_OK__\n'"
  $mkOut = Invoke-Ssh $mkdirCmd 20
  if ($script:LastSshTimedOut) {
    return @{ ok = $false; msg = "ssh_timeout_mkdir" }
  }
  if ($mkOut -notmatch "__BRIDGE_MK_OK__") {
    return @{ ok = $false; msg = "ssh_mkdir_failed" }
  }

  $scpRc = Invoke-Stage2ScpUploadFile -LocalPath $LocalFile -Server $Server -Port $Port -RemotePath $remoteTmp
  if ($scpRc -ne 0) {
    return @{ ok = $false; msg = ("scp_failed_rc_" + $scpRc) }
  }

  $verifyCmd = @(
    "set -e",
    "mkdir -p '$remoteDir'",
    "rm -f '$remoteFile' '$remoteDone'",
    "python3 -c ""import tarfile; tarfile.open(r'$remoteTmp', 'r:*').getmembers()""",
    "mv '$remoteTmp' '$remoteFile'",
    "printf 'ok\n' > '$remoteDone'",
    "printf '__BRIDGE_OK__\n'"
  ) -join "; "
  $verifyOut = Invoke-Ssh $verifyCmd 30
  if ($script:LastSshTimedOut) {
    return @{ ok = $false; msg = "ssh_timeout_verify" }
  }
  if ($verifyOut -notmatch "__BRIDGE_OK__") {
    return @{ ok = $false; msg = "remote_verify_failed" }
  }
  return @{ ok = $true; msg = "ok"; remote_file = $remoteFile }
}

function Get-Stage2ServerActionsState {
  param(
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][int]$Port,
    [Parameter(Mandatory=$true)][string]$RepoDir,
    [Parameter(Mandatory=$true)][string]$DeviceId
  )

  $remoteDir = "$RepoDir/storage/tcp_out/$DeviceId"
  $localRoot = Get-Stage2BridgeLocalRoot
  Ensure-Stage2BridgeDir $localRoot
  $localDir = Join-Path $localRoot ("downlink\" + $DeviceId)
  Ensure-Stage2BridgeDir $localDir

  $localRun = Join-Path $localDir "latest_run_id.txt"
  $localActions = Join-Path $localDir "latest_actions_device.txt"
  $localConclusion = Join-Path $localDir "latest_conclusion.txt"
  $rcRun = Invoke-Stage2ScpDownloadFile -Server $Server -Port $Port -RemotePath "$remoteDir/latest_run_id.txt" -LocalPath $localRun
  $rcActions = Invoke-Stage2ScpDownloadFile -Server $Server -Port $Port -RemotePath "$remoteDir/latest_actions_device.txt" -LocalPath $localActions
  if ($rcRun -ne 0 -or $rcActions -ne 0 -or -not (Test-Path -LiteralPath $localRun) -or -not (Test-Path -LiteralPath $localActions)) {
    return $null
  }

  $run = (Get-Content -LiteralPath $localRun -TotalCount 1 -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($null -eq $run) { $run = "" } else { $run = $run.ToString().Trim() }
  $len = 0
  try { $len = [int](Get-Item -LiteralPath $localActions).Length } catch { $len = 0 }
  if (-not $run -or $len -le 0) { return $null }

  # Download conclusion (best-effort; not required for success)
  $null = Invoke-Stage2ScpDownloadFile -Server $Server -Port $Port -RemotePath "$remoteDir/latest_conclusion.txt" -LocalPath $localConclusion
  $conclusion = ""
  if (Test-Path -LiteralPath $localConclusion) {
    $conclusion = (Get-Content -LiteralPath $localConclusion -Raw -ErrorAction SilentlyContinue)
    if ($null -ne $conclusion) { $conclusion = $conclusion.Trim() } else { $conclusion = "" }
  }

  return [pscustomobject]@{
    run_id = $run
    len = $len
    conclusion = $conclusion
    remote_dir = $remoteDir
    remote_actions = "$remoteDir/latest_actions_device.txt"
    remote_run = "$remoteDir/latest_run_id.txt"
    local_actions = $localActions
    local_run = $localRun
    local_conclusion = $localConclusion
  }
}

function Get-Stage2BoardDownlinkState {
  param(
    [Parameter(Mandatory=$true)][string[]]$HdcArgs,
    [string]$InboxDir = "/data/faultmon/demo_stage2/inbox"
  )

  $scriptBody = @'
ready=; done=; [ -f __INBOX__/actions.ready ] && [ -f __INBOX__/latest_run_id.txt ] && ready=$(head -n 1 __INBOX__/latest_run_id.txt 2>/dev/null); [ -f __INBOX__/actions.done ] && done=$(head -n 1 __INBOX__/actions.done 2>/dev/null); printf 'READY=%s\nDONE=%s\n' $ready $done
'@
  $scriptBody = $scriptBody.Replace("__INBOX__", $InboxDir)
  $out = @(& hdc @HdcArgs shell $scriptBody 2>&1)
  $ready = ""
  $done = ""
  foreach ($raw in $out) {
    $line = ($raw | Out-String).Trim()
    if ($line -like "READY=*") { $ready = $line.Substring(6).Trim() }
    if ($line -like "DONE=*") { $done = $line.Substring(5).Trim() }
  }
  return [pscustomobject]@{ ready = $ready; done = $done }
}

function Invoke-Stage2BoardDownlinkOnce {
  param(
    [Parameter(Mandatory=$true)][string[]]$HdcArgs,
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][int]$ServerPort,
    [Parameter(Mandatory=$true)][string]$RepoDir,
    [Parameter(Mandatory=$true)][string]$DeviceId,
    [string]$InboxDir = "/data/faultmon/demo_stage2/inbox"
  )

  $state = Get-Stage2ServerActionsState -Server $Server -Port $ServerPort -RepoDir $RepoDir -DeviceId $DeviceId
  if ($null -eq $state) { return @() }

  $board = Get-Stage2BoardDownlinkState -HdcArgs $HdcArgs -InboxDir $InboxDir
  if ($state.run_id -eq $board.ready -or $state.run_id -eq $board.done) {
    return @()
  }

  $remoteActions = "$InboxDir/actions_device.txt"
  $remoteRun = "$InboxDir/latest_run_id.txt"
  $mkdirInboxCmd = "mkdir -p $InboxDir"
  $null = @(& hdc @HdcArgs shell $mkdirInboxCmd 2>&1)
  $send1 = @(& hdc @HdcArgs file send $state.local_actions $remoteActions 2>&1)
  $send1Rc = $LASTEXITCODE
  $send2 = @(& hdc @HdcArgs file send $state.local_run $remoteRun 2>&1)
  $send2Rc = $LASTEXITCODE
  if ($send1Rc -ne 0 -or $send2Rc -ne 0) {
    return @([pscustomobject]@{ ok = $false; device = $DeviceId; run_id = $state.run_id; step = "hdc_send"; msg = (($send1 + $send2) -join "`n").Trim() })
  }
  # Best-effort: push conclusion to board inbox so triggerd can surface it via status/latest
  if ($state.conclusion -and (Test-Path -LiteralPath $state.local_conclusion)) {
    $remoteConclusion = "$InboxDir/latest_conclusion.txt"
    $null = @(& hdc @HdcArgs file send $state.local_conclusion $remoteConclusion 2>&1)
  }

  $markCmd = "mkdir -p $InboxDir; rm -f $InboxDir/actions.done $InboxDir/actions.fail; echo $($state.run_id) > $InboxDir/actions.ready"
  $null = @(& hdc @HdcArgs shell $markCmd 2>&1)

  return @([pscustomobject]@{ ok = $true; device = $DeviceId; run_id = $state.run_id; step = "downlink"; msg = "delivered" })
}

function Get-Stage2BoardReadyItems {
  param(
    [Parameter(Mandatory=$true)][string[]]$HdcArgs,
    [string]$SpoolRoot = "/data/faultmon/demo_stage2/spool"
  )

  $scriptBody = @'
for d in __SPOOL_ROOT__/*; do [ -d $d ] || continue; dev=${d##*/}; for r in $d/*.ready; do [ -f $r ] || continue; base=${r##*/}; name=$(echo $base | sed s/\.ready$//); printf '%s|%s\n' $dev $name; done; done
'@
  $scriptBody = $scriptBody.Replace("__SPOOL_ROOT__", $SpoolRoot)
  $lines = @(& hdc @HdcArgs shell $scriptBody 2>&1)
  $items = @()
  foreach ($raw in $lines) {
    $line = ($raw | Out-String).Trim()
    if (-not $line) { continue }
    if ($line -notmatch "^\s*([^|]+)\|(.+)$") { continue }
    $items += [pscustomobject]@{
      device = $Matches[1].Trim()
      name   = $Matches[2].Trim()
    }
  }
  return $items
}

function Set-Stage2BoardMarker {
  param(
    [Parameter(Mandatory=$true)][string[]]$HdcArgs,
    [Parameter(Mandatory=$true)][string]$DeviceId,
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$Status,
    [string]$SpoolRoot = "/data/faultmon/demo_stage2/spool"
  )

  $base = "$SpoolRoot/$DeviceId/$Name"
  if ($Status -eq "done") {
    $scriptBody = "rm -f $base.fail $base.ready; printf ok\\n > $base.done"
  } else {
    $scriptBody = "rm -f $base.done; printf host_upload_failed\\n > $base.fail"
  }
  $null = @(& hdc @HdcArgs shell $scriptBody 2>&1)
}

function Invoke-Stage2BoardBridgeOnce {
  param(
    [Parameter(Mandatory=$true)][string[]]$HdcArgs,
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][int]$ServerPort,
    [Parameter(Mandatory=$true)][string]$RepoDir,
    [string]$SpoolRoot = "/data/faultmon/demo_stage2/spool"
  )

  $localRoot = Get-Stage2BridgeLocalRoot
  Ensure-Stage2BridgeDir $localRoot
  $localSpool = Join-Path $localRoot "spool"
  Ensure-Stage2BridgeDir $localSpool

  $readyItems = Get-Stage2BoardReadyItems -HdcArgs $HdcArgs -SpoolRoot $SpoolRoot
  $results = @()

  foreach ($item in $readyItems) {
    $localDev = Join-Path $localSpool $item.device
    Ensure-Stage2BridgeDir $localDev

    $remoteBase = "$SpoolRoot/$($item.device)/$($item.name)"
    $remoteMeta = "$remoteBase.meta"
    $localFile = Join-Path $localDev $item.name
    $localMeta = $localFile + ".meta"

    $recvMeta = @(& hdc @HdcArgs file recv $remoteMeta $localMeta 2>&1)
    $recvMetaRc = $LASTEXITCODE
    $recvFile = @(& hdc @HdcArgs file recv $remoteBase $localFile 2>&1)
    $recvFileRc = $LASTEXITCODE
    if ($recvMetaRc -ne 0 -or $recvFileRc -ne 0 -or -not (Test-Path -LiteralPath $localMeta) -or -not (Test-Path -LiteralPath $localFile)) {
      Set-Stage2BoardMarker -HdcArgs $HdcArgs -DeviceId $item.device -Name $item.name -Status "fail" -SpoolRoot $SpoolRoot
      $results += [pscustomobject]@{ device = $item.device; name = $item.name; ok = $false; step = "recv"; msg = (($recvMeta + $recvFile) -join "`n").Trim() }
      continue
    }

    $meta = Read-Stage2MetaFile -Path $localMeta
    $deviceId = if ($meta.ContainsKey("DEVICE") -and $meta["DEVICE"]) { $meta["DEVICE"] } else { $item.device }
    $pub = Publish-Stage2InboxItem -LocalFile $localFile -DeviceId $deviceId -Server $Server -Port $ServerPort -RepoDir $RepoDir
    if ($pub.ok) {
      Set-Stage2BoardMarker -HdcArgs $HdcArgs -DeviceId $item.device -Name $item.name -Status "done" -SpoolRoot $SpoolRoot
      $results += [pscustomobject]@{ device = $deviceId; name = $item.name; ok = $true; step = "uploaded"; msg = $pub.msg }
    } else {
      Set-Stage2BoardMarker -HdcArgs $HdcArgs -DeviceId $item.device -Name $item.name -Status "fail" -SpoolRoot $SpoolRoot
      $results += [pscustomobject]@{ device = $deviceId; name = $item.name; ok = $false; step = "upload"; msg = $pub.msg }
    }
  }

  return $results
}
