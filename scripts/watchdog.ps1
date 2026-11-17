<#
  Principal's Agent watchdog (06 H2). Task Scheduler runs this every 5 minutes.
  If the listener's heartbeat file is missing or older than -StaleMinutes, restart the TwinAgent-Listener task.
  It reads one timestamp line and nothing else: no secrets, no network.

    powershell -NoProfile -ExecutionPolicy Bypass -File "C:\TwinAgent\app\scripts\watchdog.ps1" [-DryRun]
#>
[CmdletBinding()]
param(
    [string]$AgentHome = $(if ($env:TWIN_AGENT_HOME) { $env:TWIN_AGENT_HOME } else { "C:\TwinAgent" }),
    [int]$StaleMinutes = 3,
    [string]$TaskName = "TwinAgent-Listener",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Beat = Join-Path $AgentHome "data\state\heartbeat"
$Log = Join-Path $AgentHome "data\logs\watchdog.log"

function Say([string]$Text) {
    $line = "{0} {1}" -f (Get-Date).ToUniversalTime().ToString("s"), $Text
    Write-Host $line
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
        Add-Content -LiteralPath $Log -Value $line -Encoding utf8
    }
}

$age = $null
if (Test-Path -LiteralPath $Beat) {
    $stamp = (Get-Content -LiteralPath $Beat -TotalCount 1).Trim()
    try {
        $last = [DateTimeOffset]::Parse($stamp, [Globalization.CultureInfo]::InvariantCulture)
        $age = ([DateTimeOffset]::UtcNow - $last).TotalMinutes
    } catch { $age = $null }
}
if ($null -ne $age -and $age -le $StaleMinutes) { exit 0 }

$why = if ($null -eq $age) { "no readable heartbeat" } else { "heartbeat {0:N1} min old" -f $age }
if ($DryRun) { Say "[dry-run] would restart '$TaskName' ($why)"; exit 0 }
Say "restarting '$TaskName' ($why)"
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName $TaskName
exit 0
