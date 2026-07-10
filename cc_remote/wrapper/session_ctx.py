"""Per-session state for the multi-session wrapper pool.

A SessionContext owns exactly one cc subprocess (via SdkHandle) plus its
conversation state: ring buffer, seq counter, state machine, turn task,
translator, pending ask_user futures, and an emit lock. The wrapper machine
holds a pool of these keyed by session id; switching the viewed session is a
focus change (no disconnect), so background turns keep streaming.

The drain contract (one async-for per turn, running to the terminal
ResultMessage before accepting another query) holds naturally per ctx: each
turn task is spawned on its own ctx with its own SDK subprocess.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from cc_remote.protocol import State
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator


@dataclass
class SessionContext:
    # None until the first ResultMessage/init SystemMessage captures the real id
    # (a brand-new session). A resumed session knows its id at spawn time.
    session_id: Optional[str]
    sdk: SdkHandle                     # one ClaudeSDKClient → one `claude` subprocess
    buffer: RingBuffer                 # per-session ring (own seq namespace)
    cwd: str                           # resume requires cwd to match the jsonl path
    # Pool key = the client-facing routing identity: the real sid once known,
    # else a temp `tmp-<uuid>` for a brand-new session. Kept in sync with the
    # machine's `sessions` dict key so every emit can stamp `sid` WITHOUT an
    # O(n) reverse lookup — and so a pre-capture new session's live frames route
    # deterministically (never leak into whatever is currently focused).
    key: Optional[str] = None
    seq: int = 0                       # per-session monotonic counter
    state: State = "idle"
    engine: str = "claude"             # "claude" (SdkHandle) | "codex" (CodexHandle)
    turn_task: Optional[asyncio.Task] = None
    translator: Optional[StreamTranslator] = None
    # /btw ephemeral fork: a throwaway side-session forked from `parent_sid` that
    # inherits its context. Never persisted, excluded from the session list, and
    # discarded on close. Its turns reuse the normal _run_turn path.
    btw: bool = False
    parent_sid: Optional[str] = None
    # cc fork_session persists a transcript under a new id (unlike codex's
    # ephemeral fork); capture it here so close_btw can hard-delete it.
    btw_real_id: Optional[str] = None
    announced_model: Optional[str] = None
    announced_effort: Optional[str] = None
    announced_perm: Optional[str] = None
    # ---- external-write mirroring (a native `claude`/`codex` in the user's
    # terminal owns this session and is appending to its transcript) ----
    # epoch of the last append this wrapper did NOT make. Recent => the session is
    # externally owned: clients show it read-only and get mirrored History frames.
    external_ts: float = 0.0
    # an external append happened, so our resumed subprocess now holds a STALE
    # context. Reload (force_reconnect with resume) before running another turn,
    # else we'd continue from a conversation that has since moved on.
    needs_reload: bool = False
    # epoch the last turn we ran finished — writes within a short grace window
    # after it are OURS, not external.
    last_turn_end: float = 0.0
    pending_asks: dict = field(default_factory=dict)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq
