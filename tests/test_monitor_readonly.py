"""Twin Monitor read-only guarantee: no send-as-Principal path, no Odoo, no write to Principal's state."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from twin_agent import paths
from twin_agent.db import StateDB
from twin_agent.monitor import deliver
from twin_agent.monitor.readonly import ReadOnlyState, ReadOnlyViolation, connect_ro
from twin_agent.monitor.store import MonitorStore, RefusedPath

PKG = Path(__file__).resolve().parents[1] / "twin_agent" / "monitor"
FORBIDDEN_IMPORTS = re.compile(r"(^|\.)(sender|sender_approval|channels|bot_controller|spawner|flow|listener|"
                               r"telethon|telegram|xmlrpc|odoo\w*|odoorpc|creds_get)($|\.)")


def _imports(tree) -> set[str]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            base = ("." * n.level) + (n.module or "")
            out.add(base)
            out |= {f"{base}.{a.name}" for a in n.names}
    return out


def test_monitor_package_imports_no_send_or_odoo_path():
    for f in PKG.glob("*.py"):
        for name in _imports(ast.parse(f.read_text(encoding="utf-8"))):
            assert not FORBIDDEN_IMPORTS.search(name.lstrip(".")), f"{f.name} imports {name}"


SQL_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|CREATE|ALTER|ATTACH|VACUUM)\b", re.I)


def _sql_strings(tree) -> list[str]:
    """Every string literal in code (docstrings excluded), incl. the parts of f-strings."""
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def test_monitor_package_never_writes_twin_state():
    for f in PKG.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "StateDB" not in calls, f.name
        assert not calls & {"log_event", "send_message", "send_file", "update_request", "transition",
                            "claim_send", "finish_send", "set_dial", "set_cursor", "add_card"}, f.name
        if f.name != "store.py":                       # the monitor's OWN db is the only one it writes
            bad = [s for s in _sql_strings(tree) if SQL_WRITE.search(s)]
            assert not bad, f"{f.name}: {bad}"


def test_readonly_state_refuses_every_write(cfg, clock):
    StateDB(paths.state_db(), clock, cfg).close()
    ro = ReadOnlyState(paths.state_db())
    try:
        with pytest.raises(ReadOnlyViolation):
            ro._all("INSERT INTO events (ts, kind) VALUES ('x', 'y')")
        with pytest.raises(ReadOnlyViolation):
            ro.log_event("anything")
        with pytest.raises(Exception):
            ro.conn.execute("DELETE FROM events")
        with pytest.raises(Exception):
            ro.conn.execute("CREATE TABLE t (x)")
        assert ro.events() == []
    finally:
        ro.close()


def test_connect_ro_does_not_create_a_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        connect_ro(tmp_path / "nope.db")
    assert not (tmp_path / "nope.db").exists()


def test_monitor_store_refuses_state_db(tmp_home, tmp_path):
    with pytest.raises(RefusedPath):
        MonitorStore(paths.state_db())
    with pytest.raises(RefusedPath):
        MonitorStore(tmp_path / "elsewhere" / "state.db")


def test_delivery_is_bot_api_to_sysadmin_only(cfg):
    cfg["run_mode"] = "live"
    cfg.setdefault("monitor", {})["delivery"] = {"sysadmin_tg_user_id": cfg["people"]["principal_tg_user_id"]}
    with pytest.raises(deliver.MonitorRefused):
        deliver.make_sender(cfg, has_secret=lambda n: True)
    cfg["monitor"]["delivery"] = {"sysadmin_tg_user_id": 4242}
    posts = []
    s = deliver.make_sender(cfg, has_secret=lambda n: n == "monitor-bot-token", get_secret=lambda n: "TOKEN",
                            post=lambda url, **k: posts.append((url, k)) or type("R", (), {"status_code": 200})())
    assert s.send("[HIGH] REQ-0001: test alert")
    assert posts[0][0].endswith("/sendMessage") and posts[0][1]["json"]["chat_id"] == 4242
    assert "TOKEN" not in repr(s)


def test_delivery_refused_when_not_live(cfg):
    cfg.setdefault("monitor", {})["delivery"] = {"sysadmin_tg_user_id": 4242}
    with pytest.raises(deliver.MonitorRefused):
        deliver.make_sender(cfg, has_secret=lambda n: True)
