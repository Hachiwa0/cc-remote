"""Detect Claude sessions owned by another local Claude Code process.

An idle Claude TUI does not keep its transcript open, so transcript growth is
not a stable ownership signal.  Prefer an explicit session id from the process
command line.  Fall back to the working directory only when it identifies one
watched session unambiguously.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Mapping

from cc_remote.wrapper.codex_external import (
    MAX_PROC_SCAN,
    HolderScan,
    ProcessIdentity,
    _process_cmdline,
    _process_start_ticks,
    _process_stat,
)


_CLAUDE_COMMANDS = frozenset({"claude", "claude.exe"})
_SESSION_FLAGS = frozenset({b"--resume", b"-r", b"--session-id"})
_CONTINUE_FLAGS = frozenset({b"--continue", b"-c"})
_BACKGROUND_ROLES = frozenset({
    b"daemon", b"bg-pty-host", b"bg-spare", b"--bg-pty-host", b"--bg-spare",
})


def _is_claude_cli(args: tuple[bytes, ...] | None) -> bool:
    if not args:
        return False
    # Recent native installers leave a daemon and pre-warmed background PTYs
    # alive after the interactive terminal exits. They do not own a transcript
    # and must not manufacture a permanent read-only session.
    if len(args) > 1 and args[1] in _BACKGROUND_ROLES:
        return False
    for raw in args[:3]:
        value = os.fsdecode(raw)
        name = os.path.basename(value).lower()
        if name in _CLAUDE_COMMANDS:
            return True
        normalized = value.replace("\\", "/").lower()
        if "/claude/versions/" in normalized:
            return True
        if "claude-code" in normalized and name in {
            "cli.js", "cli.mjs", "index.js", "index.mjs",
        }:
            return True
    return False


def _explicit_session_ids(
    args: tuple[bytes, ...], sid_by_arg: Mapping[bytes, str],
) -> tuple[set[str], bool]:
    result: set[str] = set()
    has_explicit_target = False
    for index, arg in enumerate(args):
        if arg in _SESSION_FLAGS:
            if index + 1 < len(args):
                target = args[index + 1]
                if target and not target.startswith(b"-"):
                    has_explicit_target = True
                    sid = sid_by_arg.get(target)
                    if sid is not None:
                        result.add(sid)
            continue
        for prefix in (b"--resume=", b"--session-id="):
            if arg.startswith(prefix):
                target = arg[len(prefix):]
                if not target:
                    continue
                has_explicit_target = True
                sid = sid_by_arg.get(target)
                if sid is not None:
                    result.add(sid)
    return result, has_explicit_target


def _is_continue(args: tuple[bytes, ...]) -> bool:
    """Return whether this CLI asks Claude to continue the cwd's latest chat."""
    return any(
        arg in _CONTINUE_FLAGS or arg.startswith(b"--continue=")
        for arg in args
    )


def claude_session_holders(
    paths: Mapping[str, str],
    cwds: Mapping[str, str],
    *,
    wrapper_pid: int,
    proc_root: str = "/proc",
    continue_bindings: dict[ProcessIdentity, str] | None = None,
    continue_candidates: dict[ProcessIdentity, str] | None = None,
    continue_resolver: Callable[[str], str | None] | None = None,
) -> HolderScan:
    """Return stable external Claude process identities for watched sessions.

    Direct children of ``wrapper_pid`` are the SDK processes owned by this
    wrapper and are excluded.  A foreign process with an explicit session flag
    owns only that session.  A foreign Claude process without a session id is
    associated by cwd only when exactly one watched session uses that cwd.
    Ambiguous same-cwd processes must not make every sibling session read-only.
    """
    holders = {sid: set() for sid in paths}
    root = Path(proc_root)
    sid_by_arg = {sid.encode(): sid for sid in paths}
    cwd_sids: dict[str, set[str]] = {}
    for sid in paths:
        cwd = cwds.get(sid)
        if not cwd:
            continue
        cwd_sids.setdefault(os.path.realpath(cwd), set()).add(sid)
    missing_cwds = set(paths).difference(
        sid for sids in cwd_sids.values() for sid in sids)
    bindings = continue_bindings if continue_bindings is not None else {}
    candidates = (
        continue_candidates if continue_candidates is not None else {})
    seen_continue: set[ProcessIdentity] = set()

    complete = True
    try:
        processes = (entry for entry in root.iterdir() if entry.name.isdigit())
        for index, proc_dir in enumerate(processes):
            if index >= MAX_PROC_SCAN:
                complete = False
                break
            process_stat = _process_stat(proc_dir)
            if process_stat is None:
                continue
            parent_pid, start_ticks, _tty_nr = process_stat
            args = _process_cmdline(proc_dir)
            if args is None:
                # A disappearing process is harmless. A stable process whose
                # command line is unreadable makes the ownership scan incomplete.
                if _process_start_ticks(proc_dir) == start_ticks:
                    complete = False
                continue
            if not _is_claude_cli(args):
                continue
            if parent_pid == wrapper_pid:
                continue

            matched, has_explicit_session = _explicit_session_ids(
                args, sid_by_arg)
            identity = ProcessIdentity(int(proc_dir.name), start_ticks)
            continue_command = (
                not matched
                and not has_explicit_session
                and _is_continue(args)
            )
            if continue_command:
                seen_continue.add(identity)
                if identity in bindings:
                    bound_sid = bindings[identity]
                else:
                    if identity in candidates:
                        bound_sid = candidates[identity]
                    else:
                        try:
                            process_cwd = os.path.realpath(
                                os.readlink(proc_dir / "cwd"))
                        except OSError:
                            if _process_start_ticks(proc_dir) == start_ticks:
                                complete = False
                            continue
                        if continue_resolver is None:
                            # The watched subset cannot prove Claude's cwd-global
                            # "latest" target. Treat missing catalog authority as
                            # incomplete, never as the sole watched sid.
                            complete = False
                            continue
                        try:
                            bound_sid = continue_resolver(process_cwd)
                        except Exception:
                            complete = False
                            continue
                        if bound_sid is None:
                            # A live `-c` process should have selected a native
                            # session. An empty/racing catalog is not proof that
                            # it owns none of the watched sessions; retry on the
                            # next scan while remaining fail-closed now.
                            complete = False
                            continue
                        # Cache the native startup selection even before Remote
                        # watches it, but do not call that an ownership binding.
                        # When the exact sid enters `paths`, promote it below.
                        candidates[identity] = bound_sid
                if bound_sid in paths:
                    bindings[identity] = bound_sid
                    matched.add(bound_sid)
            if (not matched and not has_explicit_session
                    and not continue_command):
                try:
                    process_cwd = os.path.realpath(os.readlink(proc_dir / "cwd"))
                except OSError:
                    if _process_start_ticks(proc_dir) == start_ticks:
                        complete = False
                    continue
                cwd_matches = cwd_sids.get(process_cwd, ())
                if len(cwd_matches) == 1:
                    matched.update(cwd_matches)
            if not matched:
                if missing_cwds:
                    complete = False
                continue
            if _process_start_ticks(proc_dir) != start_ticks:
                bindings.pop(identity, None)
                candidates.pop(identity, None)
                continue
            for sid in matched:
                holders[sid].add(identity)
    except OSError:
        return HolderScan(holders, False)
    if complete:
        for identity in set(bindings).difference(seen_continue):
            bindings.pop(identity, None)
        for identity in set(candidates).difference(seen_continue):
            candidates.pop(identity, None)
    return HolderScan(holders, complete)
