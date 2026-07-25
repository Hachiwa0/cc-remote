from __future__ import annotations

import base64
import json
from pathlib import Path
import sqlite3

import pytest

from cc_remote.wrapper.codex_provider_repair import (
    CANONICAL_OPENAI_PROVIDER_ID,
    canonical_thread_provider_is_restored,
    CodexProviderRepairError,
    HTTP_COMPAT_PROVIDER_ID,
    _replacement_first_line,
    main,
    repair_http_provider_records,
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".codex"
    (home / "sessions").mkdir(parents=True)
    (home / "archived_sessions").mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "model_provider TEXT NOT NULL, archived INTEGER NOT NULL, "
            "source TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL DEFAULT 123)"
        )
    return home


def _add_thread(
    home: Path,
    thread_id: str,
    *,
    metadata_provider: str,
    db_provider: str = HTTP_COMPAT_PROVIDER_ID,
    archived: bool = False,
    parent: str | None = None,
    depth: int = 1,
    body: bytes = b'{"type":"event_msg","payload":{"message":"keep"}}\n',
) -> Path:
    root = home / ("archived_sessions" if archived else "sessions")
    path = root / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    source = "vscode"
    if parent is not None:
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent,
                    "depth": depth,
                    "agent_path": "/root/test",
                },
            },
        }
    payload = {
        "id": thread_id,
        "originator": "Codex Desktop",
        "model_provider": metadata_provider,
        "source": source,
    }
    line = json.dumps(
        {"type": "session_meta", "payload": payload},
        separators=(",", ":"),
    ).encode() + b"\n"
    path.write_bytes(line + body)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "INSERT INTO threads("
            "id, rollout_path, model_provider, archived, source"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                thread_id,
                str(path),
                db_provider,
                int(archived),
                json.dumps(source, separators=(",", ":"))
                if isinstance(source, dict) else source,
            ),
        )
    return path


def _provider(home: Path, thread_id: str) -> str:
    with sqlite3.connect(home / "state_5.sqlite") as db:
        return db.execute(
            "SELECT model_provider FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()[0]


def _meta(path: Path) -> tuple[bytes, dict]:
    with path.open("rb") as stream:
        line = stream.readline()
    return line, json.loads(line)["payload"]


def test_provider_repair_dry_run_finds_direct_and_verified_descendant(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    child = "child-alias"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=root,
    )

    report = repair_http_provider_records(codex_home=home)

    assert [
        (item.thread_id, item.root_thread_id, item.patch_rollout)
        for item in report.candidates
    ] == [
        (child, root, True),
        (root, root, False),
    ]
    assert _provider(home, root) == HTTP_COMPAT_PROVIDER_ID
    assert _provider(home, child) == HTTP_COMPAT_PROVIDER_ID


def test_provider_repair_updates_db_and_patches_only_same_size_first_line(
    tmp_path,
):
    home = _home(tmp_path)
    root = "root-openai"
    child = "child-alias"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    path = _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=root,
    )
    original = path.read_bytes()
    original_line, original_meta = _meta(path)

    report = repair_http_provider_records(
        codex_home=home,
        apply=True,
        backup_dir=tmp_path / "backup",
        journal_dir=tmp_path / "journal",
    )

    repaired = path.read_bytes()
    repaired_line, repaired_meta = _meta(path)
    assert len(repaired) == len(original)
    assert len(repaired_line) == len(original_line)
    assert repaired[len(repaired_line):] == original[len(original_line):]
    assert repaired_meta == {
        **original_meta,
        "model_provider": CANONICAL_OPENAI_PROVIDER_ID,
    }
    assert _provider(home, root) == CANONICAL_OPENAI_PROVIDER_ID
    assert _provider(home, child) == CANONICAL_OPENAI_PROVIDER_ID
    assert report.changed_db_thread_ids == (child, root)
    assert report.changed_rollout_thread_ids == (child,)
    backup_manifest = json.loads(
        (tmp_path / "backup" / "rollout-first-lines.json").read_text())
    assert base64.b64decode(
        backup_manifest["records"][0]["first_line"]) == original_line
    assert list((tmp_path / "journal").glob("*.json")) == []
    assert canonical_thread_provider_is_restored(
        root, codex_home=home) is True
    assert canonical_thread_provider_is_restored(
        child, codex_home=home) is True


def test_provider_repair_defers_active_alias_rollout(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    child = "child-active"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=False,
        parent=root,
    )

    report = repair_http_provider_records(
        codex_home=home, roots=[root], apply=True,
        journal_dir=tmp_path / "journal",
    )

    assert report.changed_db_thread_ids == (root,)
    assert report.deferred_thread_ids == (child,)
    assert _provider(home, child) == HTTP_COMPAT_PROVIDER_ID


@pytest.mark.parametrize("case", ["unknown-parent", "cycle", "foreign-origin"])
def test_provider_repair_rejects_unproven_alias_lineage(tmp_path, case):
    home = _home(tmp_path)
    child = "child-alias"
    if case == "cycle":
        parent = "other-alias"
        _add_thread(
            home,
            parent,
            metadata_provider=HTTP_COMPAT_PROVIDER_ID,
            archived=True,
            parent=child,
        )
    else:
        parent = "missing-parent"
    path = _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=parent,
    )
    if case == "foreign-origin":
        line, _ = _meta(path)
        record = json.loads(line)
        record["payload"]["originator"] = "codex_cli_rs"
        replacement = json.dumps(record, separators=(",", ":")).encode() + b"\n"
        body = path.read_bytes()[len(line):]
        path.write_bytes(replacement + body)

    report = repair_http_provider_records(
        codex_home=home,
        apply=True,
        journal_dir=tmp_path / "journal",
    )

    assert report.candidates == ()
    assert child in report.rejected_thread_ids
    assert _provider(home, child) == HTTP_COMPAT_PROVIDER_ID


def test_provider_repair_accepts_depth_two_and_rejects_bad_depth(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    child = "child-alias"
    grandchild = "grandchild-alias"
    bad = "bad-depth"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=root,
    )
    _add_thread(
        home,
        grandchild,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=child,
        depth=2,
    )
    _add_thread(
        home,
        bad,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=child,
        depth=1,
    )

    report = repair_http_provider_records(codex_home=home)

    assert grandchild in {
        candidate.thread_id for candidate in report.candidates
    }
    assert bad in report.rejected_thread_ids


def test_provider_repair_rejects_db_and_rollout_source_mismatch(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET source='cli' WHERE id=?",
            (root,),
        )

    report = repair_http_provider_records(codex_home=home)

    assert report.candidates == ()
    assert report.rejected_thread_ids == (root,)


def test_provider_repair_is_idempotent(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    first = repair_http_provider_records(
        codex_home=home,
        apply=True,
        journal_dir=tmp_path / "journal",
    )
    second = repair_http_provider_records(
        codex_home=home,
        apply=True,
        journal_dir=tmp_path / "journal",
    )
    assert first.changed_db_thread_ids == (root,)
    assert second.candidates == ()


def test_provider_repair_rolls_back_db_and_rollout_together(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    child = "child-alias"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    path = _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=root,
    )
    original = path.read_bytes()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "CREATE TRIGGER block_root_provider BEFORE UPDATE OF model_provider "
            "ON threads WHEN NEW.id='root-openai' "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )

    with pytest.raises(CodexProviderRepairError, match="database"):
        repair_http_provider_records(
            codex_home=home,
            apply=True,
            journal_dir=tmp_path / "journal",
        )

    assert path.read_bytes() == original
    assert _provider(home, root) == HTTP_COMPAT_PROVIDER_ID
    assert _provider(home, child) == HTTP_COMPAT_PROVIDER_ID


def test_provider_repair_does_not_change_thread_recency(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        before = db.execute(
            "SELECT updated_at FROM threads WHERE id=?", (root,),
        ).fetchone()[0]

    repair_http_provider_records(
        codex_home=home,
        apply=True,
        journal_dir=tmp_path / "journal",
    )

    with sqlite3.connect(home / "state_5.sqlite") as db:
        after = db.execute(
            "SELECT updated_at FROM threads WHERE id=?", (root,),
        ).fetchone()[0]
    assert after == before


def test_provider_repair_recovers_interrupted_first_line_from_journal(tmp_path):
    home = _home(tmp_path)
    root = "root-openai"
    child = "child-alias"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)
    path = _add_thread(
        home,
        child,
        metadata_provider=HTTP_COMPAT_PROVIDER_ID,
        archived=True,
        parent=root,
    )
    original = path.read_bytes()
    first_line = original.splitlines(keepends=True)[0]
    replacement = _replacement_first_line(first_line)
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / f"{child}.json").write_text(json.dumps({
        "version": 1,
        "thread_id": child,
        "rollout_path": str(path),
        "original": base64.b64encode(first_line).decode(),
        "replacement": base64.b64encode(replacement).decode(),
    }))
    corrupted = bytearray(original)
    corrupted[:20] = b"!" * 20
    path.write_bytes(corrupted)

    report = repair_http_provider_records(
        codex_home=home,
        apply=True,
        journal_dir=journal,
    )

    assert report.changed_db_thread_ids == (child, root)
    assert _meta(path)[1]["model_provider"] == CANONICAL_OPENAI_PROVIDER_ID
    assert list(journal.glob("*.json")) == []


def test_provider_repair_cli_is_dry_run_by_default(tmp_path, capsys):
    home = _home(tmp_path)
    root = "root-openai"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)

    assert main(["--codex-home", str(home)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"][0]["thread_id"] == root
    assert payload["changed_db_thread_ids"] == []
    assert _provider(home, root) == HTTP_COMPAT_PROVIDER_ID


def test_provider_repair_cli_apply_requires_backup_and_stopped_writers(
    tmp_path,
):
    home = _home(tmp_path)
    with pytest.raises(SystemExit):
        main(["--codex-home", str(home), "--apply"])


def test_provider_repair_cli_apply_uses_backup_and_confirmation(
    tmp_path,
    capsys,
):
    home = _home(tmp_path)
    root = "root-openai"
    _add_thread(
        home, root, metadata_provider=CANONICAL_OPENAI_PROVIDER_ID)

    assert main([
        "--codex-home", str(home),
        "--apply",
        "--backup-dir", str(tmp_path / "backup"),
        "--journal-dir", str(tmp_path / "journal"),
        "--confirm-writers-stopped",
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["changed_db_thread_ids"] == [root]
    assert (tmp_path / "backup" / "state_5.sqlite").is_file()
    assert _provider(home, root) == CANONICAL_OPENAI_PROVIDER_ID


def test_provider_repair_rejects_unsupported_threads_schema(tmp_path):
    home = tmp_path / ".codex"
    (home / "sessions").mkdir(parents=True)
    (home / "archived_sessions").mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY)")

    with pytest.raises(CodexProviderRepairError, match="schema"):
        repair_http_provider_records(codex_home=home)
