"""Zero-token tests for private Codex per-thread control durability."""

from __future__ import annotations

import os

import pytest

from cc_remote.wrapper.codex_controls import (
    CodexControlStore,
    CodexControlStoreError,
)


def test_codex_control_store_round_trips_and_clears_override(tmp_path):
    store = CodexControlStore(tmp_path)
    thread_id = "019f94fc-6230-7212-8a08-4e19bbc49104"

    assert store.get(thread_id).as_dict() == {}
    controls = store.update(
        thread_id,
        approval_policy="never",
        permission_profile=":danger-full-access",
        web_search="live",
    )
    assert controls.as_dict() == {
        "approval_policy": "never",
        "permission_profile": ":danger-full-access",
        "web_search": "live",
    }
    assert CodexControlStore(tmp_path).get(thread_id) == controls
    assert (os.stat(store.path).st_mode & 0o077) == 0

    store.update(
        thread_id,
        approval_policy=None,
        permission_profile=None,
        web_search=None,
    )
    assert CodexControlStore(tmp_path).get(thread_id).as_dict() == {}


def test_codex_control_store_loads_legacy_search_only_entry(tmp_path):
    path = tmp_path / "codex-session-controls.json"
    path.write_text(
        '{"version":1,"sessions":{"legacy":{"web_search":"live"}}}')
    path.chmod(0o600)

    assert CodexControlStore(tmp_path).get("legacy").as_dict() == {
        "web_search": "live",
    }


def test_codex_control_store_rejects_unsafe_file(tmp_path):
    path = tmp_path / "codex-session-controls.json"
    path.write_text('{"version":1,"sessions":{}}')
    path.chmod(0o644)
    with pytest.raises(CodexControlStoreError, match="private"):
        CodexControlStore(tmp_path)
