from __future__ import annotations

import json
import os

import pytest

from cc_remote.wrapper.claude_client_messages import (
    ClaudeClientMessageStore,
    ClaudeClientMessageStoreError,
)


SESSION_ID = "fa800ca3-18e3-4391-b401-a33fe52e2f56"
NATIVE_ID = "2259073b-7676-455f-b7b0-b9b3892dbe93"
CLIENT_ID = "6b09ee37-f861-4422-b98a-21f509c951b0"


def test_client_message_alias_survives_reopen_and_append(tmp_path):
    source = tmp_path / f"{SESSION_ID}.jsonl"
    source.write_text("{}\n")
    state = tmp_path / "state"

    store = ClaudeClientMessageStore(state)
    store.put(SESSION_ID, source, NATIVE_ID, CLIENT_ID)
    source.write_text("{}\n{}\n")

    reopened = ClaudeClientMessageStore(state)
    assert reopened.get(SESSION_ID, source) == {NATIVE_ID: CLIENT_ID}
    record = next((state / "claude-client-message-ids").glob("*.json"))
    assert oct(record.stat().st_mode & 0o777) == "0o600"
    assert CLIENT_ID in record.read_text()
    assert "prompt" not in record.read_text().lower()


def test_client_message_alias_is_bound_to_exact_transcript_inode(tmp_path):
    source = tmp_path / f"{SESSION_ID}.jsonl"
    source.write_text("old\n")
    store = ClaudeClientMessageStore(tmp_path / "state")
    store.put(SESSION_ID, source, NATIVE_ID, CLIENT_ID)

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("new\n")
    os.replace(replacement, source)

    assert store.get(SESSION_ID, source) == {}
    next_native = "3259073b-7676-455f-b7b0-b9b3892dbe93"
    next_client = "7b09ee37-f861-4422-b98a-21f509c951b0"
    store.put(SESSION_ID, source, next_native, next_client)
    assert store.get(SESSION_ID, source) == {next_native: next_client}


def test_client_message_aliases_are_exact_bounded_and_deletable(tmp_path):
    source = tmp_path / f"{SESSION_ID}.jsonl"
    source.write_text("{}\n")
    store = ClaudeClientMessageStore(tmp_path / "state", max_aliases=2)
    pairs = [
        (
            f"{index}259073b-7676-455f-b7b0-b9b3892dbe93",
            f"{index}b09ee37-f861-4422-b98a-21f509c951b0",
        )
        for index in (1, 2, 3)
    ]
    for native_id, client_id in pairs:
        store.put(SESSION_ID, source, native_id, client_id)

    assert store.get(SESSION_ID, source) == dict(pairs[-2:])
    store.delete(SESSION_ID)
    assert store.get(SESSION_ID, source) == {}


def test_client_message_store_rejects_unsafe_or_malformed_records(tmp_path):
    source = tmp_path / f"{SESSION_ID}.jsonl"
    source.write_text("{}\n")
    store = ClaudeClientMessageStore(tmp_path / "state")
    with pytest.raises(ClaudeClientMessageStoreError):
        store.put(SESSION_ID, source, "../bad", CLIENT_ID)

    store.put(SESSION_ID, source, NATIVE_ID, CLIENT_ID)
    record = next(store.directory.glob("*.json"))
    record.write_text(json.dumps({"version": 1, "aliases": []}))
    os.chmod(record, 0o600)
    with pytest.raises(ClaudeClientMessageStoreError):
        store.get(SESSION_ID, source)
