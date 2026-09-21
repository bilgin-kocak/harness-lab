from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.trace.normalize import close_orphaned_calls
from harnesslab.trace.redaction import Redactor


def test_emitter_sequences_and_redacts():
    seen: list[list] = []
    emitter = EventEmitter(
        "run_x", redactor=Redactor(include_process_env=False), sink=seen.append, flush_threshold=2
    )
    e1 = emitter.emit(
        EventKind.ASSISTANT_MESSAGE,
        payload={"text": "key sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"},
    )
    e2 = emitter.emit(
        EventKind.TOOL_STARTED, name="read", call_id="c1", payload={"input": {"path": "a"}}
    )
    e3 = emitter.emit(EventKind.TOOL_FINISHED, name="read", call_id="c1", duration_ms=5)
    assert [e.sequence for e in (e1, e2, e3)] == [0, 1, 2]
    assert e1.run_id == "run_x" and e1.event_id.startswith("evt_")
    assert "sk-proj" not in e1.payload["text"]
    assert len(seen) == 1 and len(seen[0]) == 2  # flushed at threshold
    flushed = emitter.flush()
    assert flushed == [e3] and len(seen) == 2
    assert emitter.count(EventKind.TOOL_STARTED) == 1


def test_close_orphaned_calls_adds_synthetic_finish():
    emitter = EventEmitter("run_y", redactor=Redactor(include_process_env=False))
    emitter.emit(EventKind.COMMAND_STARTED, name="shell", call_id="a", payload={"command": "ls"})
    emitter.emit(EventKind.TOOL_STARTED, name="edit", call_id="b", parent_call_id="sub")
    emitter.emit(EventKind.TOOL_FINISHED, name="edit", call_id="b")
    assert close_orphaned_calls(emitter) == 1
    last = emitter.events[-1]
    assert (
        last.kind == EventKind.COMMAND_FINISHED
        and last.call_id == "a"
        and last.payload["status"] == "interrupted"
    )
