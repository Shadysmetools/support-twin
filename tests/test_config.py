"""config.py: loading, split files, dial coercion, allow-list, consent gates."""
from datetime import date

import pytest
import yaml

from twin_agent import config
from twin_agent.models import Channel, Dial, Lane
from twin_agent.testing import fakes


def test_example_defaults_are_safe(tmp_home):
    cfg = config.load_config()
    assert cfg["run_mode"] == "dry_run" and cfg["send_enabled"] is False
    assert config.lane_dial(cfg, Lane.L11_internal_diagnosis) == Dial.auto
    for lane in Lane:
        if lane != Lane.L11_internal_diagnosis:
            assert config.lane_dial(cfg, lane) in (Dial.observe, Dial.off)


def test_fixture_config_loads(cfg):
    assert cfg["people"]["principal_tg_user_id"] == fakes.PRINCIPAL_ID
    assert cfg["send_enabled"] is False
    assert isinstance(cfg["dial"]["L6_ticket_email"], str)


def test_bare_off_is_coerced(tmp_home):
    p = tmp_home / "config" / "config.yaml"
    p.write_text("dial:\n  L3_t1_answer: off\n", encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg["dial"]["L3_t1_answer"] == "off"
    assert config.lane_dial(cfg, Lane.L3_t1_answer) == Dial.off


def test_bare_on_is_rejected(tmp_home):
    p = tmp_home / "config" / "config.yaml"
    p.write_text("dial:\n  L3_t1_answer: on\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_config(p)


def test_bad_dial_and_lane_are_errors(cfg):
    cfg["dial"]["L3_t1_answer"] = "yolo"
    cfg["dial"]["L99"] = "auto"
    errs = config.validate_config(cfg)
    assert any("L3_t1_answer" in e for e in errs) and any("L99" in e for e in errs)


def test_split_files_merge(tmp_home):
    d = tmp_home / "config"
    (d / "dial.yaml").write_text("L1_holding: draft\n", encoding="utf-8")
    (d / "holidays.yaml").write_text("holidays: [2026-11-05, '2026-12-25']\n", encoding="utf-8")
    (d / "consent.yaml").write_text(yaml.safe_dump({"d1_ref": "DOC-1", "channels": {"telegram": True}}),
                                    encoding="utf-8")
    cfg = config.load_config(d / "config.yaml")
    assert config.lane_dial(cfg, Lane.L1_holding) == Dial.draft
    assert config.lane_dial(cfg, Lane.L3_t1_answer) == Dial.observe       # untouched keys keep their value
    assert config.holidays(cfg) == {date(2026, 11, 5), date(2026, 12, 25)}
    assert config.consent_ok(cfg, Channel.telegram_group)
    assert not config.consent_ok(cfg, Channel.email)
    assert cfg["consent"]["notice_gate"]["sysadmin_b1_ref"] is None          # deep merge kept the rest


def test_live_mode_needs_real_ids(tmp_home):
    p = tmp_home / "config" / "config.yaml"
    p.write_text("run_mode: live\npeople: {principal_tg_user_id: 0}\nchats: []\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_config(p)


def test_missing_explicit_file_raises(tmp_home):
    with pytest.raises(FileNotFoundError):
        config.load_config(tmp_home / "nope.yaml")


def test_allowed_chat(cfg):
    assert config.allowed_chat(cfg, fakes.GROUP_ID, fakes.TOPIC_LOGISTICS)["name"] == "Odoo x Globex"
    assert config.allowed_chat(cfg, str(fakes.GROUP_ID), None) is not None
    assert config.allowed_chat(cfg, fakes.GROUP_ID, 999) is None             # topic not on the list
    assert config.allowed_chat(cfg, -100600) is None                         # Saudi Setup: read false
    assert config.allowed_chat(cfg, 12345) is None                           # personal chat
    assert config.allowed_chat(cfg, 0) is None                               # placeholder never matches
    cfg["chats"][0]["all_topics"] = True
    assert config.allowed_chat(cfg, fakes.GROUP_ID, 999) is not None


def test_consent_and_notice_gate(cfg):
    assert not config.consent_ok(cfg, Channel.telegram_group)
    cfg["consent"]["d1_ref"] = "D1-signed"
    cfg["consent"]["channels"]["telegram"] = True
    assert config.consent_ok(cfg, Channel.telegram_dm)
    assert not config.consent_ok(cfg, Channel.ticket_mail)
    assert not config.notice_gate_ok(cfg)
    cfg["consent"]["notice_gate"] = {"sysadmin_b1_ref": "x", "executive_b2_ref": "y"}
    assert config.notice_gate_ok(cfg)
    cfg["consent"]["signatory_must_approve"] = True
    assert not config.notice_gate_ok(cfg)
