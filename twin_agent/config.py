"""Load and validate config.yaml (plus optional split files). CONTRACT section 9.1.

How loading works:
1. Start from the repo's config.example.yaml (safe defaults: dry_run, send off, every lane observe/off).
2. Deep-merge the user's config.yaml on top (dicts merge key by key, lists replace).
3. Deep-merge optional split files from the same folder: chats.yaml, timers.yaml, dial.yaml, holidays.yaml,
   consent.yaml. Each one fills the top-level key named after the file. The file may hold the value directly
   or wrapped in that key (e.g. dial.yaml may be "L1_holding: observe" or "dial: {L1_holding: observe}").
4. Coerce YAML booleans in dials ("off" typed bare becomes False in YAML) and validate.
"""
from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from . import paths
from .models import Channel, Dial, Lane

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
SPLIT_FILES = ("chats", "timers", "dial", "holidays", "consent")
TEAM_BUSES = ("telegram", "teamhub")   # the hub's team channel; client-send approvals stay on Telegram either way
RUN_MODES = ("dry_run", "live")

# Which consent switch covers which channel.
_CONSENT_KEY = {
    Channel.telegram_group: "telegram",
    Channel.telegram_dm: "telegram",
    Channel.bot: "telegram",
    Channel.email: "email",
    Channel.ticket_mail: "tickets",
}


class ConfigError(ValueError):
    """The config has errors; the message lists them."""


def _read_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    return yaml.safe_load(text)


def _deep_merge(base: Any, over: Any) -> Any:
    """Merge `over` into a copy of `base`. Dicts merge per key; anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def _defaults() -> dict:
    if EXAMPLE.exists():
        data = _read_yaml(EXAMPLE)
        if isinstance(data, dict):
            return data
    return {"run_mode": "dry_run", "send_enabled": False, "dial": {}}


def _coerce_dial_value(v: Any) -> Any:
    """YAML turns bare off/no into False and on/yes into True. Map them back to text."""
    if v is False or v is None:
        return "off"
    if v is True:
        return "on"   # not a valid Dial: validate_config reports it, on purpose
    return str(v).strip()


def load_config(path: Path | None = None) -> dict:
    """Load config.yaml (default: <home>/config/config.yaml), merge split files and defaults, validate."""
    path = Path(path) if path else paths.config_dir() / "config.yaml"
    cfg = _defaults()
    if path.exists():
        user = _read_yaml(path) or {}
        if not isinstance(user, dict):
            raise ConfigError(f"{path.name}: top level must be a mapping")
        cfg = _deep_merge(cfg, user)
    elif path != paths.config_dir() / "config.yaml":
        raise FileNotFoundError(str(path))
    for stem in SPLIT_FILES:
        split = path.parent / f"{stem}.yaml"
        if not split.exists():
            continue
        data = _read_yaml(split)
        if isinstance(data, dict) and set(data) == {stem}:
            data = data[stem]
        if data is None:
            continue
        cfg[stem] = _deep_merge(cfg.get(stem), data) if isinstance(cfg.get(stem), dict) else data
    cfg["dial"] = {str(k): _coerce_dial_value(v) for k, v in (cfg.get("dial") or {}).items()}
    errors = validate_config(cfg)
    if errors:
        raise ConfigError("config errors:\n- " + "\n- ".join(errors))
    return cfg


def owner_ids(cfg: dict) -> set[int]:
    """The Telegram id(s) that OWN this install's board (may approve sends / toggle autopilot). Per-install, read
    from `people.principal_tg_user_id` - NEVER a hard-coded id. Fail closed: an unset / 0 / non-numeric id yields an
    empty set, so an unconfigured bot grants no one owner powers and each installer owns their own board (S3)."""
    people = (cfg or {}).get("people") or {}
    try:
        oid = int(people.get("principal_tg_user_id") or 0)
    except (TypeError, ValueError):
        return set()
    return {oid} if oid > 0 else set()


def validate_config(cfg: dict) -> list[str]:
    """Return human-readable errors ([] = fine). Placeholder ids (0) are errors only in live mode."""
    errs: list[str] = []
    if cfg.get("run_mode") not in RUN_MODES:
        errs.append(f"run_mode must be one of {RUN_MODES}, got {cfg.get('run_mode')!r}")
    if cfg.get("team_bus", "telegram") not in TEAM_BUSES:
        errs.append(f"team_bus must be one of {TEAM_BUSES}, got {cfg.get('team_bus')!r}")
    if not isinstance(cfg.get("send_enabled"), bool):
        errs.append("send_enabled must be true or false")
    lanes = {lane.value for lane in Lane}
    for k, v in (cfg.get("dial") or {}).items():
        if k not in lanes:
            errs.append(f"dial: unknown lane {k!r}")
        try:
            Dial(_coerce_dial_value(v))
        except ValueError:
            errs.append(f"dial.{k}: {v!r} is not one of off|observe|draft|auto_on_timeout|auto")
    chats = cfg.get("chats") or []
    if not isinstance(chats, list):
        errs.append("chats must be a list")
        chats = []
    for i, c in enumerate(chats):
        if not isinstance(c, dict) or "chat_id" not in c:
            errs.append(f"chats[{i}] needs a chat_id")
            continue
        if c.get("company", "EG") not in ("EG", "KSA", "UNKNOWN"):
            errs.append(f"chats[{i}].company must be EG or KSA")
    try:
        holidays(cfg)
    except (TypeError, ValueError) as e:
        errs.append(f"holidays: {e}")
    if cfg.get("run_mode") == "live":
        people = cfg.get("people") or {}
        if not people.get("principal_tg_user_id"):
            errs.append("people.principal_tg_user_id must be set in live mode")
        if not any(isinstance(c, dict) and c.get("chat_id") and c.get("read", True) for c in chats):
            errs.append("live mode needs at least one chat with a real chat_id and read: true")
    return errs


def _same_id(a: Any, b: Any) -> bool:
    if a in (None, "", 0, "0") or b in (None, ""):
        return False
    return str(a).strip() == str(b).strip()


def allowed_chat(cfg: dict, chat_id, topic_id=None) -> dict | None:
    """The chats[] entry for this chat (and topic), or None if it is not on the allow-list.

    A chat with read: false is not allowed. If the entry lists topics with real ids, only those topics (and
    topic_id None = the chat itself) are allowed, unless the entry says all_topics: true.
    """
    for c in cfg.get("chats") or []:
        if not isinstance(c, dict) or not _same_id(c.get("chat_id"), chat_id):
            continue
        if c.get("read", True) is False:
            return None
        topic_ids = [t.get("id") for t in (c.get("topics") or []) if isinstance(t, dict) and t.get("id")]
        if topic_id is not None and topic_ids and not c.get("all_topics"):
            if not any(_same_id(t, topic_id) for t in topic_ids):
                return None
        return c
    return None


def chat_flag(cfg: dict, chat_id, key: str, default: bool) -> bool:
    """A per-chat switch from chats[] (holding, auto_ticket, ...). Not set, or chat not listed = default."""
    for c in cfg.get("chats") or []:
        if isinstance(c, dict) and _same_id(c.get("chat_id"), chat_id) and key in c:
            return c.get(key) is True
    return default


def topic_entry(cfg: dict, chat_id, topic_id) -> dict | None:
    """The topics[] entry for this topic, if configured (used for the company hint)."""
    chat = allowed_chat(cfg, chat_id, topic_id)
    if not chat or topic_id is None:
        return None
    for t in chat.get("topics") or []:
        if isinstance(t, dict) and _same_id(t.get("id"), topic_id):
            return t
    return None


def lane_dial(cfg: dict, lane: Lane) -> Dial:
    """The configured dial for a lane. Missing or bad values mean 'off' (the safe side)."""
    raw = (cfg.get("dial") or {}).get(str(Lane(lane)))
    try:
        return Dial(_coerce_dial_value(raw))
    except ValueError:
        return Dial.off


def holidays(cfg: dict) -> set[date]:
    """Cairo dates from config 'holidays' (strings or YAML dates)."""
    out: set[date] = set()
    for h in cfg.get("holidays") or []:
        if isinstance(h, date):
            out.add(h)
        elif isinstance(h, str):
            out.add(date.fromisoformat(h.strip()))
        else:
            raise TypeError(f"holiday {h!r} is not a date")
    return out


def consent_ok(cfg: dict, channel: Channel) -> bool:
    """D1 reference on file AND this channel's consent switch is on."""
    consent = cfg.get("consent") or {}
    if not consent.get("d1_ref"):
        return False
    key = _CONSENT_KEY.get(Channel(channel))
    return bool(key and (consent.get("channels") or {}).get(key) is True)


def notice_gate_ok(cfg: dict) -> bool:
    """05 3.1 Globex notice gate: Sysadmin's B#1 and Executive's B#2 refs on file (and Signatory's, if he must approve)."""
    consent = cfg.get("consent") or {}
    gate = consent.get("notice_gate") or {}
    if not (gate.get("sysadmin_b1_ref") and gate.get("executive_b2_ref")):
        return False
    if consent.get("signatory_must_approve") and not consent.get("signatory_ref"):
        return False
    return True
