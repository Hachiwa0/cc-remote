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
    assert stored.owner_turn_ids == frozenset({"turn-1"})
    mismatched = SessionPlanStore(tmp_path).put("mismatched-owner", TurnPlan(
        item_id="plan:wrong-turn",
        turn_id="right-turn",
        explanation=None,
        plan=[{"step": "fix", "status": "inProgress"}],
    ))
    assert mismatched.owner_turn_ids == frozenset({"right-turn"})

    restored = SessionPlanStore(tmp_path)
    assert restored.get("tmp-0123456789abcdef") == stored
    terminal = restored.mark_terminal(
        "tmp-0123456789abcdef",
        turn_id="turn-1",
        status="succeeded",
    )
    assert terminal is not None
    assert terminal.terminal_status == "succeeded"
    assert terminal.as_process_block()["done"] is True
    assert terminal.as_process_block()["plan"][1]["status"] == "inProgress"
    assert SessionPlanStore(tmp_path).get(
        "tmp-0123456789abcdef") == terminal
    restored.move("tmp-0123456789abcdef", "session-1")
    assert restored.get("tmp-0123456789abcdef") is None
    assert restored.get("session-1") == terminal

    restored.delete("session-1")
    assert SessionPlanStore(tmp_path).get("session-1") is None


def test_session_plan_store_retires_only_settled_previous_turns(tmp_path):
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

    store.put("session-1", _plan())
    assert store.mark_terminal(
        "session-1", turn_id="another-turn", status="succeeded",
    ) is None
    store.put("session-raw-item", TurnPlan(
        item_id="turn-1",
        turn_id=None,
        explanation="unbound item",
        plan=[{"step": "fix", "status": "inProgress"}],
    ))
    assert store.mark_terminal(
        "session-raw-item", turn_id="turn-1", status="succeeded",
    ) is None
    store.put("session-current-item", TurnPlan(
        item_id="plan:current",
        turn_id=None,
        explanation="unbound current plan",
        plan=[{"step": "fix", "status": "inProgress"}],
    ))
    assert store.mark_terminal(
        "session-current-item", turn_id="current", status="succeeded",
    ) is None
    store.put("session-derived-owner", TurnPlan(
        item_id="plan:derived-turn",
        turn_id=None,
        explanation=None,
        plan=[{"step": "done", "status": "completed"}],
    ))
    assert store.retire_settled(
        "session-derived-owner",
        current_turn_ids=frozenset({"derived-turn"}),
    ) is False
    assert store.retire_settled("session-1") is False
    terminal = store.mark_terminal(
        "session-1", turn_id="turn-1", status="succeeded",
    )
    assert terminal is not None
    assert terminal.complete is False
    assert terminal.settled is True
    assert store.retire_settled(
        "session-1", current_turn_ids=frozenset({"turn-1"})) is False
    assert store.retire_settled(
        "session-1", current_turn_ids=frozenset({"turn-2"})) is True


def test_session_plan_store_reads_v2_as_unsettled_and_upgrades_on_write(
    tmp_path,
):
    path = tmp_path / "session-plans.json"
    path.write_text(json.dumps({
        "version": 2,
        "profile_revision": 2,
        "plans": {
            "session-1": {
                "item_id": "plan:turn-1",
                "turn_id": "turn-1",
                "explanation": "legacy",
                "plan": [{"step": "x", "status": "inProgress"}],
                "updated_at": 1,
            },
        },
    }), encoding="utf-8")

    store = SessionPlanStore(tmp_path)
    assert store.get("session-1").terminal_status is None
    store.mark_terminal(
        "session-1", turn_id="turn-1", status="succeeded")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 3
    assert payload["plans"]["session-1"]["terminal_status"] == "succeeded"


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


def test_session_plan_profile_migration_is_replay_safe(tmp_path):
    store = SessionPlanStore(tmp_path)
    store.put("old@session-1", _plan())

    assert store.migrate_profile_sessions(
        lambda sid: sid.replace("old@", "new@", 1),
        profile_revision=4,
    ) == 1
    assert store.migrate_profile_sessions(
        lambda _sid: "must-not-run",
        profile_revision=4,
    ) == 0
    restored = SessionPlanStore(tmp_path)
    assert restored.get("old@session-1") is None
    assert restored.get("new@session-1") is not None
