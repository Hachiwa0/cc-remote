"""Codex executable discovery and wrapper-owned child process environment.

This module is deliberately independent from the Codex session and handle
layers. Control-plane RPCs, model discovery, and resident handles all need the
same executable selection without importing each other.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from cc_remote.log import logger
from cc_remote.wrapper.child_env import sanitized_child_env

log = logger("cc_remote.wrapper.codex_runtime")

_BIN_CACHE: Optional[str] = None
_BIN_CACHE_INVENTORY: Optional[tuple[tuple[object, ...], ...]] = None
_MAX_CODEX_CANDIDATES = 16
_MAX_STANDALONE_CANDIDATES = 6
_MAX_NVM_CANDIDATES = 3
_CODEX_VERSION_TIMEOUT = 5


def codex_candidates() -> list[str]:
    """Return bounded Codex installs in stable tie-break order."""
    home = os.path.expanduser("~")
    out = list(islice(glob.iglob(
        os.path.join(home, ".codex/packages/standalone/releases/*/bin/codex")),
        _MAX_STANDALONE_CANDIDATES))
    out.append(os.path.join(home, ".local/bin/codex"))
    which = shutil.which("codex")
    if which:
        out.append(which)
    out += list(islice(glob.iglob(
        os.path.join(home, ".nvm/versions/node/*/bin/codex")),
        _MAX_NVM_CANDIDATES))
    out += ["/opt/homebrew/bin/codex", "/usr/local/bin/codex", "/usr/bin/codex"]
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in out:
        if not os.path.exists(candidate):
            continue
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        unique.append(candidate)
        if len(unique) >= _MAX_CODEX_CANDIDATES:
            break
    return unique


def codex_version(path: str) -> tuple[int, ...]:
    """Return ``codex --version`` as a tuple, or ``(-1,)`` when unusable."""
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=_CODEX_VERSION_TIMEOUT,
            env=codex_env(path),
        )
    except Exception:
        return (-1,)
    match = re.search(
        r"(\d+)\.(\d+)\.(\d+)",
        (result.stdout or "") + (result.stderr or ""),
    )
    return tuple(int(group) for group in match.groups()) if match else (-1,)


def codex_inventory(
    candidates: list[str],
) -> tuple[tuple[object, ...], ...]:
    """Fingerprint candidates so an in-place CLI upgrade invalidates cache."""
    inventory: list[tuple[object, ...]] = []
    for path in candidates:
        real = os.path.realpath(path)
        try:
            stat = os.stat(path)
            identity: tuple[object, ...] = (
                path,
                real,
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
            )
        except OSError:
            identity = (path, real, None, None, None, None)
        inventory.append(identity)
    return tuple(inventory)


def resolve_codex_bin() -> str:
    """Locate the newest usable Codex CLI with upgrade-aware caching."""
    global _BIN_CACHE, _BIN_CACHE_INVENTORY
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    candidates = codex_candidates()
    inventory = codex_inventory(candidates)
    if _BIN_CACHE and inventory == _BIN_CACHE_INVENTORY:
        return _BIN_CACHE
    if not candidates:
        _BIN_CACHE = None
        _BIN_CACHE_INVENTORY = inventory
        return "codex"
    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        probed = list(pool.map(codex_version, candidates))
    versions = list(zip(probed, candidates))
    best_version, best = max(versions, key=lambda item: item[0])
    if best_version == (-1,):
        best = candidates[0]
    _BIN_CACHE = best
    _BIN_CACHE_INVENTORY = inventory
    log.info(
        "codex bin resolved",
        path=best,
        version=".".join(map(str, best_version)),
        considered=[
            {"path": candidate, "version": ".".join(map(str, version))}
            for version, candidate in versions
        ],
    )
    return best


def codex_env(
    bin_path: str,
    codex_home: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Build the sanitized environment for wrapper-owned Codex subprocesses."""
    env = sanitized_child_env()
    if codex_home is not None:
        home = Path(codex_home).expanduser()
        if not home.is_absolute():
            raise ValueError("CODEX_HOME must be absolute")
        env["CODEX_HOME"] = str(home.resolve(strict=False))
    proxy = os.environ.get("CC_REMOTE_CODEX_PROXY", "").strip()
    if proxy:
        scheme = urlsplit(proxy).scheme.lower()
        if scheme in {"http", "https"}:
            env.update({
                "HTTP_PROXY": proxy,
                "HTTPS_PROXY": proxy,
                "http_proxy": proxy,
                "https_proxy": proxy,
            })
        elif scheme in {"socks5", "socks5h"}:
            env.update({"ALL_PROXY": proxy, "all_proxy": proxy})
        bypass = [
            value.strip()
            for value in (
                env.get("NO_PROXY") or env.get("no_proxy") or ""
            ).split(",")
            if value.strip()
        ]
        for local in ("127.0.0.1", "localhost", "::1"):
            if local not in bypass:
                bypass.append(local)
        env["NO_PROXY"] = env["no_proxy"] = ",".join(bypass)
    bindir = (
        os.path.dirname(os.path.abspath(bin_path))
        if os.sep in bin_path else ""
    )
    if bindir and os.path.exists(os.path.join(bindir, "node")):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env
