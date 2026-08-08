from __future__ import annotations

import os

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
    assert lease.daemon_epoch == "a" * 32
    assert lease.automatic is True
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
