"""Shared onboarding wizard plumbing: validators that never echo the input, and a bounded prompt loop
that validates, re-asks on invalid, skips optional blanks, and can never hang on scripted/empty input."""
from __future__ import annotations

from twin_agent.onboarding import wizard


def test_validators_accept_good_and_reject_bad():
    assert wizard.valid_int("1234567")[0]
    assert not wizard.valid_int("12ab")[0]
    assert wizard.valid_email("principal@example.com")[0]
    assert not wizard.valid_email("not-an-email")[0]
    assert wizard.valid_url("https://app.example.com")[0]
    assert not wizard.valid_url("app.example.com")[0]
    assert wizard.valid_json_object('{"a":1}')[0]
    assert not wizard.valid_json_object("[1,2,3]")[0]   # array is JSON but not an object
    assert not wizard.valid_json_object("nope")[0]
    assert wizard.valid_bot_token("123456:AAbbcc")[0]
    assert not wizard.valid_bot_token("no-colon-here")[0]


def test_validator_errors_never_contain_the_input():
    bad = "my-secret-value-9931"
    for v in (wizard.valid_int, wizard.valid_email, wizard.valid_url,
              wizard.valid_json, wizard.valid_json_object, wizard.valid_bot_token):
        ok, msg = v(bad)
        assert not ok
        assert bad not in msg, f"{v.__name__} leaked the input into its error"


def test_is_skip_matches_common_skip_words():
    for w in ("", "no", "No", "SKIP", "n/a", "not yet", "-"):
        assert wizard.is_skip(w)
    assert not wizard.is_skip("yes")
    assert not wizard.is_skip("C:/rec/principal.wav")


def test_ask_value_returns_first_valid_answer():
    feed = iter(["  hello  "])
    out: list[str] = []
    assert wizard.ask_value(lambda _p: next(feed, ""), "q?", say=out.append) == "hello"


def test_ask_value_optional_blank_is_skipped_in_one_read():
    reads = []
    out: list[str] = []
    val = wizard.ask_value(lambda p: (reads.append(p) or ""), "q?", optional=True, say=out.append)
    assert val == ""
    assert len(reads) == 1   # skipped immediately, not re-asked


def test_ask_value_reprompts_on_invalid_then_takes_valid():
    feed = iter(["bad", '{"ok":1}'])
    out: list[str] = []
    val = wizard.ask_value(lambda _p: next(feed, ""), "paste json:",
                           validate=wizard.valid_json_object, say=out.append)
    assert val == '{"ok":1}'
    assert any("again" in line.lower() for line in out)


def test_ask_value_is_bounded_and_never_hangs_on_bad_input():
    calls = {"n": 0}

    def always_bad(_p):
        calls["n"] += 1
        return "still-bad"

    out: list[str] = []
    val = wizard.ask_value(always_bad, "paste json:", validate=wizard.valid_json_object, say=out.append)
    assert val == ""                       # gave up instead of looping forever
    assert calls["n"] == wizard.MAX_ATTEMPTS
