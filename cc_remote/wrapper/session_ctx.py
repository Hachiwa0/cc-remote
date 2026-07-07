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
    turn_task: Optional[asyncio.Task] = None
    translator: Optional[StreamTranslator] = None
    announced_model: Optional[str] = None
    announced_perm: Optional[str] = None
    pending_asks: dict = field(default_factory=dict)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq
