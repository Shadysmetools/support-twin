"""aa.py must work under python -I (06 runs every script that reads handover data that way)."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_aa_help_runs_isolated(tmp_path):
    # run from an unrelated folder, isolated mode: only aa.py's own repo path is added
    r = subprocess.run([sys.executable, "-I", str(REPO / "aa.py"), "--help"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "commands:" in r.stdout


def test_aa_reaches_package_code_isolated(tmp_path):
    """The package imports under -I from any folder (a stub main may still raise NotImplementedError)."""
    r = subprocess.run([sys.executable, "-I", str(REPO / "aa.py"), "creds", "list"], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr


def test_every_command_module_exists():
    sys.path.insert(0, str(REPO))
    import aa
    for name, mod in aa.COMMANDS.items():
        if mod.startswith("scripts."):
            continue      # OPS adds these
        path = REPO / (mod.replace(".", "/") + ".py")
        assert path.exists(), name
