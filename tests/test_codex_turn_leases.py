from __future__ import annotations

import json
import os
import threading

from cc_remote.wrapper.codex_turn_leases import CodexTurnLeaseStore


def test_codex_turn_lease_round_trip_and_matched_release(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.claim(
        "session",
        "turn-a",
        "message",
        daemon_epoch="a" * 32,
        automatic=True,
    )

    lease = store.get("session")
    assert lease is not None
    assert lease.turn_id == "turn-a"
    assert lease.msg_id == "message"
    assert lease.initial_msg_id == "message"
    assert lease.daemon_epoch == "a" * 32
    assert lease.automatic is True
    assert lease.stream_bindings == ()
    assert store.list() == (lease,)
    assert os.stat(store.path).st_mode & 0o777 == 0o600

    assert store.release("session", turn_id="turn-b") is False
    assert store.get("session") == lease
    assert store.release("session", turn_id="turn-a") is True
    assert store.get("session") is None


def test_codex_turn_lease_rejects_corrupt_or_oversized_state(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.path.write_text('{"version":1,"leases":{"sid":{"turn_id":7}}}')
    assert store.get("sid") is None

    store.path.write_bytes(b"x" * (64 * 1024 + 1))
    assert store.get("sid") is None


def test_codex_turn_lease_reads_legacy_record_without_daemon_epoch(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.path.write_text(
        '{"version":1,"leases":{"sid":{'
        '"turn_id":"turn","msg_id":"message","updated_at":1}}}'
    )

    lease = store.get("sid")
    assert lease is not None
    assert lease.daemon_epoch is None
    assert lease.automatic is False
    assert lease.initial_msg_id is None
    assert lease.stream_bindings == ()


def test_codex_turn_lease_reads_v2_record_without_stream_binding(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.path.write_text(json.dumps({
        "version": 2,
        "leases": {
            "sid": {
                "turn_id": "control-turn",
                "msg_id": "browser-message",
                "initial_msg_id": "first-browser-message",
                "daemon_epoch": None,
                "automatic": False,
                "updated_at": 1,
            },
        },
    }))

    lease = store.get("sid")
    assert lease is not None
    assert lease.initial_msg_id == "first-browser-message"
    assert lease.stream_bindings == ()


def test_codex_turn_lease_rebind_is_compare_and_swap(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.claim(
        "session",
        "turn-a",
        "message-a",
        daemon_epoch="a" * 32,
        automatic=True,
    )

    assert store.rebind(
        "session",
        "turn-a",
        "message-b",
        expected_msg_id="message-a",
        daemon_epoch="a" * 32,
    ) is True
    rebound = store.get("session")
    assert rebound is not None
    assert rebound.msg_id == "message-b"
    assert rebound.initial_msg_id == "message-a"
    assert rebound.automatic is True

    assert store.rebind(
        "session",
        "turn-a",
        "stale-message",
        expected_msg_id="message-a",
        daemon_epoch="a" * 32,
    ) is False
    assert store.rebind(
        "session",
        "turn-a",
        "wrong-generation",
        expected_msg_id="message-b",
        daemon_epoch="b" * 32,
    ) is False
    assert store.rebind(
        "session",
        "turn-b",
        "wrong-turn",
        expected_msg_id="message-b",
        daemon_epoch="a" * 32,
    ) is False
    assert store.get("session") == rebound

    # Reliable-command replay may repeat the same accepted boundary.
    assert store.rebind(
        "session",
        "turn-a",
        "message-b",
        expected_msg_id="message-a",
        daemon_epoch="a" * 32,
    ) is True
    assert store.get("session") == rebound


def test_codex_turn_stream_binding_is_source_bound_and_compare_and_swap(
    tmp_path,
):
    store = CodexTurnLeaseStore(tmp_path)
    store.claim(
        "default@session",
        "control-turn",
        "browser-message",
        daemon_epoch="a" * 32,
    )

    assert store.bind_stream(
        "default@session",
        "control-turn",
        "rollout-task",
        "native-user-item",
        source_device=10,
        source_inode=20,
        expected_msg_id="browser-message",
        daemon_epoch="a" * 32,
    ) is True
    lease = store.get("default@session")
    assert lease is not None
    assert lease.stream_task_ids(
        source_device=10, source_inode=20,
    ) == {"rollout-task"}
    assert lease.stream_task_ids(
        source_device=10, source_inode=21,
    ) == set()

    # Idempotent reliable-command replay is accepted without duplicating state.
    assert store.bind_stream(
        "default@session",
        "control-turn",
        "rollout-task",
        "native-user-item",
        source_device=10,
        source_inode=20,
        expected_msg_id="browser-message",
        daemon_epoch="a" * 32,
    ) is True
    assert store.get("default@session") == lease

    # The same exact upstream user item cannot prove a different task, and stale
    # browser/generation owners cannot extend the binding lineage.
    assert store.bind_stream(
        "default@session",
        "control-turn",
        "other-task",
        "native-user-item",
        source_device=10,
        source_inode=20,
        expected_msg_id="browser-message",
        daemon_epoch="a" * 32,
    ) is False
    assert store.bind_stream(
        "default@session",
        "control-turn",
        "rollout-task",
        "second-native-user",
        source_device=10,
        source_inode=20,
        expected_msg_id="stale-browser-message",
        daemon_epoch="a" * 32,
    ) is False
    assert store.bind_stream(
        "default@session",
        "control-turn",
        "rollout-task",
        "second-native-user",
        source_device=10,
        source_inode=20,
        expected_msg_id="browser-message",
        daemon_epoch="b" * 32,
    ) is False
    assert store.get("default@session") == lease

    # A later accepted steer may move the browser segment while retaining the
    # exact native task lineage used after a restart.
    assert store.rebind(
        "default@session",
        "control-turn",
        "browser-message-2",
        expected_msg_id="browser-message",
        daemon_epoch="a" * 32,
    ) is True
    rebound = store.get("default@session")
    assert rebound is not None
    assert rebound.stream_bindings == lease.stream_bindings


def test_codex_turn_lease_serializes_cross_thread_read_modify_write(
    tmp_path, monkeypatch,
):
    store = CodexTurnLeaseStore(tmp_path)
    store.claim(
        "session-a",
        "control-turn-a",
        "browser-message-a",
        daemon_epoch="a" * 32,
    )

    original_read_state = store._read_state
    first_read = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_read = threading.Event()
    read_count = 0
    read_count_lock = threading.Lock()

    def gated_read_state():
        nonlocal read_count
        state = original_read_state()
        with read_count_lock:
            read_count += 1
            call_number = read_count
        if call_number == 1:
            first_read.set()
            if not release_first.wait(timeout=2):
                raise AssertionError("timed out releasing first lease read")
        elif call_number == 2:
            second_read.set()
        return state

    monkeypatch.setattr(store, "_read_state", gated_read_state)
    errors: list[BaseException] = []

    def bind_first_session():
        try:
            assert store.bind_stream(
                "session-a",
                "control-turn-a",
                "rollout-task-a",
                "native-message-a",
                source_device=10,
                source_inode=20,
                expected_msg_id="browser-message-a",
                daemon_epoch="a" * 32,
            ) is True
        except BaseException as exc:
            errors.append(exc)

    def claim_second_session():
        try:
            second_started.set()
            store.claim(
                "session-b",
                "control-turn-b",
                "browser-message-b",
                daemon_epoch="b" * 32,
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=bind_first_session)
    second = threading.Thread(target=claim_second_session)
    first.start()
    assert first_read.wait(timeout=1)
    second.start()
    assert second_started.wait(timeout=1)
    second_entered_during_transaction = second_read.wait(timeout=0.2)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert second_entered_during_transaction is False
    first_lease = store.get("session-a")
    assert first_lease is not None
    assert first_lease.stream_task_ids(
        source_device=10,
        source_inode=20,
    ) == {"rollout-task-a"}
    assert store.get("session-b") is not None
