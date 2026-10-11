# tests/unit/test_sleep_mode.py
"""Archive mode (BLINK_SLEEP=1, 2026-10-11): the deployment rests honestly.

The sleep page replaces the app shell, every /v1 call answers 503 with a
plain-words explanation instead of pretending to plan, and /_health stays
live so monitoring can tell "asleep" from "dead". Unset the var to wake it.
"""
import os
from unittest import mock

from fastapi.testclient import TestClient
from src.api.server import app

client = TestClient(app)

_ASLEEP = {"BLINK_SLEEP": "1"}


def test_awake_by_default_serves_the_app():
    res = client.get("/")
    assert res.status_code == 200
    assert "sleeping" not in res.text.lower()


def test_asleep_serves_the_sleep_page():
    with mock.patch.dict(os.environ, _ASLEEP):
        res = client.get("/")
    assert res.status_code == 200
    assert "Blink is sleeping" in res.text
    # The page must be inert: a static document with no script at all, so a
    # crawler's visit runs nothing and mints no orphan guest workspace.
    assert "<script" not in res.text.lower()
    assert "/v1/" not in res.text


def test_asleep_api_answers_503_in_plain_words():
    with mock.patch.dict(os.environ, _ASLEEP):
        res = client.post("/v1/workspaces/ws_sleep_test/turn",
                          json={"message": "plan my week"})
    assert res.status_code == 503
    body = res.json()
    assert body["type"] == "message"
    assert "asleep" in body["text"].lower()


def test_asleep_health_still_answers():
    with mock.patch.dict(os.environ, _ASLEEP):
        res = client.get("/_health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_awake_api_is_untouched():
    res = client.post("/v1/workspaces/ws_sleep_test/turn",
                      json={"message": "hello"})
    assert res.status_code == 200
