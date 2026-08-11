"""Zero-token tests for durable Codex plan snapshots."""
from __future__ import annotations

import json

import pytest

from cc_remote.protocol import TurnPlan
from cc_remote.wrapper.session_plans import (
    SessionPlanStore,
    SessionPlanStoreError,
)


def _plan() -> TurnPlan:
    return TurnPlan(
        item_id="plan:turn-1",
        turn_id="turn-1",
        explanation="ship it",
        plan=[
            {"step": "inspect", "status": "completed"},
            {"step": "fix", "status": "inProgress"},
        ],
    )


def test_session_plan_store_round_trips_rekeys_and_deletes(tmp_path):
    store = SessionPlanStore(tmp_path)
    stored = store.put("tmp-0123456789abcdef", _plan())
    assert stored.as_event().plan[1]["status"] == "inProgress"

    restored = SessionPlanStore(tmp_path)
    assert restored.get("tmp-0123456789abcdef") == stored
    restored.move("tmp-0123456789abcdef", "session-1")
    assert restored.get("tmp-0123456789abcdef") is None
    assert restored.get("session-1") == stored

    restored.delete("session-1")
    assert SessionPlanStore(tmp_path).get("session-1") is None


def test_session_plan_store_retires_only_completed_previous_turns(tmp_path):
    store = SessionPlanStore(tmp_path)
    store.put("session-1", _plan())
    assert store.retire_completed("session-1") is False
    assert store.get("session-1") is not None

    completed = TurnPlan(
        item_id="plan:turn-1",
        turn_id="turn-1",
        explanation="done",
        plan=[{"step": "ship", "status": "completed"}],
    )
    store.put("session-1", completed)
    assert store.retire_completed(
        "session-1", current_turn_ids=frozenset({"turn-1"})) is False
    assert store.get("session-1") is not None

    assert store.retire_completed(
        "session-1", current_turn_ids=frozenset({"turn-2"})) is True
    assert SessionPlanStore(tmp_path).get("session-1") is None


def test_session_plan_store_rejects_unbounded_or_unknown_payloads(tmp_path):
    path = tmp_path / "session-plans.json"
    path.write_text(json.dumps({
        "version": 1,
        "plans": {
            "session-1": {
                "item_id": "plan-1",
                "turn_id": "turn-1",
                "explanation": None,
                "plan": [{"step": "x", "status": "future"}],
                "updated_at": 1,
                "future_secret": "must-not-pass",
            },
        },
    }), encoding="utf-8")

    with pytest.raises(SessionPlanStoreError):
        SessionPlanStore(tmp_path)


def test_session_plan_store_rejects_symlinks(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"plans":{}}', encoding="utf-8")
    (tmp_path / "session-plans.json").symlink_to(target)

    with pytest.raises(SessionPlanStoreError):
        SessionPlanStore(tmp_path)
