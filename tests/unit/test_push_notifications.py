"""
Server-side push for the companion (P15-10): registration, the APNs gateway,
and the five-minute sweep.

FULLY OFFLINE. Every test drives the real decision path with `push.set_client`
holding a fake HTTP/2 client, and the JWT tests generate their OWN P-256 key
locally. No Apple key, no network, no device, nothing to spend.
"""
import json
import os
import unittest
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.agent import auth as blink_auth
from src.agent import push, push_scheduler
from src.agent import workspace_registry
from src.api import server
from src.sim.fake_store import FakeStore
from src.types.entities import Block, Task

SECRET = "test-session-secret-for-push"
SUB = "push-user-1"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def _user_ws():
    os.environ["BLINK_SESSION_SECRET"] = SECRET
    return blink_auth.user_workspace_id(SUB)


def _auth(workspace_id):
    return {"Authorization": f"Bearer {blink_auth.make_bearer_token(workspace_id)}"}


class _FakeApns:
    """Records every request and answers from a scripted queue.

    Default answer is 200 with an empty body, which is exactly what APNs sends
    on success.
    """

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls = []

    def post(self, url, *, headers=None, body=b""):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "payload": json.loads(body.decode("utf-8"))})
        if self.answers:
            return self.answers.pop(0)
        return 200, {}


def _test_key_pem():
    """A P-256 private key generated HERE, for this test run only.

    Deliberately not a real Apple key and not a fixture on disk: this proves
    the signer works without any secret existing anywhere in the repo.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii"), key.public_key()


TEST_CONFIG = None  # filled in by setUpModule


def setUpModule():
    global TEST_CONFIG
    pem, _ = _test_key_pem()
    TEST_CONFIG = push.ApnsConfig(
        key_pem=pem, key_id="ABC1234567", team_id="W893S8L2T5",
        topic="dev.oapps.blink.companion",
    )


def _clean():
    push.set_client(None)
    push._token_cache.clear()
    server.stores.clear()
    workspace_registry.reset_persistence_state()
    os.environ.pop("BLINK_SESSION_SECRET", None)
    os.environ.pop("BLINK_SWEEP_SECRET", None)


def _store_with_block(workspace_id="ws_push", tz="America/Los_Angeles",
                      starts_at=None, minutes=60, title="Rehearse the talk"):
    store = FakeStore(workspace_id=workspace_id)
    store.update_profile(timezone=tz)
    if starts_at is not None:
        task = Task(id="t1", workspace_id=workspace_id, commitment_id="c1",
                    title=title, estimated_minutes=minutes)
        store.tasks[task.id] = task
        store.blocks["b1"] = Block(
            id="b1", workspace_id=workspace_id, task_id="t1",
            starts_at=starts_at, ends_at=starts_at + timedelta(minutes=minutes),
        )
    return store


# --- Gap 2: registration ----------------------------------------------------

class TestDeviceRegistration(unittest.TestCase):
    def setUp(self):
        _clean()
        self.ws = _user_ws()
        self.client = TestClient(server.app)

    def tearDown(self):
        _clean()

    def test_register_then_list_then_delete(self):
        res = self.client.post(f"/v1/workspaces/{self.ws}/devices",
                               json={"apns_token": TOKEN_A, "environment": "sandbox",
                                     "platform": "ios", "app_version": "1.0"},
                               headers=_auth(self.ws))
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["devices"], 1)
        self.assertEqual(res.json()["environment"], "sandbox")

        listed = self.client.get(f"/v1/workspaces/{self.ws}/devices",
                                 headers=_auth(self.ws)).json()["devices"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["fingerprint"], push.token_fingerprint(TOKEN_A))
        # The response must never echo the token back.
        self.assertNotIn(TOKEN_A, json.dumps(listed))

        gone = self.client.delete(f"/v1/workspaces/{self.ws}/devices/{TOKEN_A}",
                                  headers=_auth(self.ws))
        self.assertEqual(gone.status_code, 200)
        self.assertTrue(gone.json()["removed"])
        self.assertEqual(gone.json()["devices"], 0)

    def test_registering_the_same_token_twice_replaces_it(self):
        for version in ("1.0", "1.1"):
            self.client.post(f"/v1/workspaces/{self.ws}/devices",
                             json={"apns_token": TOKEN_A, "app_version": version},
                             headers=_auth(self.ws))
        store = server.stores[self.ws]
        self.assertEqual(len(store.devices), 1)
        self.assertEqual(store.devices[TOKEN_A]["app_version"], "1.1")

    def test_multiple_devices_coexist(self):
        for token in (TOKEN_A, TOKEN_B):
            self.client.post(f"/v1/workspaces/{self.ws}/devices",
                             json={"apns_token": token}, headers=_auth(self.ws))
        self.assertEqual(len(server.stores[self.ws].devices), 2)

    def test_deleting_an_unknown_token_reports_the_truth(self):
        res = self.client.delete(f"/v1/workspaces/{self.ws}/devices/{TOKEN_B}",
                                 headers=_auth(self.ws))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["removed"])

    def test_a_stranger_cannot_register_or_read_devices(self):
        for call in (
            lambda: self.client.post(f"/v1/workspaces/{self.ws}/devices",
                                     json={"apns_token": TOKEN_A}),
            lambda: self.client.get(f"/v1/workspaces/{self.ws}/devices"),
            lambda: self.client.delete(f"/v1/workspaces/{self.ws}/devices/{TOKEN_A}"),
        ):
            self.assertEqual(call().status_code, 403)

    def test_a_guest_workspace_id_is_not_a_credential(self):
        """The tenancy gate lets guest ids through for reading a plan. A device
        token is a delivery address, so registration needs a real session."""
        res = self.client.post("/v1/workspaces/g_someguest/devices",
                               json={"apns_token": TOKEN_A})
        self.assertEqual(res.status_code, 403)

    def test_devices_ride_the_snapshot(self):
        from src.agent import persistence

        store = FakeStore(workspace_id="ws_snap")
        store.register_device(TOKEN_A, environment="sandbox")
        store.notification_day = "2026-08-29"
        restored = persistence.restore(FakeStore(workspace_id="ws_snap"),
                                       persistence.snapshot(store))
        self.assertEqual(list(restored.devices), [TOKEN_A])
        self.assertEqual(restored.devices[TOKEN_A]["environment"], "sandbox")
        self.assertEqual(restored.notification_day, "2026-08-29")


# --- the JWT ----------------------------------------------------------------

class TestProviderToken(unittest.TestCase):
    def setUp(self):
        _clean()

    def tearDown(self):
        _clean()

    def test_signs_a_wellformed_es256_token_a_verifier_accepts(self):
        import base64

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils

        pem, public_key = _test_key_pem()
        token = push.sign_jwt(pem, "ABC1234567", "W893S8L2T5", issued_at=1700000000)
        header_b64, claims_b64, sig_b64 = token.split(".")

        def unpad(raw):
            return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

        self.assertEqual(json.loads(unpad(header_b64)),
                         {"alg": "ES256", "kid": "ABC1234567", "typ": "JWT"})
        self.assertEqual(json.loads(unpad(claims_b64)),
                         {"iss": "W893S8L2T5", "iat": 1700000000})
        raw_sig = unpad(sig_b64)
        self.assertEqual(len(raw_sig), 64)  # JWS wants r||s, 32 bytes each
        der = asym_utils.encode_dss_signature(
            int.from_bytes(raw_sig[:32], "big"), int.from_bytes(raw_sig[32:], "big")
        )
        # Raises on a bad signature; reaching the next line IS the assertion.
        public_key.verify(der, f"{header_b64}.{claims_b64}".encode("ascii"),
                          ec.ECDSA(hashes.SHA256()))

    def test_the_token_is_cached_and_refreshed_at_forty_minutes(self):
        pem, _ = _test_key_pem()
        first = push._token_cache.get(pem, "K", "T", now=1000.0)
        self.assertEqual(first, push._token_cache.get(pem, "K", "T", now=1000.0 + 39 * 60))
        self.assertNotEqual(
            first, push._token_cache.get(pem, "K", "T", now=1000.0 + 41 * 60))

    def test_a_non_pem_key_is_push_unavailable_not_a_crash(self):
        with self.assertRaises(push.PushUnavailable):
            push.sign_jwt("not a key", "K", "T")


# --- the payload contract ---------------------------------------------------

class TestPayloadMatchesTheClientContract(unittest.TestCase):
    def test_category_and_userinfo_key_match_the_swift_strings(self):
        payload = push.build_payload("nudge", "x starts in ten minutes.",
                                     block_id="b1", task_title="x")
        # SignalKind.categoryIdentifier -> "blink.signal.\(rawValue)"
        self.assertEqual(payload["aps"]["category"], "blink.signal.nudge")
        # SignalContext.userInfoKey
        self.assertIn("blink_signal", payload)
        self.assertEqual(payload["blink_signal"],
                         {"block_id": "b1", "task_title": "x"})

    def test_every_kind_the_client_knows_has_a_category(self):
        for kind in ("nudge", "morning_brief", "check_in", "insight"):
            self.assertEqual(push.build_payload(kind, "body")["aps"]["category"],
                             f"blink.signal.{kind}")

    def test_absent_context_members_are_omitted_not_null(self):
        payload = push.build_payload("morning_brief", "body")
        self.assertEqual(payload["blink_signal"], {})

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            push.build_payload("pep_talk", "you got this")


# --- the sweep --------------------------------------------------------------

class TestSweepPicksTheRightKind(unittest.TestCase):
    """Every case runs in America/Los_Angeles (UTC-7 in August), so a UTC-day
    or UTC-hour bug shows up rather than passing by coincidence."""

    def setUp(self):
        _clean()
        self.apns = _FakeApns()
        push.set_client(self.apns)

    def tearDown(self):
        _clean()

    def _sweep(self, store, now, brief=None):
        return push_scheduler.sweep_workspace(
            store, now, brief_body_for=brief, config=TEST_CONFIG)

    def test_nudge_fires_ten_minutes_before_a_session_in_local_time(self):
        # 09:10 UTC == 02:10 Los Angeles. The block starts at 09:20 UTC.
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=10))
        store.register_device(TOKEN_A)
        result = self._sweep(store, now)
        self.assertEqual(result.kind, "nudge")
        self.assertTrue(result.sent)
        body = self.apns.calls[0]["payload"]["aps"]["alert"]["body"]
        self.assertEqual(body, "Rehearse the talk starts in ten minutes.")

    def test_the_nudge_says_the_real_number_of_minutes(self):
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=7))
        store.register_device(TOKEN_A)
        self._sweep(store, now)
        self.assertEqual(self.apns.calls[0]["payload"]["aps"]["alert"]["body"],
                         "Rehearse the talk starts in seven minutes.")

    def test_morning_brief_only_before_ten_am_local(self):
        # 15:00 UTC == 08:00 Los Angeles: inside the window.
        morning = datetime(2026, 8, 29, 15, 0)
        store = _store_with_block(starts_at=morning + timedelta(hours=3))
        store.register_device(TOKEN_A)
        result = self._sweep(store, morning, brief=lambda s, n: "Good morning. Two blocks.")
        self.assertEqual(result.kind, "morning_brief")
        self.assertEqual(self.apns.calls[0]["payload"]["aps"]["alert"]["body"],
                         "Good morning. Two blocks. First at 11:00 AM.")

    def test_the_same_utc_hour_is_too_late_in_a_different_zone(self):
        """15:00 UTC is 08:00 in Los Angeles and 18:00 in Nairobi. Same instant,
        different verdict, which is the entire reason localtime.py exists."""
        morning = datetime(2026, 8, 29, 15, 0)
        store = _store_with_block(tz="Africa/Nairobi",
                                  starts_at=morning + timedelta(hours=3))
        store.register_device(TOKEN_A)
        result = self._sweep(store, morning, brief=lambda s, n: "Good morning.")
        self.assertNotEqual(result.kind, "morning_brief")

    def test_no_brief_when_today_has_no_sessions(self):
        store = _store_with_block(starts_at=None)
        store.register_device(TOKEN_A)
        result = self._sweep(store, datetime(2026, 8, 29, 15, 0),
                             brief=lambda s, n: "Good morning.")
        self.assertFalse(result.sent)
        self.assertEqual(result.skipped, "nothing due")

    def test_check_in_after_five_pm_local_for_an_ended_block(self):
        # 03:00 UTC on the 30th == 20:00 Los Angeles on the 29th. A UTC-day
        # comparison here finds nothing at all.
        now = datetime(2026, 8, 30, 3, 0)
        store = _store_with_block(starts_at=datetime(2026, 8, 30, 1, 0))
        store.register_device(TOKEN_A)
        result = self._sweep(store, now)
        self.assertEqual(result.kind, "check_in")
        self.assertEqual(self.apns.calls[0]["payload"]["aps"]["alert"]["body"],
                         "How did Rehearse the talk go?")

    def test_no_check_in_before_the_check_in_hour(self):
        # 20:00 UTC == 13:00 Los Angeles, block already ended but it is early.
        now = datetime(2026, 8, 29, 20, 0)
        store = _store_with_block(starts_at=datetime(2026, 8, 29, 18, 0))
        store.register_device(TOKEN_A)
        self.assertFalse(self._sweep(store, now).sent)

    def test_a_workspace_with_no_devices_is_skipped(self):
        store = _store_with_block(starts_at=datetime(2026, 8, 29, 9, 20))
        result = self._sweep(store, datetime(2026, 8, 29, 9, 10))
        self.assertEqual(result.skipped, "no devices")
        self.assertEqual(self.apns.calls, [])

    def test_sandbox_and_production_tokens_go_to_different_hosts(self):
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=10))
        store.register_device(TOKEN_A, environment="sandbox")
        store.register_device(TOKEN_B, environment="production")
        self._sweep(store, now)
        hosts = sorted(call["url"].split("/")[2] for call in self.apns.calls)
        self.assertEqual(hosts, [push.PRODUCTION_HOST, push.SANDBOX_HOST])


class TestBudgetAndGap(unittest.TestCase):
    def setUp(self):
        _clean()
        self.apns = _FakeApns()
        push.set_client(self.apns)

    def tearDown(self):
        _clean()

    def _store_with_three_blocks(self):
        store = _store_with_block(starts_at=None)
        for index in range(4):
            task = Task(id=f"t{index}", workspace_id=store.workspace_id,
                        commitment_id="c1", title=f"Task {index}",
                        estimated_minutes=30)
            store.tasks[task.id] = task
            start = datetime(2026, 8, 29, 9, 20) + timedelta(hours=index)
            store.blocks[f"b{index}"] = Block(
                id=f"b{index}", workspace_id=store.workspace_id,
                task_id=task.id, starts_at=start,
                ends_at=start + timedelta(minutes=30))
        store.register_device(TOKEN_A)
        return store

    def _sweep_at(self, store, now):
        return push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)

    def test_each_send_decrements_the_budget_exactly_once(self):
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=10))
        store.register_device(TOKEN_A)
        store.register_device(TOKEN_B)   # two devices, ONE signal
        self._sweep_at(store, now)
        self.assertEqual(len(self.apns.calls), 2)
        self.assertEqual(store.notification_budget, 2)
        self.assertEqual(len(store.notifications_sent), 1)

    def test_the_fourth_signal_of_the_day_is_blocked(self):
        store = self._store_with_three_blocks()
        kinds = []
        for index in range(4):
            now = datetime(2026, 8, 29, 9, 10) + timedelta(hours=index)
            result = self._sweep_at(store, now)
            kinds.append(result.sent)
        self.assertEqual(kinds, [True, True, True, False])
        self.assertEqual(store.notification_budget, 0)
        self.assertEqual(len(store.notifications_sent), 3)
        self.assertEqual(len(self.apns.calls), 3)

    def test_never_two_sends_within_fifteen_minutes(self):
        store = self._store_with_three_blocks()
        # Two blocks whose nudge windows are minutes apart.
        store.blocks["b1"].starts_at = datetime(2026, 8, 29, 9, 25)
        store.blocks["b1"].ends_at = datetime(2026, 8, 29, 9, 55)
        store.blocks["b2"].starts_at = datetime(2026, 8, 29, 9, 50)
        store.blocks["b2"].ends_at = datetime(2026, 8, 29, 10, 20)
        first = self._sweep_at(store, datetime(2026, 8, 29, 9, 10))
        second = self._sweep_at(store, datetime(2026, 8, 29, 9, 15))
        third = self._sweep_at(store, datetime(2026, 8, 29, 9, 40))
        self.assertTrue(first.sent)
        self.assertFalse(second.sent)
        self.assertEqual(second.skipped, "within the 15 minute gap")
        self.assertTrue(third.sent)

    def test_the_same_signal_is_never_sent_twice_in_a_local_day(self):
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=10))
        store.register_device(TOKEN_A)
        self.assertTrue(self._sweep_at(store, now).sent)
        # 20 minutes later the gap has passed, but the block is the same one.
        later = self._sweep_at(store, now + timedelta(minutes=20))
        self.assertFalse(later.sent)

    def test_the_budget_rolls_over_on_the_users_local_day_not_utc(self):
        store = _store_with_block(starts_at=None)
        store.register_device(TOKEN_A)
        # 09:00 UTC on the 29th is still the 28th in Los Angeles.
        push_scheduler.roll_budget_if_new_day(
            store, datetime(2026, 8, 29, 9, 0),
            push_scheduler.resolve_zone("America/Los_Angeles"))
        self.assertEqual(store.notification_day, "2026-08-29")
        store.notification_budget = 0
        # Same UTC day, and still the same LOCAL day: no free refill.
        rolled = push_scheduler.roll_budget_if_new_day(
            store, datetime(2026, 8, 29, 20, 0),
            push_scheduler.resolve_zone("America/Los_Angeles"))
        self.assertFalse(rolled)
        self.assertEqual(store.notification_budget, 0)
        # Past local midnight, the budget comes back.
        self.assertTrue(push_scheduler.roll_budget_if_new_day(
            store, datetime(2026, 8, 30, 8, 0),
            push_scheduler.resolve_zone("America/Los_Angeles")))
        self.assertEqual(store.notification_budget, 3)


class TestFailureIsNeverAClaim(unittest.TestCase):
    def setUp(self):
        _clean()

    def tearDown(self):
        _clean()

    def _nudge_store(self):
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=10))
        store.register_device(TOKEN_A)
        return store, now

    def test_a_410_prunes_the_token(self):
        push.set_client(_FakeApns([(410, {"reason": "Unregistered"})]))
        store, now = self._nudge_store()
        result = push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
        self.assertEqual(result.pruned, 1)
        self.assertEqual(store.devices, {})
        self.assertFalse(result.sent)

    def test_a_bad_device_token_prunes_too(self):
        push.set_client(_FakeApns([(400, {"reason": "BadDeviceToken"})]))
        store, now = self._nudge_store()
        push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
        self.assertEqual(store.devices, {})

    def test_a_failed_send_neither_decrements_nor_claims_delivery(self):
        push.set_client(_FakeApns([(400, {"reason": "PayloadTooLarge"})]))
        store, now = self._nudge_store()
        result = push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
        self.assertFalse(result.sent)
        self.assertEqual(store.notification_budget, 3)
        self.assertEqual(store.notifications_sent, [])
        # The device survives: the payload was wrong, not the token.
        self.assertEqual(len(store.devices), 1)

    def test_one_device_accepting_is_enough_to_spend_the_budget_once(self):
        push.set_client(_FakeApns([(200, {}), (410, {"reason": "Unregistered"})]))
        store, now = self._nudge_store()
        store.register_device(TOKEN_B)
        result = push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
        self.assertTrue(result.sent)
        self.assertEqual(store.notification_budget, 2)
        self.assertEqual(len(store.notifications_sent), 1)
        self.assertEqual(len(store.devices), 1)

    def test_an_unconfigured_gateway_spends_nothing(self):
        push.set_client(_FakeApns())
        store, now = self._nudge_store()
        os.environ.pop("APNS_KEY_P8", None)
        result = push_scheduler.sweep_workspace(store, now)  # no config: env is empty
        self.assertFalse(result.sent)
        self.assertEqual(result.skipped, "push unavailable")
        self.assertEqual(store.notification_budget, 3)
        self.assertEqual(store.notifications_sent, [])

    def test_a_transport_error_is_push_unavailable_not_a_false_success(self):
        class _Broken:
            def post(self, *a, **k):
                raise OSError("connection reset")

        push.set_client(_Broken())
        store, now = self._nudge_store()
        result = push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
        self.assertFalse(result.sent)
        self.assertEqual(store.notification_budget, 3)


class TestLogsCarryNoSecrets(unittest.TestCase):
    def setUp(self):
        _clean()
        push.set_client(_FakeApns())

    def tearDown(self):
        _clean()

    def test_no_log_line_contains_a_token_or_copy(self):
        import io
        import contextlib

        ws = _user_ws()
        client = TestClient(server.app)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            client.post(f"/v1/workspaces/{ws}/devices",
                        json={"apns_token": TOKEN_A}, headers=_auth(ws))
            store = server.stores[ws]
            store.update_profile(timezone="America/Los_Angeles")
            now = datetime(2026, 8, 29, 9, 10)
            task = Task(id="t1", workspace_id=ws, commitment_id="c1",
                        title="Rehearse the talk", estimated_minutes=60)
            store.tasks["t1"] = task
            store.blocks["b1"] = Block(id="b1", workspace_id=ws, task_id="t1",
                                       starts_at=now + timedelta(minutes=10),
                                       ends_at=now + timedelta(minutes=70))
            push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
            client.delete(f"/v1/workspaces/{ws}/devices/{TOKEN_A}",
                          headers=_auth(ws))
        logged = buffer.getvalue()
        self.assertNotIn(TOKEN_A, logged)
        self.assertNotIn("Rehearse the talk", logged)     # never the copy
        self.assertNotIn("starts in ten minutes", logged)
        self.assertIn("sent nudge", logged)                # but the DECISION is there
        self.assertIn(push.token_fingerprint(TOKEN_A), logged)

    def test_the_ledger_row_carries_no_copy_and_no_token(self):
        now = datetime(2026, 8, 29, 9, 10)
        store = _store_with_block(starts_at=now + timedelta(minutes=10))
        store.register_device(TOKEN_A)
        push_scheduler.sweep_workspace(store, now, config=TEST_CONFIG)
        row = store.notifications_sent[0]
        self.assertEqual(row["kind"], "nudge")
        self.assertNotIn("body", row)
        blob = json.dumps(row)
        self.assertNotIn(TOKEN_A, blob)
        self.assertNotIn("Rehearse", blob)


# --- the internal endpoint --------------------------------------------------

class TestSweepEndpointIsNotPublic(unittest.TestCase):
    def setUp(self):
        _clean()
        push.set_client(_FakeApns())
        self.client = TestClient(server.app)

    def tearDown(self):
        _clean()

    def test_no_secret_configured_means_nobody_can_fire_it(self):
        self.assertEqual(self.client.post("/internal/sweep").status_code, 403)
        self.assertEqual(
            self.client.post("/internal/sweep",
                             headers={"X-Blink-Sweep-Secret": ""}).status_code, 403)

    def test_a_wrong_secret_is_refused(self):
        os.environ["BLINK_SWEEP_SECRET"] = "the-real-one"
        self.assertEqual(
            self.client.post("/internal/sweep",
                             headers={"X-Blink-Sweep-Secret": "guess"}).status_code, 403)

    def test_the_right_secret_sweeps_and_reports_counts(self):
        os.environ["BLINK_SWEEP_SECRET"] = "the-real-one"
        res = self.client.post("/internal/sweep",
                               headers={"X-Blink-Sweep-Secret": "the-real-one"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["sent"], 0)
        self.assertIn("swept_at", body)
        self.assertFalse(body["push_configured"])

    def test_the_sweep_walks_only_workspaces_with_devices(self):
        os.environ["BLINK_SWEEP_SECRET"] = "the-real-one"
        now = datetime(2026, 8, 29, 9, 10)
        with_device = _store_with_block(workspace_id="ws_one",
                                        starts_at=now + timedelta(minutes=10))
        with_device.register_device(TOKEN_A)
        server.stores["ws_one"] = with_device
        server.stores["ws_two"] = _store_with_block(
            workspace_id="ws_two", starts_at=now + timedelta(minutes=10))
        report = push_scheduler.sweep(server.stores, now, config=TEST_CONFIG)
        self.assertEqual(report.workspaces, 2)
        self.assertEqual(report.considered, 1)
        self.assertEqual(report.sent, 1)
        self.assertEqual(report.by_kind, {"nudge": 1})


if __name__ == "__main__":
    unittest.main()
