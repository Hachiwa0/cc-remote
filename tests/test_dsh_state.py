from __future__ import annotations

import json
import os

from cc_remote.wrapper.dsh_state import (
    DshSessionPinStore,
    DshSessionPresentationStore,
)
from cc_remote.wrapper.session_pins import SessionPinStore
from cc_remote.wrapper.session_presentation import SessionPresentationStore


DSH_SID = "dsh@11111111-2222-3333-4444-555555555555"


def test_dsh_pins_do_not_change_the_rollback_compatible_pin_store(tmp_path):
    common = SessionPinStore(tmp_path)
    common.set_pinned("claude", "claude-session", True)
    common.set_pinned("codex", "codex-session", True)

    dsh = DshSessionPinStore(tmp_path)
    dsh.set_pinned("dsh", DSH_SID, True)

    common_payload = json.loads(
        (tmp_path / "session-pins.json").read_text(encoding="utf-8")
    )
    assert set(common_payload) == {"claude", "codex", "profile_revision"}
    assert SessionPinStore(tmp_path).ids("claude") == {"claude-session"}
    assert SessionPinStore(tmp_path).ids("codex") == {"codex-session"}
    assert DshSessionPinStore(tmp_path).ids("dsh") == {DSH_SID}
    assert oct(
        os.stat(tmp_path / "dsh-session-pins.json").st_mode & 0o777
    ) == "0o600"


def test_dsh_completion_receipts_use_an_independent_state_file(tmp_path):
    common = SessionPresentationStore(tmp_path)
    common.mark_completion("claude", "claude-session", "claude-completion")

    dsh = DshSessionPresentationStore(tmp_path)
    marked = dsh.mark_completion("dsh", DSH_SID, "dsh-completion")
    assert marked.completion_unread is True
    acknowledged = dsh.acknowledge_completion(
        "dsh", DSH_SID, "dsh-completion"
    )
    assert acknowledged.completion_unread is False

    common_payload = json.loads(
        (tmp_path / "session-presentation.json").read_text(encoding="utf-8")
    )
    assert all(
        not key.startswith("dsh\0")
        for key in common_payload["sessions"]
    )
    restored_common = SessionPresentationStore(tmp_path)
    assert restored_common.get(
        "claude", "claude-session"
    ).completion_unread is True
    restored_dsh = DshSessionPresentationStore(tmp_path)
    assert restored_dsh.get("dsh", DSH_SID).completion_unread is False
    assert oct(
        os.stat(tmp_path / "dsh-session-presentation.json").st_mode & 0o777
    ) == "0o600"
