#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Monitor and filter KaihongOS device logs via HDC hilog.

.PARAMETER Filter
    Regex pattern to filter logs (default: all)

.PARAMETER Level
    Log level filter: D(ebug), I(nfo), W(arn), E(rror), F(atal)

.PARAMETER Tag
    Filter by specific tag

.PARAMETER Output
    Save logs to file

.PARAMETER Duration
    Stop after N seconds (0 = indefinite)

.EXAMPLE
    .\hilog-monitor.ps1 -Filter "dsoftbus|rmw" -Level E
    .\hilog-monitor.ps1 -Tag "ROS2" -Output .\logs\ros2.log -Duration 60
#>

param(
    [string]$Filter = "",
    [ValidateSet("", "D", "I", "W", "E", "F")]
    [string]$Level = "",
    [string]$Tag = "",
    [string]$Output = "",
    [int]$Duration = 0
)

$ErrorActionPreference = "Stop"

# Build filter pattern
$patterns = @()
if ($Filter) { $patterns += $Filter }
if ($Level) { $patterns += "^\d{2}-\d{2}.*\s$Level/" }
if ($Tag) { $patterns += "\s$Tag\s" }

$combinedPattern = if ($patterns.Count -gt 0) {
    "(" + ($patterns -join "|") + ")"
} else {
    "."
}

Write-Host "[*] Starting hilog monitor..." -ForegroundColor Cyan
Write-Host "    Filter: $combinedPattern" -ForegroundColor Gray
if ($Output) {
    Write-Host "    Output: $Output" -ForegroundColor Gray
}
if ($Duration -gt 0) {
    Write-Host "    Duration: ${Duration}s" -ForegroundColor Gray
}
Write-Host "    Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host ""

$startTime = Get-Date
$lineCount = 0

try {
    hdc hilog | ForEach-Object {
        $line = $_
        
        # Check duration limit
        if ($Duration -gt 0) {
            $elapsed = ((Get-Date) - $startTime).TotalSeconds
            if ($elapsed -ge $Duration) {
                throw "Duration limit reached"
            }
        }
        
        # Apply filter
        if ($line -match $combinedPattern) {
            $lineCount++
            
            # Colorize output
            $color = switch -Regex ($line) {
                '\sF/' { 'Magenta' }
                '\sE/' { 'Red' }
                '\sW/' { 'Yellow' }
                '\sI/' { 'White' }
                '\sD/' { 'Gray' }
                default { 'White' }
            }
            
            Write-Host $line -ForegroundColor $color
            
            # Save to file if specified
            if ($Output) {
                $line | Out-File -FilePath $Output -Append -Encoding UTF8
            }
        }
    }
} catch {
    if ($_.Exception.Message -ne "Duration limit reached") {
        throw
    }
}

Write-Host ""
Write-Host "[+] Captured $lineCount lines" -ForegroundColor 