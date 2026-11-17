"""Credential vault (05 section 8, CONTRACT 9.2).

Values live in Windows Credential Manager (DPAPI, bound to Principal's Windows user) through `keyring`.
Values too big for Credential Manager (about 2.5 KB) go to a DPAPI-encrypted file in
%LOCALAPPDATA%\\TwinAgent\\vault\\ and keyring keeps only a marker.

Rules:
- There is NO command that prints a value. `run` puts values only into one child process's environment.
- `_get` is private: only child_env and real in-process adapters (Telethon, Bot API, Gmail) may call it.
- Every use writes one line to logs/vault-audit.jsonl (name, caller, request id, purpose, success). Never the value.

CLI (through aa.py):  creds set <name> | set-file <name> <path> | list | run --env VAR=name -- cmd ... | selftest
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from . import dpapi, paths
from .redact import redact, value_hash

VAULT_SERVICE = "twinagent"
NAMES = ["tg-api", "tg-user-session", "tg-bot-token", "google-oauth-client", "gmail-ticketmail-refresh",
         "gmail-globex-refresh", "odoo-client-read", "elevenlabs", "wiki", "deadman-url", "whatsapp-cloud",
         # 2026-11-03 Digital Twin: his own helpdesk.example.com API key (JSON {"login", "api_key"}), the browser
         # broker's work-site logins (JSON {"<site>": {"username", "password"}}) and an optional Bitwarden session.
         "odoo-sys-principal", "site-logins", "bitwarden-session",
         # 2026-11-03 Twin Monitor: Sysadmin's own bot token for monitor delivery (monitor/deliver.py default).
         "monitor-bot-token",
         # 2026-11-04 Cross-model refuter (17): Groq API key for the NON-Claude adversarial refuter
         # (checks/refuter_xmodel.py). Only the redacted claim + redacted evidence ever leave the machine.
         "groq",
         # 2026-11-12 Hub team bus: the Teamhub inbound-webhook URL (the URL is the credential; post-only, no client data).
         "teamhub-hub",
         # 2026-11-15 Hub ticket writer: Claude key for the English ticket text (hub/adapters_real.RealTicketWriter).
         "anthropic",
         # 2026-11-15 Hub per-person Odoo keys, JSON {"login", "api_key"} each (hub/odoo_people.py). Each Odoo action
         # runs AS the person who owns the task; "odoo-automation" is the intake/unassigned account.
         "odoo-principal", "odoo-supervisor", "odoo-riley", "odoo-automation"]
MAX_KEYRING_BYTES = 2500          # Credential Manager stores UTF-16; its blob limit is 2560 bytes
FILE_MARKER = "@dpapi-file"       # what keyring holds when the real value is in a vault file
SELFTEST_NAME = "selftest-probe"  # outside NAMES on purpose; deleted after the self-test

# Names whose value is two fields typed in two hidden prompts, stored as "<field1>:<field2>"
# (channels/tg_client.parse_tg_api reads tg-api as "<api_id>:<api_hash>").
MULTI_FIELD = {"tg-api": ("api_id", "api_hash")}


class VaultError(RuntimeError):
    """A vault problem. The message never contains a value."""


# ---------------------------------------------------------------- backend and names

def _backend(backend: Any = None) -> Any:
    """The keyring backend: the one passed in (tests: FakeKeyring) or the real keyring module (lazy import)."""
    if backend is not None:
        return backend
    try:
        import keyring  # lazy: tests and other modules import creds without keyring installed
    except ImportError as e:
        raise VaultError("the 'keyring' package is not installed in this Python (pip install keyring)") from e
    return keyring


def normalize_name(name: str) -> str:
    """Accept 'tg-api' or 'twinagent/tg-api' (the 06 run-sheet form). Unknown names are refused."""
    name = (name or "").strip()
    if name.startswith(VAULT_SERVICE + "/"):
        name = name[len(VAULT_SERVICE) + 1:]
    if name not in NAMES and name != SELFTEST_NAME:
        raise VaultError(f"unknown vault name {name!r}; known: {', '.join(NAMES)}")
    return name


def _file_path(name: str) -> Path:
    return paths.vault_file_dir() / f"{name}.bin"


# ---------------------------------------------------------------- audit

def _audit(event: str, name: str, *, caller: str, purpose: str = "", request_id: str | None = None,
           ok: bool = True, detail: str = "") -> None:
    """One line in vault-audit.jsonl. Never the value. Audit failure never leaks; it is reported to stderr."""
    try:
        from .audit import AuditLog
        from .clock import SystemClock
        log = AuditLog(paths.logs_dir() / "vault-audit.jsonl", SystemClock())
        log.append({"event": event, "name": name, "caller": caller, "purpose": purpose,
                    "request_id": request_id, "success": ok, "detail": detail})
    except Exception as e:  # noqa: BLE001  (the vault must still work if the log folder is missing)
        print(f"vault audit write failed: {type(e).__name__}", file=sys.stderr)


# ---------------------------------------------------------------- public API

def set_secret(name: str, value: str, *, backend: Any = None, caller: str = "cli") -> None:
    """Store a value. Over about 2.5 KB it goes to a DPAPI file and keyring keeps a marker."""
    name = normalize_name(name)
    if not value:
        raise VaultError("empty value refused")
    kr = _backend(backend)
    try:
        if len(value.encode("utf-16-le")) > MAX_KEYRING_BYTES:
            dpapi.write_file(_file_path(name), value.encode("utf-8"))
            kr.set_password(VAULT_SERVICE, name, FILE_MARKER)
        else:
            kr.set_password(VAULT_SERVICE, name, value)
            _file_path(name).unlink(missing_ok=True)   # an older big value must not linger
        ok = _get(name, backend=kr, _audit_it=False) == value
    except VaultError:
        _audit("set", name, caller=caller, ok=False)
        raise
    except Exception as e:  # noqa: BLE001
        _audit("set", name, caller=caller, ok=False, detail=type(e).__name__)
        raise VaultError(f"could not store {name}: {type(e).__name__}") from None
    _audit("set", name, caller=caller, ok=ok)
    if not ok:
        raise VaultError(f"stored {name} but read-back did not match")


def has_secret(name: str, *, backend: Any = None) -> bool:
    name = normalize_name(name)
    stored = _backend(backend).get_password(VAULT_SERVICE, name)
    if stored == FILE_MARKER:
        return _file_path(name).exists()
    return bool(stored)


def list_names(*, backend: Any = None) -> list[str]:
    """Names only, never values. Reads nothing into the log."""
    return [n for n in NAMES if has_secret(n, backend=backend)]


def delete_secret(name: str, *, backend: Any = None, caller: str = "cli") -> None:
    name = normalize_name(name)
    kr = _backend(backend)
    try:
        kr.delete_password(VAULT_SERVICE, name)
    except Exception:  # noqa: BLE001  (keyring raises if missing; deleting a missing name is fine)
        pass
    _file_path(name).unlink(missing_ok=True)
    _audit("delete", name, caller=caller)


def _get(name: str, *, backend: Any = None, caller: str = "adapter", purpose: str = "adapter",
         request_id: str | None = None, _audit_it: bool = True) -> str:
    """Private. Only child_env and real in-process adapters may call this. Never print the result."""
    name = normalize_name(name)
    try:
        stored = _backend(backend).get_password(VAULT_SERVICE, name)
        if stored == FILE_MARKER:
            stored = dpapi.read_file(_file_path(name)).decode("utf-8")
        if not stored:
            raise VaultError(f"{name} is not in the vault")
    except Exception as e:
        if _audit_it:
            _audit("read", name, caller=caller, purpose=purpose, request_id=request_id, ok=False,
                   detail=type(e).__name__)
        if isinstance(e, VaultError):
            raise
        raise VaultError(f"could not read {name}: {type(e).__name__}") from None
    if _audit_it:
        _audit("read", name, caller=caller, purpose=purpose, request_id=request_id, ok=True)
    return stored


def child_env(mapping: dict[str, str], *, purpose: str, request_id: str | None = None,
              backend: Any = None) -> dict[str, str]:
    """{"ENV_VAR": "vault-name"} -> a copy of os.environ plus those values. The ONLY way a value leaves.

    The returned dict goes straight to subprocess(env=...). Never log it, print it or keep it.
    """
    env = dict(os.environ)
    for var, name in mapping.items():
        if not var or not var.replace("_", "").isalnum():
            raise VaultError(f"bad environment variable name {var!r}")
        env[var] = _get(name, backend=backend, caller="child_env", purpose=purpose, request_id=request_id)
    return env


def run(mapping: dict[str, str], cmd: list[str], *, purpose: str, request_id: str | None = None,
        backend: Any = None) -> int:
    """Run cmd (an argument list, never a shell string) with the values in its environment only."""
    if not cmd or isinstance(cmd, str):
        raise VaultError("cmd must be a non-empty argument list")
    env = child_env(mapping, purpose=purpose, request_id=request_id, backend=backend)
    return subprocess.run(list(cmd), env=env, shell=False).returncode


def vault_hashes(*, backend: Any = None) -> set[str]:
    """sha256 of every stored value (and each field of multi-field values), for exact-leak redaction.

    Guards, audit and log writers pass this to redact.redact(). Hashes only; values are dropped at once.
    One audit line per call (purpose "vault_hashes").
    """
    out: set[str] = set()
    for name in list_names(backend=backend):
        try:
            value = _get(name, backend=backend, caller="vault_hashes", purpose="leak scan", _audit_it=False)
        except VaultError:
            continue
        out.add(value_hash(value))
        for part in _fields(value) + value.split(":"):
            if len(part) >= 6:
                out.add(value_hash(part))
    _audit("hashes", "*", caller="vault_hashes", purpose="leak scan", detail=str(len(out)))
    return out


def redact_values(text: str, *, hashes: Iterable[str] | None = None, backend: Any = None) -> str:
    """Redaction helper: regex patterns plus exact vault values. Use on anything bound for a log or card."""
    return redact(text, vault_hashes(backend=backend) if hashes is None else hashes)


def _fields(value: str) -> list[str]:
    """String fields inside a JSON value (tg-api, google-oauth-client); [] for plain values."""
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return []
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, (str, int)) and not isinstance(v, bool):
            found.append(str(v))
    walk(data)
    return found


def get_fields(name: str, *, backend: Any = None, caller: str = "adapter", purpose: str = "adapter") -> dict:
    """For in-process adapters: a JSON value as a dict (e.g. tg-api -> {"api_id", "api_hash"}). Audited."""
    return json.loads(_get(name, backend=backend, caller=caller, purpose=purpose))


# ---------------------------------------------------------------- self-test

# Rules the worker settings must carry (review: shell, every write tool, and the private folders).
REQUIRED_WORKER_DENY = ("Bash", "PowerShell", "Write", "Edit", "NotebookEdit", "Read(**/data/state/**)",
                        "Read(~/.claude/**)", "Read(~/AppData/**)")


def _worker_settings_deny_ok() -> bool:
    """The worker settings deny shell access, so a worker cannot reach the vault (05 section 8).

    Parses the file strictly as JSON (a file Claude cannot parse protects nothing) and requires Bash and
    PowerShell, every write tool and the private-folder Read rules in permissions.deny. The real hook on Principal's own Claude Code is checked by hand (06 E5 step 7).
    """
    candidates = [paths.config_dir() / "worker-settings.json",
                  Path(__file__).parent / "worker" / "worker-settings.json"]
    for p in candidates:
        if p.exists():
            try:
                deny = json.loads(p.read_text(encoding="utf-8")).get("permissions", {}).get("deny", [])
            except (ValueError, AttributeError):
                return False
            return all(rule in deny for rule in REQUIRED_WORKER_DENY)
    return False


def selftest(*, backend: Any = None) -> list[tuple[str, bool]]:
    """06 E5 step 7: set OK, run-into-child OK, audit line written, hash chain OK, deny rules present."""
    from .audit import AuditLog
    from .clock import SystemClock

    results: list[tuple[str, bool]] = []
    probe = "probe-" + os.urandom(12).hex()
    log = AuditLog(paths.logs_dir() / "vault-audit.jsonl", SystemClock())
    before = log.count()
    try:
        set_secret(SELFTEST_NAME, probe, backend=backend, caller="selftest")
        results.append(("set + read back", has_secret(SELFTEST_NAME, backend=backend)))
    except Exception:  # noqa: BLE001
        results.append(("set + read back", False))
    try:
        code = ("import os,sys,hashlib; v=os.environ.get('AA_PROBE',''); "
                f"sys.exit(0 if hashlib.sha256(v.encode()).hexdigest()=='{value_hash(probe)}' else 3)")
        rc = run({"AA_PROBE": SELFTEST_NAME}, [sys.executable, "-I", "-c", code], purpose="selftest",
                 backend=backend)
        results.append(("value reaches child env only", rc == 0 and "AA_PROBE" not in os.environ))
    except Exception:  # noqa: BLE001
        results.append(("value reaches child env only", False))
    big = "x" * 3000
    try:
        set_secret(SELFTEST_NAME, big, backend=backend, caller="selftest")
        results.append(("big value via DPAPI file", _get(SELFTEST_NAME, backend=backend, caller="selftest",
                                                         purpose="selftest") == big))
    except Exception:  # noqa: BLE001
        results.append(("big value via DPAPI file", False))
    delete_secret(SELFTEST_NAME, backend=backend, caller="selftest")
    results.append(("audit lines written", log.count() > before))
    ok, _bad = log.verify()
    results.append(("audit hash chain OK", ok))
    log_text = (paths.logs_dir() / "vault-audit.jsonl").read_text(encoding="utf-8") if log.path.exists() else ""
    results.append(("no value in audit log", probe not in log_text))
    results.append(("worker deny rules present (check the hook by hand too)", _worker_settings_deny_ok()))
    return results


# ---------------------------------------------------------------- CLI

def _prompt_value(name: str) -> str:
    """Hidden prompt(s). tg-api asks api_id then api_hash and stores "<api_id>:<api_hash>"."""
    if name in MULTI_FIELD:
        parts = {}
        for field in MULTI_FIELD[name]:
            parts[field] = getpass.getpass(f"{name} {field} (hidden): ").strip()
            if not parts[field]:
                raise VaultError(f"empty {field}")
        return ":".join(parts[f] for f in MULTI_FIELD[name])
    first = getpass.getpass(f"{name} value (hidden): ").strip()
    second = getpass.getpass("type it again (hidden): ").strip()
    if first != second:
        raise VaultError("the two entries do not match; nothing stored")
    return first


def _parse_env(items: list[str]) -> dict[str, str]:
    mapping = {}
    for item in items:
        var, sep, name = item.partition("=")
        if not sep:
            raise VaultError(f"--env needs VAR=name, got {item!r}")
        mapping[var.strip()] = normalize_name(name)
    return mapping


def main(argv: list[str] | None = None, *, backend: Any = None) -> int:
    """CLI: set | set-file | list | run --env VAR=name -- cmd | selftest. There is no 'get'."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd_after: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd_after = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(prog="aa.py creds", description="Principal's Agent vault. Never prints a value.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="store a value typed in a hidden prompt")
    s.add_argument("name")
    sf = sub.add_parser("set-file", help="store a file's content, then delete the file")
    sf.add_argument("name")
    sf.add_argument("path")
    sf.add_argument("--keep", action="store_true", help="do not delete the source file")
    sub.add_parser("list", help="names only")
    r = sub.add_parser("run", help="run a command with values in its environment only: run --env VAR=name -- cmd")
    r.add_argument("--env", action="append", default=[], required=True)
    r.add_argument("--purpose", default="cli run")
    sub.add_parser("selftest")
    sub.add_parser("test", help="same as selftest")
    args = p.parse_args(argv)
    try:
        if args.cmd == "set":
            name = normalize_name(args.name)
            set_secret(name, _prompt_value(name), backend=backend, caller="cli")
            print(f"stored {name}")
        elif args.cmd == "set-file":
            name = normalize_name(args.name)
            src = Path(args.path)
            if not src.is_file():
                raise VaultError(f"no such file: {src}")
            set_secret(name, src.read_text(encoding="utf-8").strip(), backend=backend, caller="cli set-file")
            if not args.keep:
                src.unlink()
                print(f"stored {name}; deleted {src.name}")
            else:
                print(f"stored {name}; delete {src.name} yourself")
        elif args.cmd == "list":
            names = list_names(backend=backend)
            for n in NAMES:
                print(f"{'set    ' if n in names else 'missing'}  {n}")
        elif args.cmd == "run":
            if not cmd_after:
                raise VaultError("nothing to run: put the command after --")
            return run(_parse_env(args.env), cmd_after, purpose=args.purpose, backend=backend)
        else:
            results = selftest(backend=backend)
            for label, ok in results:
                print(f"{'OK  ' if ok else 'FAIL'}  {label}")
            return 0 if all(ok for _, ok in results) else 1
    except VaultError as e:
        print(f"creds: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
