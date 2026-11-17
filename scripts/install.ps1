<#
  support-twin quickstart installer (Windows PowerShell 5.1+ or PowerShell 7).

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1           # core + tests
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -Live     # + real-integration SDKs

  Creates .venv, installs the package in editable mode, copies the example config into .\.twin and .env.
  Never overwrites a filled config. Never asks for or stores a secret.
  To run the listener as a logged-on Windows task instead, see scripts\install-windows-service.ps1.
#>
[CmdletBinding()]
param(
    [switch]$Live,
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @("-3.12")
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) { $PythonExe = "python"; $PythonArgs = @() }
$ok = & $PythonExe @PythonArgs -c "import sys; print(int(sys.version_info >= (3, 12)))"
if ($LASTEXITCODE -ne 0 -or "$ok".Trim() -ne "1") { throw "Python 3.12+ is required." }

Write-Host "== venv (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $PythonExe @PythonArgs -m venv .venv
    if ($LASTEXITCODE) { throw "venv failed" }
}
$VPy = Join-Path $Repo ".venv\Scripts\python.exe"

$extras = "test"
if ($Live) { $extras = "test,live" }
Write-Host "== pip install -e .[$extras]"
& $VPy -m pip install --quiet --upgrade pip
& $VPy -m pip install --quiet -e ".[$extras]"
if ($LASTEXITCODE) { throw "pip install failed" }

Write-Host "== config"
New-Item -ItemType Directory -Force -Path ".twin\config", ".twin\data" | Out-Null
if (Test-Path ".twin\config\config.yaml") { Write-Host "keep existing .twin\config\config.yaml" }
else { Copy-Item "config.example.yaml" ".twin\config\config.yaml" }
if (Test-Path ".env") { Write-Host "keep existing .env" } else { Copy-Item ".env.example" ".env" }

Write-Host "== guard self-test"
$env:TWIN_AGENT_HOME = Join-Path $Repo ".twin"
& $VPy -m twin_agent.cli guards-scan --selftest
if ($LASTEXITCODE) { throw "Guard self-test failed." }

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  support-twin demo                 # one synthetic ticket end to end, nothing leaves this machine"
Write-Host "  notepad .twin\config\config.yaml  # set your persona under 'profile:'"
Write-Host "  python -m pytest -q               # the test-suite"
