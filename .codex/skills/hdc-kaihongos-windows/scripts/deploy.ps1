#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Deploy build artifacts to KaihongOS device via HDC.

.PARAMETER BuildDir
    Source build directory (default: .\build)

.PARAMETER DeviceDir
    Target directory on device (default: /data/local/tmp)

.PARAMETER Compress
    Enable compression during transfer

.PARAMETER Clean
    Clean target directory before deployment

.EXAMPLE
    .\deploy.ps1 -BuildDir .\out\rk3588s -DeviceDir /opt/ros2 -Compress -Clean
#>

param(
    [string]$BuildDir = ".\build",
    [string]$DeviceDir = "/data/local/tmp",
    [switch]$Compress,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

function Test-SafeDeviceDir {
    param(
        [string]$Path
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }

    if ($Path -ne $Path.Trim()) {
        return $false
    }

    if ($Path -match '[;&|`$<>]' -or $Path -match '\s') {
        return $false
    }

    if ($Path -match '\\' -or $Path -match '(^|/)\.(/|$)') {
        return $false
    }

    if ($Path -match '(^|/)\.\.(/|$)' -or $Path -match '[''\"()\[\]{}*?]') {
        return $false
    }

    $normalized = $Path
    while ($normalized.Length -gt 1 -and $normalized.EndsWith("/")) {
        $normalized = $normalized.Substring(0, $normalized.Length - 1)
    }

    $unsafeRoots = @("/", "/data", "/system", "/vendor", "/etc", "/bin", "/usr", "/tmp", "/data/local/tmp")
    if ($unsafeRoots -contains $normalized) {
        return $false
    }

    $safeProjectPrefixes = @(
        "/data/faultmon/",
        "/data/local/tmp/faultmon/",
        "/data/local/tmp/os_fault/",
        "/data/local/tmp/qwen3_os_fault/"
    )

    foreach ($prefix in $safeProjectPrefixes) {
        if ($normalized.StartsWith($prefix)) {
            return $true
        }
    }

    return $false
}

# Verify HDC connection
Write-Host "[*] Checking device connection..." -ForegroundColor Cyan
$devices = hdc list targets 2>&1
if ($devices -match "Empty" -or $LASTEXITCODE -ne 0) {
    Write-Error "No device connected. Run 'hdc list targets' to verify."
    exit 1
}
Write-Host "[+] Device connected: $devices" -ForegroundColor Green

# Verify build directory
if (-not (Test-Path $BuildDir)) {
    Write-Error "Build directory not found: $BuildDir"
    exit 1
}

# Clean if requested
if ($Clean) {
    if (-not (Test-SafeDeviceDir -Path $DeviceDir)) {
        Write-Error "Unsafe DeviceDir for -Clean: $DeviceDir" -ErrorAction Continue
        exit 1
    }
    Write-Host "[deploy] Cleaning safe device directory: $DeviceDir" -ForegroundColor Cyan
    hdc shell "rm -rf $DeviceDir/*" 2>&1 | Out-Null
}

# Ensure target exists
hdc shell "mkdir -p $DeviceDir" 2>&1 | Out-Null

# Build transfer options
$opts = @()
if ($Compress) { $opts += "-z" }

# Deploy
Write-Host "[*] Deploying $BuildDir -> $DeviceDir" -ForegroundColor Cyan
$startTime = Get-Date

if ($opts.Count -gt 0) {
    hdc file send $opts $BuildDir $DeviceDir
} else {
    hdc file send $BuildDir $DeviceDir
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Deployment failed"
    exit 1
}

$elapsed = (Get-Date) - $startTime
Write-Host "[+] Deployment complete in $($elapsed.TotalSeconds.ToString('F1'))s" -ForegroundColor Green

# Set permissions for executables
Write-Host "[*] Setting permissions..." -ForegroundColor Cyan
hdc shell "find $DeviceDir -type f -name '*.sh' -exec chmod 755 {} \;" 2>&1 | Out-Null
hdc shell "find $DeviceDir/bin -type f -exec chmod 755 {} \;" 2>&1 | Out-Null

Write-Host "[+] Done" -ForegroundColor Green
