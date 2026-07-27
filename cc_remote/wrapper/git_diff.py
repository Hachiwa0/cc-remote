"""Bounded, containment-aware Git diff projection for Remote artifacts."""
from __future__ import annotations

import asyncio
import os
import signal
import stat
from collections.abc import Awaitable, Callable

CommandRunner = Callable[[tuple[str, ...], int], Awaitable[str]]
_TRUNCATED_MARKER = "[diff truncated at transport safety limit]"


async def read_git_diff(
    cwd: str,
    file: str,
    *,
    allowed_external_paths: frozenset[str],
    max_bytes: int,
    source_max_bytes: int,
    run_command: CommandRunner,
) -> str:
    """Return a bounded diff while refusing path escapes and special files."""
    if not file:
        tracked = await run_command(
            ("git", "-C", cwd, "diff", "--no-ext-diff", "--no-textconv",
             "HEAD"),
            max_bytes,
        )
        if _TRUNCATED_MARKER in tracked:
            return tracked

        root_text = await run_command(
            ("git", "-C", cwd, "rev-parse", "--show-toplevel"),
            4096,
        )
        root = os.path.realpath(
            root_text.strip().splitlines()[0] if root_text.strip() else cwd)
        untracked_text = await run_command(
            ("git", "-C", root, "ls-files", "-z", "--others",
             "--exclude-standard"),
            min(max_bytes, 512 * 1024),
        )
        untracked_paths = untracked_text.split("\0")
        if not untracked_text.endswith("\0"):
            untracked_paths = untracked_paths[:-1]

        parts = [tracked]
        used = len(tracked.encode(errors="replace"))
        for relative_file in untracked_paths:
            if not relative_file or used >= max_bytes:
                continue
            candidate = os.path.join(root, relative_file)
            parent = os.path.realpath(os.path.dirname(candidate))
            contained = os.path.join(parent, os.path.basename(candidate))
            try:
                if os.path.commonpath((root, contained)) != root:
                    continue
                file_stat = os.lstat(contained)
            except (OSError, ValueError):
                continue
            if (not stat.S_ISREG(file_stat.st_mode)
                    or file_stat.st_size > source_max_bytes):
                continue
            separator = (
                "\n" if parts[-1] and not parts[-1].endswith("\n") else ""
            )
            remaining = max_bytes - used - len(separator.encode())
            if remaining <= 0:
                break
            addition = await run_command(
                ("git", "-C", root, "diff", "--no-ext-diff",
                 "--no-textconv", "--no-index", "--", "/dev/null",
                 relative_file),
                remaining,
            )
            if not addition:
                continue
            if separator:
                parts.append(separator)
                used += len(separator.encode())
            parts.append(addition)
            used += len(addition.encode(errors="replace"))
            if _TRUNCATED_MARKER in addition:
                break
        return "".join(parts)

    root_text = await run_command(
        ("git", "-C", cwd, "rev-parse", "--show-toplevel"),
        4096,
    )
    in_repository = bool(root_text.strip())
    root = os.path.realpath(
        root_text.strip().splitlines()[0] if in_repository else cwd)
    expanded = os.path.expanduser(file)
    candidate = os.path.abspath(
        expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded))
    parent = os.path.realpath(os.path.dirname(candidate))
    contained = os.path.join(parent, os.path.basename(candidate))
    try:
        below_root = os.path.commonpath((root, contained)) == root
    except ValueError:
        below_root = False
    if not below_root:
        resolved = os.path.realpath(contained)
        if resolved not in allowed_external_paths:
            raise ValueError("diff path is outside the session repository")
        try:
            file_stat = os.lstat(resolved)
        except OSError:
            return ""
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("external diff target must be a regular file")
        if file_stat.st_size > source_max_bytes:
            raise ValueError(
                "external diff target exceeds the source size limit")
        return await run_command(
            ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
             "--no-index", "--", "/dev/null", resolved),
            max_bytes,
        )
    relative_file = os.path.relpath(contained, root)

    if not in_repository:
        try:
            file_stat = os.lstat(contained)
        except OSError:
            return ""
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("diff target outside Git must be a regular file")
        if file_stat.st_size > source_max_bytes:
            raise ValueError("diff target exceeds the source size limit")
        return await run_command(
            ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
             "--no-index", "--", "/dev/null", relative_file),
            max_bytes,
        )

    diff = await run_command(
        ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
         "HEAD", "--", relative_file),
        max_bytes,
    )
    if diff.strip():
        return diff

    try:
        file_stat = os.lstat(contained)
    except OSError:
        return ""
    if not stat.S_ISREG(file_stat.st_mode):
        tracked = await run_command(
            ("git", "-C", root, "ls-files", "--stage", "--", relative_file),
            64 * 1024,
        )
        if tracked:
            return ""
        raise ValueError("untracked diff target must be a regular file")
    untracked = await run_command(
        ("git", "-C", root, "ls-files", "--others", "--exclude-standard",
         "--", relative_file),
        64 * 1024,
    )
    if not untracked:
        return ""
    if file_stat.st_size > source_max_bytes:
        raise ValueError("untracked diff target exceeds the source size limit")
    return await run_command(
        ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
         "--no-index", "--", "/dev/null", relative_file),
        max_bytes,
    )


async def bounded_process_output(
    argv: tuple[str, ...],
    max_bytes: int,
    timeout: float = 10.0,
) -> str:
    """Capture stdout to a hard byte limit and always reap the process group."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    chunks: list[bytes] = []
    total = 0
    truncated = False

    async def read_stdout() -> None:
        nonlocal total, truncated
        while True:
            chunk = await proc.stdout.read(
                min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            remaining = max_bytes - total
            if len(chunk) > remaining:
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)

    async def discard_stdout_and_wait() -> None:
        async def discard_stdout() -> None:
            while await proc.stdout.read(64 * 1024):
                pass

        await asyncio.gather(proc.wait(), discard_stdout())

    timed_out = False

    def stop_group(sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    reaped = False
    try:
        try:
            await asyncio.wait_for(read_stdout(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
        if timed_out:
            stop_group(signal.SIGKILL)
        elif truncated:
            stop_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(discard_stdout_and_wait(), timeout=2.0)
        except asyncio.TimeoutError:
            stop_group(signal.SIGKILL)
            await discard_stdout_and_wait()
        reaped = True
    finally:
        if not reaped:
            stop_group(signal.SIGKILL)
            await discard_stdout_and_wait()
    if timed_out:
        raise asyncio.TimeoutError("diff command exceeded its time limit")
    text = b"".join(chunks).decode(errors="replace")
    if truncated:
        text += f"\n\n{_TRUNCATED_MARKER}\n"
    return text
