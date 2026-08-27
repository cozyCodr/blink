"""
Offline unit tests for the llm client lifecycle (Bug 1: long-running server
staleness).

Proves:
- A transient failure on the real-client path is retried exactly once on a
  freshly built client (via the _build_client seam), and the retry's success
  is returned.
- A second failure raises LlmUnavailable.
- Injected fakes (set_client) are never dropped or rebuilt — the test seam
  stays fully offline.
- Cached real clients older than the max age are proactively rebuilt.

No network, no spend: _build_client is monkeypatched everywhere a real client
would be constructed.
"""
import time
import types as pytypes
import unittest

from src.agent import llm


class _FakeModels:
    def __init__(self, outcomes):
        # outcomes: list of Exception instances to raise or response objects to return
        self._outcomes = list(outcomes)
        self.calls = 0

    def generate_content(self, *a, **k):
        self.calls += 1
        out = self._outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _FakeClient:
    def __init__(self, outcomes):
        self.models = _FakeModels(outcomes)


def _ok(text="ok"):
    return pytypes.SimpleNamespace(text=text)


class TestLlmClientLifecycle(unittest.TestCase):
    def setUp(self):
        self._orig_build = llm._build_client
        llm.set_client(None)

    def tearDown(self):
        llm._build_client = self._orig_build
        llm.set_client(None)

    def _install_build_seq(self, clients):
        """Monkeypatch _build_client to hand out the given clients in order."""
        state = {"builds": 0}
        pool = list(clients)

        def fake_build():
            state["builds"] += 1
            return pool.pop(0)

        llm._build_client = fake_build
        return state

    def test_retry_once_with_fresh_client_returns_success(self):
        first = _FakeClient([RuntimeError("stale connection pool")])
        second = _FakeClient([_ok("fresh client wins")])
        state = self._install_build_seq([first, second])

        text = llm.generate_text("sys", "hello")

        self.assertEqual(text, "fresh client wins")
        self.assertEqual(state["builds"], 2)
        self.assertEqual(first.models.calls, 1)
        self.assertEqual(second.models.calls, 1)
        # The fresh client is now the cached one.
        self.assertIs(llm._client, second)

    def test_second_failure_raises_llm_unavailable(self):
        first = _FakeClient([RuntimeError("boom 1")])
        second = _FakeClient([RuntimeError("boom 2")])
        state = self._install_build_seq([first, second])

        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text("sys", "hello")
        self.assertEqual(state["builds"], 2)

    def test_injected_fake_is_never_dropped_or_rebuilt(self):
        def fail_build():
            raise AssertionError("_build_client must not run for injected fakes")

        llm._build_client = fail_build
        fake = _FakeClient([RuntimeError("fake always fails")])
        llm.set_client(fake)

        with self.assertRaises(llm.LlmUnavailable):
            llm.generate_text("sys", "hello")
        # The injected fake stays installed and was called exactly once (no retry).
        self.assertIs(llm._client, fake)
        self.assertEqual(fake.models.calls, 1)

    def test_cached_client_expires_after_max_age(self):
        a = _FakeClient([])
        b = _FakeClient([])
        state = self._install_build_seq([a, b])

        self.assertIs(llm.get_client(), a)
        self.assertIs(llm.get_client(), a)  # fresh cache reused
        self.assertEqual(state["builds"], 1)

        # Age the cached client past the 30-minute horizon.
        llm._client_created_at = time.monotonic() - (llm._CLIENT_MAX_AGE_SECONDS + 1)
        self.assertIs(llm.get_client(), b)
        self.assertEqual(state["builds"], 2)

    def test_injected_fake_never_expires(self):
        def fail_build():
            raise AssertionError("_build_client must not run for injected fakes")

        llm._build_client = fail_build
        fake = _FakeClient([_ok()])
        llm.set_client(fake)
        # Even with no created_at timestamp, injected fakes are returned as-is.
        self.assertIs(llm.get_client(), fake)


if __name__ == "__main__":
    unittest.main()
