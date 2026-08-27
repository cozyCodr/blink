# P11-11: commitment titles are LABELS in the horizon, so they must be short
# names, never the user's raw brain-dump sliced mid-word at 60 characters
# ("Also I am prepping for a conference talk in six weeks. Outli").
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from src.agent import llm
from src.agent.specialists import namer
from src.agent.workspace_registry import stores
from src.api import server
from src.api.server import app


BRAIN_DUMPS = [
    "Also I am prepping for a conference talk in six weeks. Outline the talk, 90m. Rehearse twice, 60m.",
    "I need to finish my tax return before the deadline. Gather receipts 45m. Fill in the forms 90m.",
    "ok so my sister's wedding is in november and I said I would handle the photo slideshow, collect photos 60m, edit the video 120m",
]


class _RaisingClient:
    """Every LLM call fails -> deterministic fallbacks everywhere."""
    class models:  # noqa: N801 - mimic google-genai client shape
        @staticmethod
        def generate_content(*a, **k):
            raise RuntimeError("offline test")


class _NamingClient:
    """Answers the naming call with a short name; fails everything else so the
    rest of the turn still runs on its deterministic path."""

    NAME = "Conference talk prep"

    class models:  # noqa: N801
        @staticmethod
        def generate_content(*a, **k):
            schema = (k.get("config") or None)
            name = getattr(getattr(schema, "response_schema", None), "__name__", "")
            if name != "CommitmentName":
                raise RuntimeError("offline test")

            class _Resp:
                parsed = None
                candidates = None
                text = json.dumps({"name": _NamingClient.NAME})
            return _Resp()


def _titles(client, ws):
    body = client.get(f"/v1/workspaces/{ws}/details").json()
    return [c["title"] for c in body.get("commitments", [])]


class TestFallbackName(unittest.TestCase):
    """The deterministic fallback must be honest and label-shaped."""

    def test_long_brain_dump_becomes_generic_not_a_fragment(self):
        for dump in BRAIN_DUMPS:
            got = namer.fallback_name(dump)
            self.assertEqual(got, namer.GENERIC_NAME, dump)

    def test_short_first_sentence_is_kept(self):
        self.assertEqual(namer.fallback_name("Plan the offsite. Book a room 30m."), "Plan the offsite")

    def test_empty_text_is_generic(self):
        self.assertEqual(namer.fallback_name("   "), namer.GENERIC_NAME)
        self.assertEqual(namer.fallback_name("", generic="From your photo"), "From your photo")

    def test_never_a_midword_truncation(self):
        for dump in BRAIN_DUMPS:
            got = namer.fallback_name(dump)
            self.assertFalse(dump.startswith(got) and len(got) < len(dump), got)


class TestNameCommitmentOffline(unittest.TestCase):
    def setUp(self):
        llm.set_client(_RaisingClient())

    def tearDown(self):
        llm.set_client(None)

    def test_model_unavailable_uses_deterministic_fallback(self):
        for dump in BRAIN_DUMPS:
            self.assertEqual(namer.name_commitment(dump), namer.GENERIC_NAME, dump)

    def test_model_name_is_used_when_usable(self):
        llm.set_client(_NamingClient())
        self.assertEqual(namer.name_commitment(BRAIN_DUMPS[0]), "Conference talk prep")

    def test_unusable_model_name_falls_back(self):
        for bad in ["", "   ", "x", "The user is preparing for a conference talk in about six weeks."]:
            _NamingClient.NAME = bad
            llm.set_client(_NamingClient())
            self.assertEqual(namer.name_commitment(BRAIN_DUMPS[0]), namer.GENERIC_NAME, bad)
        _NamingClient.NAME = "Conference talk prep"


class TestStoredCommitmentTitles(unittest.TestCase):
    """End to end through /turn: no stored title is a sentence fragment."""

    WS = "ws_p1111_naming"

    def setUp(self):
        llm.set_client(_RaisingClient())
        stores.pop(self.WS, None)
        self.client = TestClient(app)

    def tearDown(self):
        llm.set_client(None)
        stores.pop(self.WS, None)

    def test_titles_are_short_and_not_raw_input(self):
        for i, dump in enumerate(BRAIN_DUMPS):
            ws = f"{self.WS}_{i}"
            stores.pop(ws, None)
            self.client.post(f"/v1/workspaces/{ws}/turn", json={"message": dump})
            for title in _titles(self.client, ws):
                self.assertLessEqual(len(title), namer.MAX_NAME_CHARS, title)
                self.assertNotIn(".", title, title)
                self.assertFalse(dump.startswith(title), f"raw-input slice: {title!r}")
            stores.pop(ws, None)


class TestNamingOffTheCriticalPath(unittest.TestCase):
    """P12-03a: the namer's output never appears in the reply, so it runs
    ALONGSIDE the turn's heavy step instead of in front of it. What must not
    change: the commitment still carries a real name by the time the response
    goes out, and it is honest at every moment in between."""

    def test_naming_overlaps_the_turns_real_work(self):
        marks = {}

        def slow_name(raw_text, *, generic=namer.GENERIC_NAME, now=None):
            marks["name_start"] = time.perf_counter()
            time.sleep(0.20)
            marks["name_end"] = time.perf_counter()
            return "Conference talk prep"

        with mock.patch.object(server, "name_commitment", slow_name):
            t0 = time.perf_counter()
            finish = server._start_naming("some notes", generic=namer.GENERIC_NAME)
            time.sleep(0.20)                      # stands in for extraction
            work_end = time.perf_counter()
            comm = SimpleNamespace(title=namer.GENERIC_NAME)
            finish(comm)
            elapsed = time.perf_counter() - t0

        self.assertEqual(comm.title, "Conference talk prep")
        # Overlap, not sequence: naming was already running during the work.
        self.assertLess(marks["name_start"], work_end)
        # Serial would be ~0.40s; concurrent is ~0.20s plus scheduling slop.
        self.assertLess(elapsed, 0.35)

    def test_a_failed_namer_leaves_the_honest_title_alone(self):
        def boom(*a, **k):
            raise RuntimeError("namer down")

        with mock.patch.object(server, "name_commitment", boom):
            finish = server._start_naming("some notes", generic="From your photo")
            comm = SimpleNamespace(title="From your photo")
            finish(comm)
        self.assertEqual(comm.title, "From your photo")

    def test_an_empty_name_leaves_the_honest_title_alone(self):
        with mock.patch.object(server, "name_commitment", lambda *a, **k: ""):
            finish = server._start_naming("some notes", generic=namer.GENERIC_NAME)
            comm = SimpleNamespace(title=namer.GENERIC_NAME)
            finish(comm)
        self.assertEqual(comm.title, namer.GENERIC_NAME)

    def test_turn_still_stores_the_model_name(self):
        """End to end: moving the call off the critical path did not drop it."""
        ws = "ws_p1203_naming"
        stores.pop(ws, None)
        _NamingClient.NAME = "Conference talk prep"
        llm.set_client(_NamingClient())
        try:
            client = TestClient(app)
            client.post(f"/v1/workspaces/{ws}/turn", json={"message": BRAIN_DUMPS[0]})
            self.assertIn("Conference talk prep", _titles(client, ws))
        finally:
            llm.set_client(None)
            stores.pop(ws, None)


if __name__ == "__main__":
    unittest.main()
