"""Live lane-session registry: add / list / mark idle / close, persisted to data/state JSON."""
from twin_agent.registry import CLOSED, IDLE, OPEN, PREPARED, LaneRegistry, registry_path


def test_add_marks_open_and_lists(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    reg.add("dept-delivery", title="dept-delivery", cmd="wt.exe ...", launched=True, person="supervisor")
    assert reg.is_open("dept-delivery") and reg.ever_launched("dept-delivery")
    rows = reg.list_open()
    assert len(rows) == 1 and rows[0]["lane"] == "dept-delivery" and rows[0]["person"] == "supervisor"
    assert rows[0]["opened_at"] and rows[0]["updated_at"] and rows[0]["status"] == OPEN


def test_mark_idle_then_close(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    reg.add("ticket-2636", launched=True)
    assert reg.mark_idle("ticket-2636")["status"] == IDLE
    assert reg.is_open("ticket-2636") is False
    assert reg.ever_launched("ticket-2636") is True          # launched flag survives an idle/close
    assert reg.close("ticket-2636")["status"] == CLOSED
    assert reg.list_open() == []


def test_observe_add_is_prepared_not_open(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    reg.add("dept-content", launched=False)                  # observe: no real tab
    assert reg.get("dept-content")["status"] == "prepared"
    assert reg.is_open("dept-content") is False and reg.list_open() == []


def test_persists_across_instances(tmp_home, clock):
    LaneRegistry(clock=clock).add("dept-sales", launched=True)
    assert LaneRegistry(clock=clock).is_open("dept-sales")
    assert registry_path().exists()


def test_close_unknown_lane_is_none(tmp_home, clock):
    assert LaneRegistry(clock=clock).close("nope") is None


# ---------------------------------------------------------------- what Digital Twin can SEE (correction #12)

def test_mark_status_sets_a_short_status_string_without_touching_open_state(tmp_home, clock):
    # The lifecycle `status` (open/idle/...) must survive a free-text status note, or is_open silently flips
    # and the next message opens a duplicate tab.
    reg = LaneRegistry(clock=clock)
    reg.add("dept-delivery", launched=True)
    row = reg.mark_status("dept-delivery", "reproducing the stray-IN bug")
    assert row["status_text"] == "reproducing the stray-IN bug"
    assert reg.is_open("dept-delivery") is True and row["status"] == OPEN   # lifecycle state untouched


def test_mark_status_on_unknown_lane_is_none(tmp_home, clock):
    assert LaneRegistry(clock=clock).mark_status("nope", "hi") is None


def test_last_task_is_recorded_redacted_and_truncated(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    long = "fix the PO for client +201234567890 " + "x" * 500          # a phone number must be redacted on disk
    reg.add("ticket-9", launched=True, last_task=long)
    row = reg.get("ticket-9")
    assert "201234567890" not in row["last_task"]                       # redacted like drop_task does
    assert len(row["last_task"]) <= 200                                 # truncated, a short hint only


def test_note_task_updates_last_task_on_an_open_lane(tmp_home, clock):
    # The inbox-only path (a task dropped on an already-open lane) must still refresh the last_task hint.
    reg = LaneRegistry(clock=clock)
    reg.add("ticket-9", launched=True, last_task="first")
    row = reg.note_task("ticket-9", "second task now")
    assert row["last_task"] == "second task now" and reg.is_open("ticket-9") is True


def test_tags_and_knows_are_word_boundary_not_substring(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    reg.add("dept-delivery", launched=True, tags=["stray-in", "po", "delivery"])
    assert reg.knows("dept-delivery", "the PO is wrong") is True        # matches tag "po"
    assert reg.knows("dept-delivery", "DELIVERY issue") is True         # case-insensitive
    assert reg.knows("dept-delivery", "deliverables report") is False   # "delivery" not a substring of a word
    assert reg.knows("ticket-x", "po") is False                         # unknown lane knows nothing


def test_register_and_open_are_thin_wrappers_keeping_launched_sticky(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    reg.register("dept-sales")                                          # observe register = prepared
    assert reg.get("dept-sales")["status"] == PREPARED and reg.is_open("dept-sales") is False
    reg.open("dept-sales")                                              # a real tab opened
    assert reg.is_open("dept-sales") and reg.ever_launched("dept-sales")
    reg.mark_idle("dept-sales")
    assert reg.ever_launched("dept-sales") is True                      # sticky across idle


def test_list_sessions_shows_what_principal_can_see(tmp_home, clock):
    reg = LaneRegistry(clock=clock)
    reg.add("dept-delivery", launched=True, last_task="fix PO", tags=["po"])
    reg.mark_status("dept-delivery", "on it")
    reg.add("dept-sales", launched=False)                              # prepared
    sessions = reg.list_sessions()
    by_lane = {s["lane"]: s for s in sessions}
    assert set(by_lane) == {"dept-delivery", "dept-sales"}             # every lane, open or prepared
    d = by_lane["dept-delivery"]
    assert d["status"] == OPEN and d["status_text"] == "on it" and d["last_task"] == "fix PO" and d["tags"] == ["po"]
