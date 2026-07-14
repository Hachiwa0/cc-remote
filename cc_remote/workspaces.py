"""Durable Work registry layered over native Claude and Codex sessions.

The engines remain the transcript authorities. This registry stores only the
cc-remote product identity and private working directory assigned to a Work
chat, so Work and Code can be listed and deleted independently.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, cast
from uuid import uuid4


Engine = Literal["claude", "codex"]

_MAX_WORK_ARTIFACTS = 200
_MAX_WORK_ARTIFACT_SCAN = 4096
_WORK_ARTIFACT_PREVIEW_SUFFIXES = frozenset({
    ".c", ".cc", ".conf", ".cpp", ".css", ".csv", ".go", ".h", ".hpp",
    ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsonl", ".log", ".md",
    ".mdown", ".markdown", ".mjs", ".py", ".rs", ".sh", ".sql", ".svg",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
    ".avif", ".doc", ".docx", ".gif", ".jpeg", ".jpg", ".odp", ".ods",
    ".odt", ".pdf", ".png", ".ppt", ".pptx", ".rtf", ".webp", ".xls",
    ".xlsx",
})
_WORK_ARTIFACT_KIND_SUFFIXES = {
    "document": frozenset({".doc", ".docx", ".md", ".odt", ".rtf", ".txt"}),
    "spreadsheet": frozenset({".csv", ".ods", ".xls", ".xlsx"}),
    "presentation": frozenset({".odp", ".ppt", ".pptx"}),
    "image": frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}),
    "pdf": frozenset({".pdf"}),
}

_MAX_CLAUDE_SETTINGS_BYTES = 1024 * 1024
_MAX_CLAUDE_SETTING_VALUE = 16 * 1024
_CLAUDE_RUNTIME_ENV_KEYS = frozenset({
    # Direct Anthropic and compatible endpoints.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
    "ANTHROPIC_BETAS",
    "ANTHROPIC_CUSTOM_HEADERS",
    # Claude Code's supported cloud-provider selectors and credentials.
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_API_KEY",
})


def _bounded_setting(value: object, *, limit: int = _MAX_CLAUDE_SETTING_VALUE) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _claude_runtime_settings() -> dict[str, object]:
    """Copy provider connectivity only; never import global Work context.

    The explicit Work policy is the sole Claude settings file used by the SDK.
    User hooks, permissions, plugins, skills and CLAUDE.md discovery therefore
    cannot cross from Code into Work, while a compatible endpoint configured in
    the user's settings remains usable.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    path = root / "settings.json"
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CLAUDE_SETTINGS_BYTES:
            return {}
        raw = path.read_bytes()
    except OSError:
        return {}
    if len(raw) > _MAX_CLAUDE_SETTINGS_BYTES:
        return {}
    try:
        source = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(source, dict):
        return {}

    runtime: dict[str, object] = {}
    model = _bounded_setting(source.get("model"), limit=256)
    if model is not None:
        runtime["model"] = model
    source_env = source.get("env")
    if isinstance(source_env, dict):
        env = {
            key: value
            for key in sorted(_CLAUDE_RUNTIME_ENV_KEYS)
            if (value := _bounded_setting(source_env.get(key))) is not None
        }
        if env:
            runtime["env"] = env
    return runtime


@dataclass(frozen=True)
class WorkSessionRecord:
    work_id: str
    engine: Engine
    cwd: str
    session_id: str | None
    title: str | None
    project_id: str | None
    archived: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class WorkProject:
    project_id: str
    name: str
    description: str
    created_at: float
    updated_at: float


class WorkRegistry:
    """One engine's Work namespace below its provider-owned home directory."""

    def __init__(self, root: Path, engine: Engine):
        self.root = Path(os.path.realpath(os.path.expanduser(str(root))))
        self.engine = engine
        self.chats_root = self.root / "chats"
        self.db_path = self.root / "registry.sqlite3"

    def initialize(self) -> None:
        self._mkdir_private(self.root)
        self._mkdir_private(self.chats_root)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS work_sessions (
                    work_id TEXT PRIMARY KEY,
                    engine TEXT NOT NULL CHECK (engine IN ('claude', 'codex')),
                    session_id TEXT UNIQUE,
                    cwd TEXT NOT NULL UNIQUE,
                    title TEXT,
                    project_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_sessions_updated
                    ON work_sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS work_sessions_project
                    ON work_sessions(project_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS work_projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_sources (
                    source_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES work_projects(project_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('file', 'link', 'note')),
                    title TEXT NOT NULL,
                    uri TEXT,
                    stored_path TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_sources_project
                    ON work_sources(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS work_plugins (
                    plugin_id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES work_projects(project_id)
                        ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES work_projects(project_id)
                        ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    next_run_at REAL NOT NULL,
                    repeat_seconds INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at REAL,
                    last_session_id TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_schedules_due
                    ON work_schedules(enabled, next_run_at);
                """
            )
        self._chmod_file(self.db_path)

    def create_session(self, project_id: str | None = None) -> WorkSessionRecord:
        self.initialize()
        if project_id and self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        now = time.time()
        work_id = f"work-{uuid4().hex}"
        chat_root = self.chats_root / work_id
        workspace = chat_root / "workspace"
        for directory in (
            chat_root, workspace, chat_root / "uploads",
        ):
            self._mkdir_private(directory)
        cwd = str(workspace)
        with self._connect() as db:
            db.execute(
                """INSERT INTO work_sessions
                   (work_id, engine, session_id, cwd, project_id, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?)""",
                (work_id, self.engine, cwd, project_id, now, now),
            )
        record = WorkSessionRecord(
            work_id=work_id, engine=self.engine, cwd=cwd, session_id=None,
            title=None, project_id=project_id, archived=False,
            created_at=now, updated_at=now,
        )
        try:
            self._materialize_project(record)
        except Exception:
            self.abandon(work_id)
            raise
        return record

    def dashboard(self) -> dict[str, list[dict[str, object]]]:
        self.initialize()
        with self._connect() as db:
            projects = [dict(row) for row in db.execute(
                "SELECT * FROM work_projects ORDER BY updated_at DESC").fetchall()]
            sources = [dict(row) for row in db.execute(
                "SELECT source_id, project_id, kind, title, uri, created_at "
                "FROM work_sources ORDER BY created_at DESC").fetchall()]
            plugins = [dict(row) for row in db.execute(
                "SELECT * FROM work_plugins ORDER BY updated_at DESC").fetchall()]
            schedules = [dict(row) for row in db.execute(
                "SELECT * FROM work_schedules ORDER BY next_run_at ASC").fetchall()]
        for plugin in plugins:
            plugin["enabled"] = bool(plugin["enabled"])
        for schedule in schedules:
            schedule["enabled"] = bool(schedule["enabled"])
        return {
            "projects": projects, "sources": sources,
            "plugins": plugins, "schedules": schedules,
        }

    def create_project(self, name: str, description: str = "") -> str:
        self.initialize()
        project_id = f"project-{uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO work_projects VALUES (?, ?, ?, ?, ?)",
                (project_id, name, description, now, now),
            )
        return project_id

    def get_project(self, project_id: str) -> WorkProject | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM work_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkProject(
            project_id=row["project_id"], name=row["name"],
            description=row["description"], created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def artifacts(self, session_id: str) -> list[dict[str, object]]:
        """List bounded, user-visible deliverables from one private Work cwd.

        Project context and copied source material are inputs, not artifacts.
        Symlinks and hidden paths are ignored so enumeration never escapes or
        exposes implementation state from the wrapper-owned workspace.
        """
        record = self.get_by_session(session_id)
        if record is None or not self.contains_cwd(record.cwd):
            raise LookupError(f"unknown Work session: {session_id}")
        root = os.path.realpath(record.cwd)
        artifacts: list[dict[str, object]] = []
        scanned = 0
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            relative_dir = os.path.relpath(current, root)
            if relative_dir == ".":
                dirs[:] = [name for name in dirs
                           if not name.startswith(".") and name != "资料库"]
            else:
                dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in files:
                scanned += 1
                if scanned > _MAX_WORK_ARTIFACT_SCAN:
                    break
                if name.startswith("."):
                    continue
                path = Path(current) / name
                relative = os.path.relpath(path, root)
                if relative == "WORK.md" or relative.startswith(f"资料库{os.sep}"):
                    continue
                try:
                    info = path.lstat()
                    resolved = os.path.realpath(path)
                    if (not stat.S_ISREG(info.st_mode)
                            or os.path.commonpath((root, resolved)) != root):
                        continue
                except (OSError, ValueError):
                    continue
                suffix = path.suffix.lower()
                kind = next((candidate for candidate, suffixes
                             in _WORK_ARTIFACT_KIND_SUFFIXES.items()
                             if suffix in suffixes), "file")
                artifacts.append({
                    "path": relative.replace(os.sep, "/"),
                    "size": info.st_size,
                    "modified_at": info.st_mtime,
                    "kind": kind,
                    "previewable": suffix in _WORK_ARTIFACT_PREVIEW_SUFFIXES,
                })
            if scanned > _MAX_WORK_ARTIFACT_SCAN:
                break
        artifacts.sort(key=lambda item: (-float(item["modified_at"]), str(item["path"])))
        return artifacts[:_MAX_WORK_ARTIFACTS]

    def delete_project(self, project_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            paths = [row["stored_path"] for row in db.execute(
                "SELECT stored_path FROM work_sources WHERE project_id = ? "
                "AND stored_path IS NOT NULL", (project_id,)).fetchall()]
            db.execute(
                "UPDATE work_sessions SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
            changed = db.execute(
                "DELETE FROM work_projects WHERE project_id = ?", (project_id,),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work project: {project_id}")
        for stored_path in paths:
            self._remove_owned_library_file(stored_path)

    def add_source(self, project_id: str, kind: str, title: str,
                   uri: str | None = None, filename: str | None = None,
                   content: bytes | None = None) -> str:
        self.initialize()
        if self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        if kind not in {"file", "link", "note"}:
            raise ValueError("unsupported Work source kind")
        source_id = f"source-{uuid4().hex}"
        stored_path = None
        if kind == "file":
            if content is None or not filename:
                raise ValueError("file source requires content and filename")
            safe_name = Path(filename).name.strip()
            if not safe_name or safe_name in {".", ".."}:
                raise ValueError("invalid source filename")
            directory = self.root / "library" / source_id
            self._mkdir_private(directory)
            path = directory / safe_name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            stored_path = str(path)
        now = time.time()
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO work_sources
                       (source_id, project_id, kind, title, uri, stored_path, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, project_id, kind, title, uri, stored_path, now),
                )
                db.execute(
                    "UPDATE work_projects SET updated_at = ? WHERE project_id = ?",
                    (now, project_id),
                )
        except Exception:
            if stored_path:
                self._remove_owned_library_file(stored_path)
            raise
        return source_id

    def delete_source(self, source_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT stored_path FROM work_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            changed = db.execute(
                "DELETE FROM work_sources WHERE source_id = ?", (source_id,),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work source: {source_id}")
        if row and row["stored_path"]:
            self._remove_owned_library_file(row["stored_path"])

    def create_plugin(self, name: str, instructions: str,
                      project_id: str | None = None) -> str:
        self.initialize()
        if project_id and self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        plugin_id = f"plugin-{uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO work_plugins VALUES (?, ?, ?, ?, 1, ?, ?)",
                (plugin_id, project_id, name, instructions, now, now),
            )
        return plugin_id

    def delete_plugin(self, plugin_id: str) -> None:
        self._delete_row("work_plugins", "plugin_id", plugin_id)

    def create_schedule(self, title: str, prompt: str, next_run_at: float,
                        repeat_seconds: int | None = None,
                        project_id: str | None = None) -> str:
        self.initialize()
        if project_id and self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        schedule_id = f"schedule-{uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                """INSERT INTO work_schedules
                   (schedule_id, project_id, title, prompt, next_run_at,
                    repeat_seconds, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (schedule_id, project_id, title, prompt, next_run_at,
                 repeat_seconds, now, now),
            )
        return schedule_id

    def delete_schedule(self, schedule_id: str) -> None:
        self._delete_row("work_schedules", "schedule_id", schedule_id)

    def claim_due_schedules(self, now: float, limit: int = 8) -> list[dict[str, object]]:
        """Atomically advance due rows before execution to avoid duplicate runs."""
        self.initialize()
        claimed: list[dict[str, object]] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT * FROM work_schedules
                   WHERE enabled = 1 AND next_run_at <= ?
                   ORDER BY next_run_at ASC LIMIT ?""", (now, limit),
            ).fetchall()
            for row in rows:
                repeat = row["repeat_seconds"]
                enabled = 1 if repeat else 0
                next_run = now + int(repeat) if repeat else row["next_run_at"]
                db.execute(
                    """UPDATE work_schedules SET enabled = ?, next_run_at = ?,
                       last_run_at = ?, last_error = NULL, updated_at = ?
                       WHERE schedule_id = ?""",
                    (enabled, next_run, now, now, row["schedule_id"]),
                )
                claimed.append(dict(row))
        return claimed

    def complete_schedule(self, schedule_id: str,
                          session_id: str | None, error: str | None) -> None:
        self.initialize()
        with self._connect() as db:
            db.execute(
                """UPDATE work_schedules SET last_session_id = ?, last_error = ?,
                   updated_at = ? WHERE schedule_id = ?""",
                (session_id, error, time.time(), schedule_id),
            )

    def _delete_row(self, table: str, key: str, value: str) -> None:
        if (table, key) not in {
            ("work_plugins", "plugin_id"),
            ("work_schedules", "schedule_id"),
        }:
            raise ValueError("unsupported Work table")
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                f"DELETE FROM {table} WHERE {key} = ?", (value,),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work item: {value}")

    def _materialize_project(self, record: WorkSessionRecord) -> None:
        project = (self.get_project(record.project_id)
                   if record.project_id else None)
        if record.project_id and project is None:
            raise LookupError(f"unknown Work project: {record.project_id}")
        with self._connect() as db:
            sources = (db.execute(
                "SELECT * FROM work_sources WHERE project_id = ? ORDER BY created_at",
                (record.project_id,),
            ).fetchall() if record.project_id else [])
            plugins = (db.execute(
                """SELECT * FROM work_plugins
                   WHERE enabled = 1 AND (project_id IS NULL OR project_id = ?)
                   ORDER BY created_at""", (record.project_id,),
            ).fetchall() if record.project_id else db.execute(
                """SELECT * FROM work_plugins
                   WHERE enabled = 1 AND project_id IS NULL ORDER BY created_at"""
            ).fetchall())
        if project is None and not plugins:
            return
        workspace = Path(record.cwd)
        library = workspace / "资料库"
        lines = ([f"# {project.name}", "", project.description.strip(), ""]
                 if project else ["# Work 工作上下文", ""])
        if sources:
            lines.extend(["## 资料库", ""])
        used_names: set[str] = set()
        for source in sources:
            if source["kind"] == "file" and source["stored_path"]:
                self._mkdir_private(library)
                base = Path(source["stored_path"]).name
                candidate = base
                index = 2
                while candidate in used_names:
                    candidate = f"{Path(base).stem}-{index}{Path(base).suffix}"
                    index += 1
                used_names.add(candidate)
                target = library / candidate
                shutil.copyfile(source["stored_path"], target)
                target.chmod(0o600)
                lines.append(f"- {source['title']}: `资料库/{candidate}`")
            elif source["kind"] == "link":
                lines.append(f"- {source['title']}: {source['uri'] or ''}")
            else:
                lines.append(f"- {source['title']}: {source['uri'] or ''}")
        if plugins:
            lines.extend(["", "## 已启用工作插件", ""])
            for plugin in plugins:
                lines.extend([f"### {plugin['name']}", plugin["instructions"], ""])
        context = workspace / "WORK.md"
        context.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        context.chmod(0o600)

    def _remove_owned_library_file(self, stored_path: str) -> None:
        library = self.root / "library"
        path = Path(stored_path)
        try:
            if os.path.commonpath((os.path.realpath(path), os.path.realpath(library))) \
                    != os.path.realpath(library):
                raise RuntimeError("refusing to remove a source outside Work library")
        except ValueError as exc:
            raise RuntimeError("invalid Work source path") from exc
        directory = path.parent
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def bind_session(self, work_id: str, session_id: str) -> None:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_sessions SET session_id = ?, updated_at = ?
                   WHERE work_id = ? AND engine = ?""",
                (session_id, time.time(), work_id, self.engine),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work session: {work_id}")

    def ensure_claude_policy(self, record: WorkSessionRecord) -> str:
        """Write the wrapper-owned fail-closed Claude sandbox configuration."""
        if self.engine != "claude" or not self.contains_cwd(record.cwd):
            raise ValueError("Claude Work policy requested for an invalid workspace")
        policy_dir = self.root / "policies"
        self._mkdir_private(policy_dir)
        path = policy_dir / f"{record.work_id}.json"
        payload = {
            **_claude_runtime_settings(),
            "permissions": {"defaultMode": "acceptEdits"},
            "sandbox": {
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "failIfUnavailable": True,
                "filesystem": {
                    "denyRead": ["~/"],
                    "allowRead": [record.cwd],
                    "denyWrite": ["~/"],
                    "allowWrite": [record.cwd],
                },
            },
        }
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            self._chmod_file(path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return str(path)

    def get_by_session(self, session_id: str) -> WorkSessionRecord | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM work_sessions WHERE session_id = ? AND engine = ?",
                (session_id, self.engine),
            ).fetchone()
        return self._record(row)

    def get_by_work_id(self, work_id: str) -> WorkSessionRecord | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM work_sessions WHERE work_id = ? AND engine = ?",
                (work_id, self.engine),
            ).fetchone()
        return self._record(row)

    def records_by_session(self) -> dict[str, WorkSessionRecord]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM work_sessions
                   WHERE session_id IS NOT NULL AND engine = ?""",
                (self.engine,),
            ).fetchall()
        records = (self._record(row) for row in rows)
        return {
            record.session_id: record
            for record in records
            if record is not None and record.session_id is not None
        }

    def unbound_records_by_cwd(self) -> dict[str, WorkSessionRecord]:
        """Crash-recovery candidates created before a native session id landed."""
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM work_sessions
                   WHERE session_id IS NULL AND engine = ?""",
                (self.engine,),
            ).fetchall()
        records = (self._record(row) for row in rows)
        return {
            os.path.realpath(record.cwd): record
            for record in records
            if record is not None
        }

    def contains_cwd(self, cwd: str | None) -> bool:
        if not cwd:
            return False
        try:
            target = os.path.realpath(os.path.expanduser(cwd))
            common = os.path.commonpath((target, str(self.chats_root)))
        except (OSError, ValueError):
            return False
        return common == str(self.chats_root)

    def update_title(self, session_id: str, title: str) -> None:
        self._update(session_id, "title", title)

    def update_archived(self, session_id: str, archived: bool) -> None:
        self._update(session_id, "archived", 1 if archived else 0)

    def delete(self, session_id: str) -> WorkSessionRecord:
        record = self.get_by_session(session_id)
        if record is None:
            raise LookupError(f"unknown Work session: {session_id}")
        chat_root = Path(record.cwd).parent
        self._require_owned_chat_root(chat_root, record.work_id)
        with self._connect() as db:
            db.execute(
                "DELETE FROM work_sessions WHERE session_id = ? AND engine = ?",
                (session_id, self.engine),
            )
        if chat_root.exists():
            shutil.rmtree(chat_root)
        policy = self.root / "policies" / f"{record.work_id}.json"
        try:
            policy.unlink()
        except FileNotFoundError:
            pass
        return record

    def abandon(self, work_id: str) -> None:
        """Remove a never-bound Work directory after native session spawn fails."""
        record = self.get_by_work_id(work_id)
        if record is None or record.session_id is not None:
            return
        chat_root = Path(record.cwd).parent
        self._require_owned_chat_root(chat_root, work_id)
        with self._connect() as db:
            db.execute(
                "DELETE FROM work_sessions WHERE work_id = ? AND session_id IS NULL",
                (work_id,),
            )
        if chat_root.exists():
            shutil.rmtree(chat_root)
        policy = self.root / "policies" / f"{work_id}.json"
        try:
            policy.unlink()
        except FileNotFoundError:
            pass

    def _update(self, session_id: str, column: str, value: object) -> None:
        if column not in {"title", "archived"}:
            raise ValueError("unsupported Work metadata column")
        with self._connect() as db:
            db.execute(
                f"UPDATE work_sessions SET {column} = ?, updated_at = ? "
                "WHERE session_id = ? AND engine = ?",
                (value, time.time(), session_id, self.engine),
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _record(row: sqlite3.Row | None) -> WorkSessionRecord | None:
        if row is None:
            return None
        return WorkSessionRecord(
            work_id=row["work_id"], engine=row["engine"], cwd=row["cwd"],
            session_id=row["session_id"], title=row["title"],
            project_id=row["project_id"], archived=bool(row["archived"]),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)

    @staticmethod
    def _chmod_file(path: Path) -> None:
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISREG(mode):
            path.chmod(0o600)

    def _require_owned_chat_root(self, path: Path, work_id: str) -> None:
        expected = self.chats_root / work_id
        if path != expected or not self.contains_cwd(str(path / "workspace")):
            raise RuntimeError("refusing to delete a path outside the Work registry")


class WorkStores:
    def __init__(self, claude_root: Path, codex_root: Path):
        self._stores: dict[Engine, WorkRegistry] = {
            "claude": WorkRegistry(claude_root, "claude"),
            "codex": WorkRegistry(codex_root, "codex"),
        }

    def for_engine(self, engine: str) -> WorkRegistry:
        if engine not in self._stores:
            raise ValueError(f"unsupported Work engine: {engine}")
        return self._stores[cast(Engine, engine)]

    def initialize(self) -> None:
        for store in self._stores.values():
            store.initialize()

    def classify(self, engine: str, session_id: str | None, cwd: str | None) -> str:
        store = self.for_engine(engine)
        if session_id and store.get_by_session(session_id) is not None:
            return "work"
        return "work" if store.contains_cwd(cwd) else "code"

    def roots(self) -> Iterable[Path]:
        return (store.root for store in self._stores.values())
