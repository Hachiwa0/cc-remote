from __future__ import annotations

import json
import os

import pytest

from cc_remote.wrapper.codex_client_messages import (
    CodexClientMessageStore,
    CodexClientMessageStoreError,
)


def _rollout(tmp_path, name: str = "rollout.jsonl"):
    path = tmp_path / name
    path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    return path


def test_codex_client_message_alias_round_trip_and_upgrade(tmp_path):
    rollout = _rollout(tmp_path)
    store = CodexClientMessageStore(tmp_path / "state")

    assert store.put(
        rollout,
        "native-turn",
        "browser-first",
        segment_index=0,
    ) is True
    assert store.put(
        rollout,
        "native-turn",
        "browser-first",
        native_message_id="native-user-first",
    ) is True
    assert store.put(
        rollout,
        "native-turn",
        "browser-steer",
        native_message_id="native-user-steer",
    ) is True

    aliases = store.get(rollout)
    rollout_info = rollout.stat()
    assert aliases.matches_source(
        str(rollout.resolve()), rollout_info.st_dev, rollout_info.st_ino)
    assert aliases.resolve(
        "native-turn", 0, "native-user-first") == "browser-first"
    assert aliases.resolve(
        "native-turn", 1, "native-user-steer") == "browser-steer"
    assert aliases.resolve("native-turn", 0) == "browser-first"
    assert store.put(
        rollout,
        "native-turn",
        "browser-first",
        segment_index=0,
        native_message_id="native-user-first",
    ) is False
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_codex_client_message_alias_accepts_live_and_history_native_ids(
    tmp_path,
):
    rollout = _rollout(tmp_path)
    store = CodexClientMessageStore(tmp_path / "state")

    assert store.put(
        rollout,
        "native-turn",
        "browser-first",
        segment_index=0,
        native_message_id="live-native-user",
    ) is True
    assert store.put(
        rollout,
        "native-turn",
        "browser-first",
        native_message_id="msg_official_native_user",
    ) is True
    assert store.put(
        rollout,
        "browser-steer-turn",
        "browser-steer",
        native_message_id="live-native-steer",
    ) is True

    aliases = store.get(rollout)
    assert aliases.native_messages == {
        "live-native-user": "browser-first",
        "msg_official_native_user": "browser-first",
        "live-native-steer": "browser-steer",
    }
    assert aliases.segments == {("native-turn", 0): "browser-first"}
    assert aliases.resolve(
        "native-turn", 0, "live-native-user") == "browser-first"
    assert aliases.resolve(
        "native-turn", 0, "msg_official_native_user") == "browser-first"
    assert store.put(
        rollout,
        "native-turn",
        "browser-first",
        segment_index=0,
        native_message_id="msg_official_native_user",
    ) is False


def test_codex_client_message_alias_is_source_inode_bound(tmp_path):
    rollout = _rollout(tmp_path)
    store = CodexClientMessageStore(tmp_path / "state")
    store.put(
        rollout,
        "native-turn",
        "browser-message",
        segment_index=0,
    )

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    os.replace(replacement, rollout)

    aliases = store.get(rollout)
    assert aliases.native_messages == {}
    assert aliases.segments == {}
    replacement_info = rollout.stat()
    assert aliases.matches_source(
        str(rollout.resolve()),
        replacement_info.st_dev,
        replacement_info.st_ino,
    )


def test_codex_client_message_alias_rejects_conflicting_identity(tmp_path):
    rollout = _rollout(tmp_path)
    store = CodexClientMessageStore(tmp_path / "state")
    store.put(
        rollout,
        "native-turn",
        "browser-first",
        segment_index=0,
        native_message_id="native-user",
    )

    with pytest.raises(CodexClientMessageStoreError):
        store.put(
            rollout,
            "native-turn",
            "different-browser-message",
            segment_index=0,
        )
    with pytest.raises(CodexClientMessageStoreError):
        store.put(
            rollout,
            "native-turn",
            "different-browser-message",
            native_message_id="native-user",
        )
    with pytest.raises(CodexClientMessageStoreError):
        store.put(
            rollout,
            "native-turn",
            "browser-first",
            segment_index=1,
        )
    with pytest.raises(CodexClientMessageStoreError):
        store.put(
            rollout,
            "different-native-turn",
            "browser-first",
            native_message_id="native-user",
        )


def test_codex_client_message_alias_rejects_corrupt_or_public_store(tmp_path):
    rollout = _rollout(tmp_path)
    store = CodexClientMessageStore(tmp_path / "state")
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"version": 1, "sources": "bad"}),
        encoding="utf-8",
    )
    os.chmod(store.path, 0o600)
    with pytest.raises(CodexClientMessageStoreError):
        store.get(rollout)

    store.path.write_text(
        json.dumps({"version": 1, "sources": []}),
        encoding="utf-8",
    )
    os.chmod(store.path, 0o644)
    with pytest.raises(CodexClientMessageStoreError):
        store.get(rollout)


def test_codex_client_message_alias_delete_uses_captured_path(tmp_path):
    rollout = _rollout(tmp_path)
    store = CodexClientMessageStore(tmp_path / "state")
    store.put(
        rollout,
        "native-turn",
        "browser-message",
        segment_index=0,
    )
    rollout.unlink()

    assert store.delete_path(rollout) is True
    assert not store.path.exists()
