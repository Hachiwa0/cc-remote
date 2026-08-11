"""Zero-token tests for durable cross-client presentation receipts."""
from __future__ import annotations

import json

import pytest

from cc_remote.wrapper.session_presentation import (
    SessionPresentationStore,
    SessionPresentationStoreError,
)


def test_completion_receipts_round_trip_and_reject_stale_acknowledgements(
    tmp_path,
):
    store = SessionPresentationStore(tmp_path)
    first = store.mark_completion("session-1", "turn-1")
    assert first.completion_unread is True
    assert first.completion_revision == 1

    # Re-emitting the same native terminal boundary is idempotent.
    assert store.mark_completion("session-1", "turn-1") == first
    second = store.mark_completion("session-1", "turn-2")
    assert second.completion_revision == 2
    assert second.completion_id == "turn-2"

    stale = store.acknowledge_completion("session-1", "turn-1")
    assert stale == second
    acknowledged = store.acknowledge_completion("session-1", "turn-2")
    assert acknowledged.completion_unread is False
    assert acknowledged.completion_revision == 3
    assert SessionPresentationStore(tmp_path).get("session-1") == acknowledged


def test_goal_dismissal_is_scoped_to_one_exact_generation(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.dismiss_goal("session-1", "goal-first")
    assert store.reconcile_goal("session-1", "goal-first") is True

    assert store.reconcile_goal("session-1", "goal-replacement") is False
    assert store.get("session-1").dismissed_goal_id is None
    store.dismiss_goal("session-1", "goal-replacement")
    assert store.reconcile_goal("session-1", None) is False
    assert store.get("session-1").dismissed_goal_id is None


def test_session_presentation_rekeys_clears_and_deletes(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.mark_completion("tmp-session", "turn-1")
    store.dismiss_goal("tmp-session", "goal-1")
    store.move("tmp-session", "real-session")
    assert store.get("tmp-session").completion_id is None
    assert store.get("real-session").completion_id == "turn-1"
    assert store.get("real-session").dismissed_goal_id == "goal-1"

    cleared = store.clear_completion("real-session")
    assert cleared.completion_id is None
    assert cleared.completion_unread is False
    assert cleared.completion_revision == 2
    store.delete("real-session")
    assert SessionPresentationStore(tmp_path).get(
        "real-session"
    ).completion_revision == 0


def test_session_presentation_rejects_unknown_payload_fields(tmp_path):
    (tmp_path / "session-presentation.json").write_text(
        json.dumps({
            "version": 1,
            "sessions": {
                "session-1": {
                    "completion_id": "turn-1",
                    "completion_unread": True,
                    "completion_revision": 1,
                    "dismissed_goal_id": None,
                    "updated_at": 1,
                    "future_secret": "must-not-pass",
                },
            },
        }),
        encoding="utf-8",
    )
    with pytest.raises(SessionPresentationStoreError):
        SessionPresentationStore(tmp_path)


def test_session_presentation_rejects_symlinks(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"sessions":{}}', encoding="utf-8")
    (tmp_path / "session-presentation.json").symlink_to(target)
    with pytest.raises(SessionPresentationStoreError):
        SessionPresentationStore(tmp_path)
