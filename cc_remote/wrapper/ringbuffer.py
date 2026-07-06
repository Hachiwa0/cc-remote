"""Monotonic-seq ring buffer for client reconnect replay.

Every downstream event the wrapper emits is appended with its seq. On a client
`hello(last_seq)`, `replay_from` returns the frames to send: the events with
seq > last_seq wrapped in replay_start/replay_end. If last_seq is older than
the buffer's tail (or None), a snapshot + truncated replay is produced.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from cc_remote.protocol import ReplayStart, ReplayEnd, Snapshot, StateEvent


class RingBuffer:
    def __init__(self, max_events: int, max_bytes: int):
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._buf: deque[tuple[int, object]] = deque()
        self._bytes = 0

    @staticmethod
    def _size(msg) -> int:
        return len(msg.model_dump_json().encode())  # type: ignore[attr-defined]

    def append(self, msg) -> None:
        size = self._size(msg)
        self._buf.append((msg.seq, msg))  # type: ignore[attr-defined]
        self._bytes += size
        while (len(self._buf) > self.max_events or self._bytes > self.max_bytes) and len(self._buf) > 1:
            _, old = self._buf.popleft()
            self._bytes -= self._size(old)

    @property
    def head_seq(self) -> int:
        return self._buf[0][0] if self._buf else 0

    @property
    def tail_seq(self) -> int:
        return self._buf[-1][0] if self._buf else 0

    def replay_from(self, last_seq: Optional[int], *, cc_session_id, state, tail_text: str = "", cwd: Optional[str] = None) -> list:
        if last_seq is None:
            # First hello: send only a snapshot (cc_session_id + state + cwd). The client
            # reads its IndexedDB cache, then re-hellos with last_seq to fetch only
            # the delta — so opening the app doesn't replay the whole buffer when
            # the client already has the history locally.
            return [Snapshot(cc_session_id=cc_session_id, state=state, tail_text=tail_text, cwd=cwd)]

        # Future cursor: the client's last_seq is beyond our buffer's tail. This
        # happens because the seq counter resets to 0 on every wrapper restart,
        # but the client's IndexedDB cache keeps the lastSeq from the previous
        # wrapper lifetime. Rebuild the client from the full buffer with
        # rebuild=True (NOT truncated — the buffer has the full history, nothing
        # is lost, so no "history may be missing" banner). No Snapshot here —
        # only hello(null) sends one, else the client re-hellos with the stale
        # cursor and loops.
        if self._buf and last_seq > self.tail_seq:
            have = list(self._buf)
            from_seq = have[0][0]
            to_seq = have[-1][0]
            frames: list = [ReplayStart(from_seq=from_seq, to_seq=to_seq, truncated=False, rebuild=True)]
            frames.extend(m for _, m in have)
            frames.append(ReplayEnd(to_seq=to_seq, truncated=False))
            return frames

        have = [(s, m) for s, m in self._buf if s > last_seq]
        # truncated if the requested last_seq+1 fell off the front of the buffer
        truncated = (last_seq + 1) < self.head_seq

        if not have:
            to_seq = last_seq
            return [ReplayStart(from_seq=last_seq + 1, to_seq=to_seq, truncated=truncated),
                    ReplayEnd(to_seq=to_seq, truncated=truncated)]

        from_seq = have[0][0]
        to_seq = have[-1][0]
        frames = [ReplayStart(from_seq=from_seq, to_seq=to_seq, truncated=truncated)]
        for _, m in have:
            frames.append(m)
        frames.append(ReplayEnd(to_seq=to_seq, truncated=truncated))
        return frames

    def latest_state(self):
        for _, m in reversed(self._buf):
            if isinstance(m, StateEvent):
                return m.state
        return None

    def latest_tail_text(self) -> str:
        parts: list[str] = []
        total = 0
        for _, m in reversed(self._buf):
            if m.type == "delta":  # type: ignore[attr-defined]
                parts.append(m.text)  # type: ignore[attr-defined]
                total += len(m.text)  # type: ignore[attr-defined]
                if total > 500:
                    break
        return "".join(reversed(parts))[-500:]
