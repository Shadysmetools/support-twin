"""Tests for scripts/install-windows-service.ps1 and the Task Scheduler XML (OPS files, built by guards-board-replay).

Nothing here registers a task or installs anything: the XML is parsed, the script is only parsed by the
PowerShell parser and run with -DryRun against a temp home.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
PS = shutil.which("powershell") or shutil.which("pwsh")


def filled_xml(name: str = "TwinAgent-Listener.xml") -> ET.Element:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    text = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", text)
    text = (text.replace("__USER__", r"PC\Principal").replace("__PYTHONW__", r"C:\TwinAgent\.venv\Scripts\pythonw.exe")
            .replace("__APP__", r"C:\Principal Agent\app"))
    assert "__" not in re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return ET.fromstring(text)


def test_listener_task_xml():
    root = filled_xml()
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == r"PC\Principal"
    assert root.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"   # only when logged on
    assert root.find("t:Settings/t:RestartOnFailure/t:Interval", NS).text == "PT1M"
    assert root.find("t:Settings/t:RestartOnFailure/t:Count", NS).text == "999"
    assert root.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    exe = root.find("t:Actions/t:Exec", NS)
    assert exe.find("t:Command", NS).text.startswith('"') and exe.find("t:Command", NS).text.endswith('pythonw.exe"')
    assert exe.find("t:Arguments", NS).text == r'-I "C:\Principal Agent\app\aa.py" listener --mode observe'   # quoted: path has spaces
    assert exe.find("t:WorkingDirectory", NS).text == r"C:\Principal Agent\app"


def test_watchdog_task_xml_and_script():
    root = filled_xml("TwinAgent-Watchdog.xml")
    assert root.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"
    assert root.find("t:Triggers/t:LogonTrigger/t:Repetition/t:Interval", NS).text == "PT5M"
    args = root.find("t:Actions/t:Exec/t:Arguments", NS).text
    assert r'-File "C:\Principal Agent\app\scripts\watchdog.ps1"' in args
    (SCRIPTS / "watchdog.ps1").read_text(encoding="ascii")


@pytest.mark.skipif(PS is None, reason="no PowerShell")
def test_watchdog_dry_run_on_missing_heartbeat(tmp_path):
    out = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "watchdog.ps1"),
                          "-AgentHome", str(tmp_path), "-DryRun"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "would restart" in out.stdout


def test_install_script_is_ascii():
    (SCRIPTS / "install-windows-service.ps1").read_text(encoding="ascii")    # PowerShell 5.1 reads BOM-less files as ANSI


@pytest.mark.skipif(PS is None, reason="no PowerShell")
def test_install_script_parses():
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{SCRIPTS / 'install-windows-service.ps1'}', [ref]$null, [ref]$e); $e.Count")
    out = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "0", out.stdout + out.stderr


@pytest.mark.skipif(PS is None or shutil.which("py") is None, reason="no PowerShell or py launcher")
def test_install_dry_run_changes_nothing(tmp_path):
    home = tmp_path / "Agent Home"
    out = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "install-windows-service.ps1"),
                          "-DryRun", "-AgentHome", str(home)],
                         capture_output=True, text=True, timeout=120)
    if "Python 3.12 is required" in out.stdout + out.stderr:
        pytest.skip("python on PATH is not 3.12")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[dry-run] Register task 'TwinAgent-Listener'" in out.stdout
    assert not home.exists()


@pytest.mark.skipif(PS is None or shutil.which("py") is None, reason="no PowerShell or py launcher")
def test_install_refreshes_worker_settings_but_keeps_config_yaml(tmp_path):
    """Review fix: a tightened worker deny list must reach a laptop that already installed; config.yaml is kept."""
    home = tmp_path / "Agent Home"
    (home / "config").mkdir(parents=True)
    for name in ("config.yaml", "worker-settings.json", "worker-mcp.json"):
        (home / "config" / name).write_text("old", encoding="utf-8")
    out = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "install-windows-service.ps1"),
                          "-DryRun", "-AgentHome", str(home)],
                         capture_output=True, text=True, timeout=120)
    if "Python 3.12 is required" in out.stdout + out.stderr:
        pytest.skip("python on PATH is not 3.12")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Keep existing" in out.stdout and "config.yaml" in out.stdout
    assert re.search(r"\[dry-run\] Merge '[^']*worker-settings\.json'", out.stdout)      # merged, not clobbered (#47)
    assert re.search(r"\[dry-run\] Refresh '[^']*worker-mcp\.json'", out.stdout)


def test_worker_settings_merge_preserves_user_deny(tmp_path):
    """#47: the merge keeps a user's extra deny rule AND refreshes the shipped denies."""
    import json, shutil
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        pytest.skip("no PowerShell")
    src = (SCRIPTS / "install-windows-service.ps1").read_text(encoding="utf-8")
    import re as _re
    m = _re.search(r"function Merge-WorkerSettings \{.*?\n\}", src, _re.S)
    assert m, "Merge-WorkerSettings not found in install.ps1"
    shipped = {"permissions": {"defaultMode": "default", "allow": ["Read"], "deny": ["Bash", "Read(**/vault/**)"]}}
    existing = {"permissions": {"allow": ["Read"], "deny": ["Read(C:/Users/DELL/**)"]}}   # user's own rule
    (tmp_path / "ship.json").write_text(json.dumps(shipped), encoding="utf-8")
    (tmp_path / "to.json").write_text(json.dumps(existing), encoding="utf-8")
    script = m.group(0) + f"\nMerge-WorkerSettings -From '{tmp_path/'ship.json'}' -To '{tmp_path/'to.json'}'\n"
    sp = tmp_path / "run.ps1"; sp.write_text(script, encoding="utf-8")
    r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(sp)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads((tmp_path / "to.json").read_text(encoding="utf-8-sig"))
    deny = out["permissions"]["deny"]
    assert "Read(C:/Users/DELL/**)" in deny          # user's rule survived
    assert "Bash" in deny and "Read(**/vault/**)" in deny   # shipped denies refreshed
