# tests/unit/test_conversation_thread.py
"""P13: the conversation thread survives a reload.

The store keeps a rolling, capped log of the thread as the user experienced
it; every turn-family endpoint appends both halves server-side; the log rides
the persisted snapshot; `conversation.respond` falls back to the server log
when the client sends no history (the reload case) and never duplicates the
current user line when the client does send its array. Fully offline: a
client that raises forces every LLM path onto its deterministic fallback.
"""
import unittest

from fastapi.testclient import TestClient

from src.agent import conversation, llm, persistence
from src.api import server
from src.sim.fake_store import (
    FakeStore, CONVERSATION_MAX_ENTRIES, CONVERSATION_MAX_CHARS,
)

WS = "ws_thread_test"


class _RaisingClient:
    """Forces the deterministic fallback path without any network call."""
    class _Models:
        def generate_content(self, *a, **k):
            raise RuntimeError("no credits in test")
    models = _Models()


class TestStoreLog(unittest.TestCase):
    def test_append_caps_entries_and_drops_oldest(self):
        store = FakeStore(workspace_id=WS)
        for i in range(25):  # 25 exchanges = 50 entries, over the 40 cap
            store.append_conversation("user", f"question {i}")
            store.append_conversation("assistant", f"answer {i}")
        self.assertEqual(len(store.conversation), CONVERSATION_MAX_ENTRIES)
        # The oldest exchanges fell off; the newest survived intact.
        self.assertEqual(store.conversation[0]["content"], "question 5")
        self.assertEqual(store.conversation[-1]["content"], "answer 24")

    def test_append_normalizes_roles_drops_empties_caps_length(self):
        store = FakeStore(workspace_id=WS)
        store.append_conversation("model", "hello")   # any non-user role -> assistant
        store.append_conversation("user", "   ")      # empty after strip: dropped
        store.append_conversation("user", "x" * (CONVERSATION_MAX_CHARS + 500))
        self.assertEqual(len(store.conversation), 2)
        self.assertEqual(store.conversation[0]["role"], "assistant")
        self.assertEqual(store.conversation[1]["role"], "user")
        self.assertEqual(len(store.conversation[1]["content"]), CONVERSATION_MAX_CHARS)
        self.assertIn("at", store.conversation[0])

    def test_append_publishes_nothing_to_the_event_stream(self):
        """The log is user content: it must never ride SSE/traces."""
        store = FakeStore(workspace_id=WS)
        queue = store.subscribe()
        store.append_conversation("user", "a private thing I typed")
        self.assertTrue(queue.empty())
        self.assertEqual(store.traces, [])


class TestSnapshotRoundTrip(unittest.TestCase):
    def test_conversation_rides_the_snapshot(self):
        store = FakeStore(workspace_id=WS)
        store.append_conversation("user", "plan my week")
        store.append_conversation("assistant", "Done. Two sessions placed.")
        revived = persistence.restore(FakeStore(workspace_id=WS), persistence.snapshot(store))
        self.assertEqual(
            [(m["role"], m["content"]) for m in revived.conversation],
            [("user", "plan my week"), ("assistant", "Done. Two sessions placed.")],
        )

    def test_restore_recaps_and_skips_junk_rows(self):
        store = FakeStore(workspace_id=WS)
        sections = persistence.snapshot(store)
        rows = [{"role": "user", "content": f"line {i}"} for i in range(60)]
        rows.insert(0, {"role": "user"})          # no content: skipped
        rows.insert(0, "not even a dict")          # junk: skipped
        sections["meta"]["conversation"] = rows
        revived = persistence.restore(FakeStore(workspace_id=WS), sections)
        self.assertEqual(len(revived.conversation), CONVERSATION_MAX_ENTRIES)
        self.assertEqual(revived.conversation[-1]["content"], "line 59")


class TestPromptHistory(unittest.TestCase):
    def setUp(self):
        server.stores.clear()

    def tearDown(self):
        server.stores.clear()

    def test_server_log_stands_in_when_client_sends_none(self):
        store = server.get_or_create_store(WS)
        store.append_conversation("user", "call the project Falcon")
        store.append_conversation("assistant", "Falcon it is.")
        turns = conversation._prompt_history(WS, None, "what did I just ask you?")
        self.assertEqual(turns, [
            {"role": "user", "text": "call the project Falcon"},
            {"role": "model", "text": "Falcon it is."},
        ])

    def test_client_history_wins_and_current_user_line_is_not_doubled(self):
        store = server.get_or_create_store(WS)
        store.append_conversation("user", "server-side only line")
        client_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "what did I ask?"},  # the live turn
        ]
        turns = conversation._prompt_history(WS, client_history, "what did I ask?")
        # Client array used (not the server log), normalized to the llm shape,
        # and the trailing copy of the live user line dropped (generate_text
        # appends the live turn itself).
        self.assertEqual(turns, [
            {"role": "user", "text": "hi"},
            {"role": "model", "text": "hello"},
        ])

    def test_prompt_window_is_capped(self):
        rows = [{"role": "user", "content": f"m{i}"} for i in range(120)]
        turns = conversation._prompt_history(WS, rows, "new message")
        self.assertEqual(len(turns), CONVERSATION_MAX_ENTRIES)
        self.assertEqual(turns[-1]["text"], "m119")

    def test_respond_reaches_the_model_with_the_server_log(self):
        store = server.get_or_create_store(WS)
        store.append_conversation("user", "remember the number 41")
        store.append_conversation("assistant", "Noted in this thread.")
        seen = {}

        def fake_generate_text(system, user, history=None, **kw):
            seen["history"] = history
            return "I remember."

        real = llm.generate_text
        llm.generate_text = fake_generate_text
        try:
            out = conversation.respond(WS, "what number did I mention?")
        finally:
            llm.generate_text = real
        self.assertEqual(out["type"], "message")
        self.assertEqual(seen["history"], [
            {"role": "user", "text": "remember the number 41"},
            {"role": "model", "text": "Noted in this thread."},
        ])


class TestTurnFamilyAppends(unittest.TestCase):
    """The endpoint seams: one append per request, both halves, no duplicates."""

    def setUp(self):
        server.stores.clear()
        llm.set_client(_RaisingClient())  # offline: deterministic replies only
        self.client = TestClient(server.app)
        self.ws = WS

    def tearDown(self):
        llm.set_client(None)
        server.stores.clear()

    def test_turn_appends_both_halves_even_when_client_sends_its_array(self):
        msg = "what does my week look like?"
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": msg,
                  "history": [{"role": "user", "content": msg}]},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        log = server.stores[self.ws].conversation
        # Exactly one user line (from the request, not the client array) and
        # exactly one assistant line: the reply that actually shipped.
        self.assertEqual([(m["role"], m["content"]) for m in log],
                         [("user", msg), ("assistant", body["text"])])

    def test_question_reply_logs_the_question_text(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "I want to become a data scientist"},
        )
        body = r.json()
        self.assertEqual(body["type"], "question")
        log = server.stores[self.ws].conversation
        self.assertEqual(log[0]["content"], "I want to become a data scientist")
        self.assertEqual(log[1]["role"], "assistant")
        self.assertEqual(log[1]["content"], body["question"]["question"])

    def test_elicit_answer_logs_the_answer_and_the_next_reply(self):
        r = self.client.post(
            f"/v1/workspaces/{self.ws}/turn",
            json={"message": "I want to become a data scientist"},
        )
        session = r.json()["session"]
        before = len(server.stores[self.ws].conversation)
        r2 = self.client.post(
            f"/v1/workspaces/{self.ws}/elicit/answer",
            json={"commitment_id": session["commitment_id"],
                  "goal": session["goal"],
                  "field": "platforms", "value": ["Coursera"]},
        )
        self.assertEqual(r2.status_code, 200)
        log = server.stores[self.ws].conversation
        self.assertEqual(len(log), before + 2)
        self.assertEqual(log[-2]["role"], "user")
        self.assertEqual(log[-2]["content"], "Coursera")
        self.assertEqual(log[-1]["role"], "assistant")

    def test_details_exposes_the_thread_for_rehydration(self):
        msg = "how are you today?"
        self.client.post(f"/v1/workspaces/{self.ws}/turn", json={"message": msg})
        d = self.client.get(f"/v1/workspaces/{self.ws}/details").json()
        self.assertIn("conversation", d)
        self.assertEqual(d["conversation"][0], {"role": "user", "content": msg})
        self.assertEqual(d["conversation"][1]["role"], "assistant")
        # Rehydration payload carries role+content only, no timestamps.
        self.assertEqual(sorted(d["conversation"][0].keys()), ["content", "role"])

    def test_many_turns_leave_exactly_the_cap_in_the_store(self):
        for i in range(25):  # 25 exchanges = 50 halves, over the 40 cap
            self.client.post(f"/v1/workspaces/{self.ws}/turn",
                             json={"message": f"what about item {i}?"})
        log = server.stores[self.ws].conversation
        self.assertEqual(len(log), CONVERSATION_MAX_ENTRIES)
        self.assertEqual(log[-2]["content"], "what about item 24?")

    def test_conversation_never_rides_the_event_stream(self):
        """Turn appends publish nothing: the SSE queue sees routing events
        only, never the thread's content."""
        store = server.get_or_create_store(self.ws)
        queue = store.subscribe()
        secret_line = "my private plan for thursday"
        self.client.post(f"/v1/workspaces/{self.ws}/turn",
                         json={"message": f"what should I do about {secret_line}?"})
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        self.assertNotIn(secret_line, str(events))


if __name__ == "__main__":
    unittest.main()
