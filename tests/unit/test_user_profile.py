# tests/unit/test_user_profile.py
from src.types.entities import UserProfile
from src.sim.fake_store import FakeStore


def test_user_profile_roundtrips():
    profile = UserProfile(
        workspace_id="ws_test",
        platforms=["Coursera", "DataCamp"],
        current_level="beginner",
        hours_per_week=6,
        target_timeline="6 months",
        notes="wants to switch into ML",
    )
    dumped = profile.model_dump(mode="json")
    restored = UserProfile.model_validate(dumped)
    assert restored.workspace_id == "ws_test"
    assert restored.platforms == ["Coursera", "DataCamp"]
    assert restored.current_level == "beginner"
    assert restored.hours_per_week == 6
    assert restored.target_timeline == "6 months"
    assert restored.notes == "wants to switch into ML"


def test_fresh_store_returns_default_empty_profile():
    store = FakeStore(workspace_id="ws_test")
    profile = store.get_profile()
    assert profile.workspace_id == "ws_test"
    assert profile.platforms == []
    assert profile.current_level is None
    assert profile.hours_per_week is None
    assert profile.target_timeline is None
    assert profile.notes is None


def test_update_profile_merges_platforms_and_preserves_scalars():
    store = FakeStore(workspace_id="ws_test")
    store.update_profile(platforms=["Coursera"], hours_per_week=6)
    store.update_profile(platforms=["DataCamp"])
    profile = store.get_profile()
    assert profile.platforms == ["Coursera", "DataCamp"]  # merged, deduped
    assert profile.hours_per_week == 6  # preserved across updates


def test_update_profile_dedupes_repeated_platforms():
    store = FakeStore(workspace_id="ws_test")
    store.update_profile(platforms=["Coursera"])
    store.update_profile(platforms=["Coursera", "DataCamp"])
    assert store.get_profile().platforms == ["Coursera", "DataCamp"]
