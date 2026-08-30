"""P19-01: Block.gcal_event_id — the handle to the Google Calendar event WE own.

The field encodes exactly one invariant: it holds the Google Calendar event id
we created for THIS block; None means the block was never mirrored to Google
Calendar (and so must never be deleted/patched there). It must ride the
Firestore snapshot automatically via model_dump/model_validate, and it must be
backward compatible with Block snapshots serialized before the field existed.
"""

import unittest
from datetime import datetime

from src.types.entities import Block

WS = "ws_1"


def _mk_block(**overrides):
    kwargs = dict(
        id="b_1",
        workspace_id=WS,
        task_id="t_1",
        starts_at=datetime(2026, 8, 30, 9, 0, 0),
        ends_at=datetime(2026, 8, 30, 10, 0, 0),
    )
    kwargs.update(overrides)
    return Block(**kwargs)


class TestBlockGcalEventId(unittest.TestCase):
    def test_defaults_to_none(self):
        """A freshly constructed Block was never mirrored to Google Calendar."""
        b = _mk_block()
        self.assertIsNone(b.gcal_event_id)

    def test_set_id_round_trips_through_snapshot(self):
        """model_dump -> model_validate preserves the owned event id, exactly
        like every other field (the store persists Blocks this way)."""
        b = _mk_block(gcal_event_id="google_evt_abc123")
        snapshot = b.model_dump()
        self.assertEqual(snapshot["gcal_event_id"], "google_evt_abc123")

        restored = Block.model_validate(snapshot)
        self.assertEqual(restored.gcal_event_id, "google_evt_abc123")

    def test_legacy_snapshot_without_field_is_backward_compatible(self):
        """A Block dict serialized before P19-01 (no gcal_event_id key) still
        validates and lands None — the block is treated as un-mirrored."""
        legacy = _mk_block().model_dump()
        legacy.pop("gcal_event_id", None)
        self.assertNotIn("gcal_event_id", legacy)

        restored = Block.model_validate(legacy)
        self.assertIsNone(restored.gcal_event_id)


if __name__ == "__main__":
    unittest.main()
