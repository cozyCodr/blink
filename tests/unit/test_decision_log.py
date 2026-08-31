# P16-01: the legible decision trace. The line SHAPE is the product — one
# stdout line per decision, ids/intents/counts only, never message content or
# task titles. All offline via a raising LLM client.
import re

from fastapi.testclient import TestClient

from src.agent import decision_log, llm
from src.agent.workspace_registry import stores
from src.api.server import app


class _RaisingClient:
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


# --- The composing helpers ---------------------------------------------------

def test_short_ws_keeps_guest_ids_and_trims_user_ids():
    assert decision_log.short_ws("ws_demo") == "ws_demo"
    assert decision_log.short_ws("g_abc123") == "g_abc123"
    trimmed = decision_log.short_ws("u_0123456789abcdef")
    assert trimmed == "u_012345…"
    # short u_ ids are left alone rather than mangled
    assert decision_log.short_ws("u_short") == "u_short"


def test_decision_prints_one_prefixed_line(capsys):
    decision_log.decision("turn", "ws_demo", "intent=chat -> reply")
    out = capsys.readouterr().out
    assert out == "[turn ws=ws_demo] intent=chat -> reply\n"


def test_turn_summary_replanned_shape():
    res = {
        "type": "replanned", "cancelled_blocks": 2, "rescheduled_blocks": 6,
        "schedule": {"utilization_pct": 42, "unplaced": []},
    }
    line = decision_log.turn_summary("disruption", res, 120)
    assert line == ("intent=disruption -> cleared 2 today, re-placed 6, "
                    "unplaced 0, utilization 42% (120ms)")


def test_turn_summary_planned_shape():
    res = {
        "type": "planned", "tasks": 3, "blocks_scheduled": 5,
        "schedule": {"utilization_pct": 17, "unplaced": [{"task_id": "t_9"}]},
    }
    line = decision_log.turn_summary("concrete_tasks", res)
    assert line == ("intent=concrete_tasks -> mapped 3 tasks, placed 5 blocks, "
                    "unplaced 1, utilization 17%")


def test_turn_summary_checkin_and_message_shapes():
    checkin = {"type": "checkin", "blocks": [{}, {}], "measured": [{}]}
    assert decision_log.turn_summary("checkin", checkin) == \
        "intent=checkin -> checkin opened: 2 pending, 1 timer-measured"
    msg = {"type": "message", "text": "whatever the reply said"}
    line = decision_log.turn_summary("chat", msg)
    assert line == "intent=chat -> reply (no schedule change)"
    # the reply TEXT never leaks into the line
    assert "whatever" not in line


def test_turn_summary_flags_a_surfaced_insight_by_id_only():
    res = {"type": "message", "insight": {"insight_id": "ins_1", "text": "secret"}}
    line = decision_log.turn_summary("chat", res)
    assert "insight surfaced id=ins_1" in line
    assert "secret" not in line


def test_checkin_close_summary_shape():
    res = {"done": 2, "partial": 1, "skipped": 1, "rescheduled": 3, "streak": 4}
    assert decision_log.checkin_close_summary(res, 40) == \
        "closed day: done 2, partial 1, skipped 1, re-placed 3, streak 4 (40ms)"


# --- The wired trace, from a mocked (offline) turn ---------------------------

def test_turn_emits_trace_line_without_message_content(capsys):
    llm.set_client(_RaisingClient())
    ws = "ws_trace_test"
    stores.pop(ws, None)
    try:
        client = TestClient(app)
        res = client.post(f"/v1/workspaces/{ws}/turn",
                          json={"message": "my meeting ran over"})
        assert res.status_code == 200
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if l.startswith(f"[turn ws={ws}]")]
        assert len(lines) == 1, out
        assert re.fullmatch(
            r"\[turn ws=ws_trace_test\] intent=disruption -> "
            r"cleared \d+ today, re-placed \d+, unplaced \d+"
            # utilization_pct is a float from the scheduler's own diagnostics
            # ("0.0%", "13.3%"); the disruption path now publishes ITS pass's
            # report instead of leaving an earlier pass's numbers standing.
            r"(, utilization [\d.]+%)? \(\d+ms\)",
            lines[0]), lines[0]
        # zero content: the user's message never reaches stdout
        assert "meeting" not in out
        assert "ran over" not in out
    finally:
        llm.set_client(None)
        stores.pop(ws, None)


def test_checkin_resolve_emits_trace_line(capsys):
    llm.set_client(_RaisingClient())
    ws = "ws_trace_resolve"
    stores.pop(ws, None)
    try:
        client = TestClient(app)
        # An unknown block 404s before any state change — no decision, no line.
        res = client.post(f"/v1/workspaces/{ws}/checkin/resolve",
                          json={"block_id": "b_missing", "outcome": "done"})
        assert res.status_code == 404
        assert "[checkin" not in capsys.readouterr().out

        # Plan something real, then resolve its first block.
        client.post(f"/v1/workspaces/{ws}/ingest",
                    json={"text": "- Draft chapter (30 mins)",
                          "commitment_title": "x", "stake": 3, "kind": "course"})
        store = stores[ws]
        assert store.blocks, "ingest should have scheduled at least one block"
        block_id = next(iter(store.blocks))
        capsys.readouterr()  # drop the ingest's own [plan] line
        res = client.post(f"/v1/workspaces/{ws}/checkin/resolve",
                          json={"block_id": block_id, "outcome": "done"})
        assert res.status_code == 200
        out = capsys.readouterr().out
        assert re.search(
            rf"\[checkin ws={ws}\] resolved block={block_id} outcome=done "
            r"source=reported remaining=\d+", out), out
        assert "Draft chapter" not in out  # titles never reach the trace
    finally:
        llm.set_client(None)
        stores.pop(ws, None)


def test_ingest_emits_plan_trace_line(capsys):
    llm.set_client(_RaisingClient())
    ws = "ws_trace_ingest"
    stores.pop(ws, None)
    try:
        client = TestClient(app)
        res = client.post(f"/v1/workspaces/{ws}/ingest",
                          json={"text": "- Read the paper (45 mins)",
                                "commitment_title": "x", "stake": 3,
                                "kind": "course"})
        assert res.status_code == 202
        out = capsys.readouterr().out
        assert re.search(
            rf"\[plan ws={ws}\] ingested commitment=c_\d+: "
            r"extracted \d+ tasks, raised \d+ questions, placed \d+ blocks",
            out), out
        assert "Read the paper" not in out
    finally:
        llm.set_client(None)
        stores.pop(ws, None)
