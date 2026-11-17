from twin_agent.onboarding import automation as A

CFG = """run_mode: observe
send_enabled: false
voice:
  enabled: false
  autopilot: false                 # true = timeout / holding-floor sends may be voice too
timesheets:
  autopilot: false                 # auto-approve ONLY when eligible: 14 days
  autopilot_days: 14
"""

def test_auto_sets_both_autopilot_true_preserving_comments():
    out, n = A.apply_to_text(CFG, "auto")
    assert n == 2
    assert "autopilot: true                 # true = timeout" in out          # comment kept
    assert "autopilot: true                 # auto-approve ONLY when eligible" in out
    assert "run_mode: observe" in out and "send_enabled: false" in out        # gates untouched
    assert "autopilot_days: 14" in out                                        # non-matching key untouched

def test_manual_sets_false():
    started, _ = A.apply_to_text(CFG, "auto")
    back, n = A.apply_to_text(started, "manual")
    assert n == 2 and "autopilot: false" in back and "autopilot: true" not in back

def test_answer_to_mode():
    assert A.answer_to_mode("AUTOMATIC") == "auto"
    assert A.answer_to_mode("manual") == "manual"
    assert A.answer_to_mode("maybe later") is None

def test_never_touches_run_mode_or_send_enabled():
    out, _ = A.apply_to_text(CFG, "auto")
    assert "run_mode: observe" in out
    assert "send_enabled: false" in out
