<#
  Principal's Agent v0 installer (06 E1, E2, E6, H1, I1). Run in PowerShell as Principal (not as admin):

    powershell -NoProfile -ExecutionPolicy Bypass -File "C:\TwinAgent\app\scripts\install.ps1"            # install
    powershell -NoProfile -ExecutionPolicy Bypass -File "C:\TwinAgent\app\scripts\install.ps1" -DryRun    # show only

  What it does: checks Python 3.12, makes the folders, creates the venv, installs requirements, copies the
  example config (never overwrites a filled one), runs the guard self-test, and registers the
  "TwinAgent-Listener" task (At log on, run only when logged on, restart every 1 min on failure).
  It never asks for, prints or stores a secret. Secrets go in with: python -I aa.py creds set <name>
  Default stays OBSERVE: run_mode dry_run, send_enabled false.
#>
[CmdletBinding()]
param(
    [string]$AgentHome = "C:\TwinAgent",
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @("-3.12"),
    [switch]$DryRun,
    [switch]$NoTask,
    [switch]$NoPip
)

$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $PSScriptRoot          # the repo folder (= C:\TwinAgent\app on the laptop)
$Venv = Join-Path $AgentHome ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvPythonW = Join-Path $Venv "Scripts\pythonw.exe"
$Config = Join-Path $AgentHome "config"

function Step([string]$What, [scriptblock]$Do) {
    if ($DryRun) { Write-Host "[dry-run] $What" } else { Write-Host "== $What"; & $Do }
}

function Xml-Escape([string]$s) { return [System.Security.SecurityElement]::Escape($s) }

# ---------------------------------------------------------------- 0. checks (read only)
Write-Host "Principal's Agent installer. Home: '$AgentHome'  App: '$App'"
if ($AgentHome -like "*Claude Code Platform*") {
    throw "Home must be outside Principal's platform folder (it auto-commits every 2 hours). Use C:\TwinAgent."
}
if ((Resolve-Path -LiteralPath $App).Path -ne (Join-Path $AgentHome "app")) {
    Write-Warning "The app is not at '$AgentHome\app'. Fine for a test, but the task will point at '$App'."
}
$ver = & $PythonExe @PythonArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0 -or $ver.Trim() -ne "3.12") { throw "Python 3.12 is required (found '$ver'). Install it from python.org." }
Write-Host "Python $ver found."

# ---------------------------------------------------------------- 1. folders (06 E1, CONTRACT 1.2)
$dirs = @("config", "claude-config", "data\state", "data\kb\persona", "data\kb\lanes", "data\work", "data\logs",
          "data\proof", "data\replay")
Step "Create folders under '$AgentHome'" {
    foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path (Join-Path $AgentHome $d) | Out-Null }
}

# ---------------------------------------------------------------- 2. venv + requirements (06 E2)
Step "Create the venv at '$Venv'" {
    if (-not (Test-Path -LiteralPath $VenvPython)) { & $PythonExe @PythonArgs -m venv "$Venv"; if ($LASTEXITCODE) { throw "venv failed" } }
}
if (-not $NoPip) {
    Step "pip install -r requirements.txt" {
        & "$VenvPython" -m pip install --upgrade pip
        & "$VenvPython" -m pip install -r (Join-Path $App "requirements.txt")
        if ($LASTEXITCODE) { throw "pip install failed" }
    }
}

# ---------------------------------------------------------------- 3. config copies (never overwrite)
$copies = @(
    @{ From = (Join-Path $App "config.example.yaml"); To = (Join-Path $Config "config.yaml") },
    @{ From = (Join-Path $App "twin_agent\worker\worker-settings.json"); To = (Join-Path $Config "worker-settings.json") },
    @{ From = (Join-Path $App "twin_agent\worker\worker-mcp.json"); To = (Join-Path $Config "worker-mcp.json") }
)
function Merge-WorkerSettings {
    param([string]$From, [string]$To)
    # Base = the SHIPPED file, so a tightened deny list always reaches an old install; then ADD any extra
    # deny/allow entries the user added to their own copy, so a personal rule (e.g. Read of a private folder)
    # survives a re-install (issue #47). If the existing file is missing or not valid JSON, fall back to shipped.
    $ship = Get-Content -LiteralPath $From -Raw | ConvertFrom-Json
    if (Test-Path -LiteralPath $To) {
        try {
            $cur = Get-Content -LiteralPath $To -Raw | ConvertFrom-Json
            foreach ($key in 'deny','allow') {
                $merged = @(@($ship.permissions.$key) + @($cur.permissions.$key) | Where-Object { $_ } | Select-Object -Unique)
                $ship.permissions.$key = $merged
            }
        } catch { }   # existing file not valid JSON: keep the shipped file as-is
    }
    [System.IO.File]::WriteAllText($To, ($ship | ConvertTo-Json -Depth 10), (New-Object System.Text.UTF8Encoding $false))
}

foreach ($c in $copies) {
    $from = $c.From; $to = $c.To
    # worker-mcp.json is pure code: always refresh. worker-settings.json is refreshed too, but MERGED so a user's
    # own extra deny rule survives (issue #47). Only config.yaml is never touched.
    $isSettings = $to -like "*worker-settings.json"
    $isCode = $to -like "*worker-*.json"
    if ($isSettings) { Step "Merge '$to' from '$from'" { Merge-WorkerSettings -From $from -To $to } }
    elseif ($isCode) { Step "Refresh '$to' from '$from'" { Copy-Item -LiteralPath $from -Destination $to -Force } }
    elseif (Test-Path -LiteralPath $to) { Write-Host "Keep existing '$to'" }
    else { Step "Copy '$from' to '$to'" { Copy-Item -LiteralPath $from -Destination $to } }
}

# ---------------------------------------------------------------- 4. guard self-test (06 I4 gate)
Step "Guard self-test" {
    $env:TWIN_AGENT_HOME = $AgentHome
    & "$VenvPython" -I (Join-Path $App "aa.py") guards-scan --selftest
    if ($LASTEXITCODE) { throw "Guard self-test failed: do not switch send_enabled on." }
}

# ---------------------------------------------------------------- 5. Task Scheduler (06 H1)
function Register-AgentTask([string]$Name, [string]$XmlFile) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $xml = Get-Content -LiteralPath $XmlFile -Raw
    $xml = $xml -replace '^\s*<\?xml[^>]*\?>\s*', ''
    $xml = $xml.Replace("__USER__", (Xml-Escape $user)).Replace("__PYTHONW__", (Xml-Escape $VenvPythonW)).Replace("__APP__", (Xml-Escape $App))
    if ($xml -match "__[A-Z]+__") { throw "Unfilled placeholder in '$XmlFile'" }
    Step "Register task '$Name' for '$user'" { Register-ScheduledTask -TaskName $Name -Xml $xml -Force | Out-Null }
}
if (-not $NoTask) {
    Register-AgentTask "TwinAgent-Listener" (Join-Path $PSScriptRoot "TwinAgent-Listener.xml")
    $wd = Join-Path $PSScriptRoot "TwinAgent-Watchdog.xml"
    if (Test-Path -LiteralPath $wd) { Register-AgentTask "TwinAgent-Watchdog" $wd }
    else { Write-Warning "No TwinAgent-Watchdog.xml yet: H2 watchdog not registered." }
}

# ---------------------------------------------------------------- 6. manual steps left (printed, not done)
Write-Host ""
Write-Host "Done. Still by hand (06 H3, H4, F2, E5):"
Write-Host " - powercfg /change standby-timeout-ac 0 ; powercfg /change hibernate-timeout-ac 0 ; lid close on AC = Do nothing"
Write-Host " - Sign-in options: 'Use my sign-in info to automatically finish setting up after an update' = On"
Write-Host " - Odoo x Globex group: turn OFF 'Remain anonymous' for Principal's account"
Write-Host " - Secrets: python -I aa.py creds set <name>  (names only are ever shown)"
Write-Host " - config\config.yaml stays run_mode: dry_run and send_enabled: false until D1 + self-test (06 I4)"
