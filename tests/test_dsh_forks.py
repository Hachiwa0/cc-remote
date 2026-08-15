from __future__ import annotations

import os
import stat

import pytest

from cc_remote.wrapper.dsh_forks import (
    DshForkJournal,
    DshForkJournalError,
)


def _begin(journal: DshForkJournal, cwd: str) -> dict:
    return journal.begin(
        "request-1",
        "dsh@parent-session",
        "parent-session",
        "dsh-seq-20",
        20,
        cwd,
        {"parent-session", "older-session"},
    )


def test_dsh_fork_journal_persists_at_most_once_state_privately(tmp_path):
    cwd = os.path.realpath(tmp_path)
    journal = DshForkJournal(tmp_path)

    assert _begin(journal, cwd)["status"] == "intent"
    assert journal.claim_submission("request-1") is True
    journal.mark_uncertain("request-1")

    reloaded = DshForkJournal(tmp_path)
    assert reloaded.get("request-1")["status"] == "uncertain"
    completed = reloaded.complete("request-1", "dsh@child-session")
    assert completed["session_id"] == "dsh@child-session"
    assert DshForkJournal(tmp_path).get("request-1") == completed
    assert stat.S_IMODE((tmp_path / "dsh-forks.json").stat().st_mode) == 0o600


def test_dsh_fork_journal_rejects_request_id_reuse(tmp_path):
    cwd = os.path.realpath(tmp_path)
    journal = DshForkJournal(tmp_path)
    _begin(journal, cwd)

    with pytest.raises(DshForkJournalError, match="reused"):
        journal.begin(
            "request-1",
            "dsh@another-parent",
            "another-parent",
            "dsh-seq-20",
            20,
            cwd,
            {"another-parent"},
        )


def test_dsh_fork_journal_refuses_symlink_or_corrupt_state(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    journal_path = tmp_path / "dsh-forks.json"
    journal_path.symlink_to(target)
    with pytest.raises(DshForkJournalError, match="not bounded"):
        DshForkJournal(tmp_path)

    journal_path.unlink()
    journal_path.write_text("not json", encoding="utf-8")
    with pytest.raises(DshForkJournalError, match="unreadable"):
        DshForkJournal(tmp_path)


def test_dsh_fork_journal_leaves_room_for_before_seq_increment(tmp_path):
    journal = DshForkJournal(tmp_path)
    with pytest.raises(DshForkJournalError, match="fork sequence"):
        journal.begin(
            "request-max",
            "dsh@parent-session",
            "parent-session",
            "dsh-seq-9007199254740991",
            9_007_199_254_740_991,
            os.path.realpath(tmp_path),
            {"parent-session"},
        )
