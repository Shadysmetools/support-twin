<#
.SYNOPSIS
  Start the dedicated "Digital Twin" Chrome profile with remote debugging on 127.0.0.1 only (CDP port 9334).

.DESCRIPTION
  - Its own user-data-dir (default C:\TwinAgent\browser\DigitalTwin), never Principal's normal Chrome profile.
    Chrome 136+ refuses remote debugging on the default profile anyway; this script refuses it too.
  - Principal logs in ONCE to his work sites inside this window. Cookies stay in Chrome's own encrypted store (DPAPI,
    his Windows user). Nothing here exports cookies or passwords, and nothing should.
  - Agents, QA (ShipShape / Playwright connect_over_cdp) and admin attach at http://127.0.0.1:9334 and open NEW tabs
    only (twin_agent\browser\attach.py).
  - The folder is locked to Principal's Windows user (inheritance removed, full control for him only).
  - Safe to run twice: if the profile already answers on the port, it says so and exits 0.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "C:\TwinAgent\app\scripts\digital-twin-browser.ps1" -DryRun
#>
param(
    [int]$Port = 9334,
    [string]$AgentHome = "C:\TwinAgent",
    [string]$UserDataDir = "",
    [string]$Url = "about:blank",
    [string]$Chrome = "",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

if (-not $UserDataDir) { $UserDataDir = Join-Path $AgentHome "browser\DigitalTwin" }
$defaultProfile = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
if ([IO.Path]::GetFullPath($UserDataDir).TrimEnd('\') -ieq [IO.Path]::GetFullPath($defaultProfile).TrimEnd('\')) {
    Write-Output "REFUSED: that is Principal's normal Chrome profile. Use a dedicated folder."
    exit 2
}
if ($Port -lt 1024 -or $Port -gt 65535) { Write-Output "REFUSED: bad port $Port"; exit 2 }

if (-not $Chrome) {
    $candidates = @(
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { $Chrome = $c; break } }
}

# Already running? (the endpoint answers only on loopback)
try {
    $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
    Write-Output "Digital Twin browser already running on 127.0.0.1:$Port ($($v.Browser))."
    exit 0
} catch { }

$chromeArgs = @(
    "--user-data-dir=`"$UserDataDir`"",
    "--remote-debugging-port=$Port",
    "--remote-debugging-address=127.0.0.1",
    "--no-first-run",
    "--no-default-browser-check",
    "--profile-directory=Default",
    $Url
)

if ($DryRun) {
    Write-Output "DRY RUN (nothing started, nothing created)"
    Write-Output "chrome: $(if ($Chrome) { $Chrome } else { 'NOT FOUND' })"
    Write-Output "profile: $UserDataDir"
    Write-Output "would lock the folder to: $env:USERDOMAIN\$env:USERNAME"
    Write-Output ("args: " + ($chromeArgs -join " "))
    Write-Output "attach: http://127.0.0.1:$Port (CDP, loopback only)"
    exit 0
}
if (-not $Chrome) { Write-Output "Chrome not found. Pass -Chrome <path to chrome.exe>."; exit 2 }

if (-not (Test-Path -LiteralPath $UserDataDir)) {
    New-Item -ItemType Directory -Force -Path $UserDataDir | Out-Null
}
& icacls "$UserDataDir" /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

Start-Process -FilePath $Chrome -ArgumentList $chromeArgs | Out-Null
Start-Sleep -Seconds 2
try {
    $v = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 5
    Write-Output "Digital Twin browser started: $($v.Browser) on 127.0.0.1:$Port"
} catch {
    Write-Output "Chrome started, but 127.0.0.1:$Port does not answer yet. Close other windows of this profile and retry."
}
Write-Output "Log in to the work sites ONCE in this window. Never export cookies or passwords."
