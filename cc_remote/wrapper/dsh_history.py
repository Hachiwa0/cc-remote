"""Paged, source-ordered DSH history projection.

The DSH host remains the only durable source.  This adapter keeps only a small
generation-local detail/image lookup so the browser can expand a turn it has
just received without copying the full DSH log into cc-remote state.
"""
from __future__ import annotations

import base64
import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cc_remote.protocol import ConversationTurn
from cc_remote.wrapper.dsh_client import DshClient, DshProtocolError
from cc_remote.wrapper.dsh_stream import (
    DshEventError,
    DshStreamTranslator,
    parse_dsh_history_cursor,
)
from cc_remote.wrapper.history_store import (
    materialize_history_turns,
)


def dsh_history_image_id(turn_id: str, attachment_id: str) -> str:
    digest = hashlib.sha256(
        f"{turn_id}\0{attachment_id}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"dsh-img-{digest}"


@dataclass(frozen=True)
class DshHistoryPage:
    events: tuple[dict[str, Any], ...]
    turns: tuple[ConversationTurn, ...]
    has_more: bool
    oldest_id: str | None
    newest_id: str | None
    last_seq: int
    projections: dict[str, Any]
    projection_seq: int
    translator: DshStreamTranslator


@dataclass(frozen=True)
class DshHistoryImage:
    media_type: str
    width: int
    height: int
    data: str


@dataclass(frozen=True)
class _DshHistoryGroup:
    events: tuple[dict[str, Any], ...]
    # The next visible human message belongs to the same native DSH turn.  It
    # closes this UI segment without creating a legal session.fork cursor.
    steer_fence: dict[str, Any] | None = None


_CONTROL_EVENT_TYPES = frozenset({
    "model", "effort", "perm", "fast", "collaboration_mode",
    "session_control",
})


def _group_dsh_history_events(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[_DshHistoryGroup]:
    """Split DSH's one native turn into visible steer segments.

    The shared history materializer predates ``TurnSteered`` and intentionally
    splits only at ``UserMsg``. DSH can persist several human messages inside a
    single turn, so keeping that generic behavior would make every steer vanish
    after refresh.
    """
    groups: list[_DshHistoryGroup] = []
    current: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        if event_type in _CONTROL_EVENT_TYPES:
            continue
        if event_type == "turn_steered":
            if current:
                groups.append(_DshHistoryGroup(
                    events=tuple(current), steer_fence=event,
                ))
            current = [event]
            continue
        if event_type == "user_msg" and current:
            groups.append(_DshHistoryGroup(events=tuple(current)))
            current = []
        current.append(event)
        if event_type == "turn_end":
            groups.append(_DshHistoryGroup(events=tuple(current)))
            current = []
    if current:
        groups.append(_DshHistoryGroup(events=tuple(current)))
    return groups


def _materialize_dsh_group(
    group: _DshHistoryGroup,
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for event in group.events:
        if event.get("type") != "turn_steered":
            normalized.append(dict(event))
            continue
        user = dict(event)
        user["type"] = "user_msg"
        user.pop("turn_id", None)
        normalized.append(user)
    has_terminal = any(event.get("type") == "turn_end" for event in normalized)
    if group.steer_fence is not None and not has_terminal:
        fence_id = group.steer_fence.get("turn_id")
        if not isinstance(fence_id, str) or not fence_id:
            fence_id = "dsh-steer-fence"
        normalized.append({
            "type": "turn_end",
            "ts": group.steer_fence.get("ts"),
            "turn_id": fence_id,
            "result": {
                "subtype": "steered",
                "duration_ms": 0,
                "is_error": False,
            },
        })
    turns = [dict(turn) for turn in materialize_history_turns(normalized)]
    if group.steer_fence is not None and not has_terminal:
        for turn in turns:
            # A steering boundary is complete for presentation, but only the
            # native turn/end sequence is a DSH session.fork point.
            turn.pop("forkPointId", None)
            turn.pop("durationMs", None)
    return tuple(turns)


def _materialize_dsh_groups(
    groups: list[_DshHistoryGroup],
) -> list[dict[str, Any]]:
    return [
        turn
        for group in groups
        for turn in _materialize_dsh_group(group)
    ]


class DshHistory:
    EVENTS_PER_PAGE_MAX = 20_000
    DETAIL_CACHE_ENTRIES = 256
    DETAIL_CACHE_BYTES = 64 * 1024 * 1024
    IMAGE_CACHE_ENTRIES = 512
    ATTACHMENT_ID_MAX_BYTES = 1024

    def __init__(self, client: DshClient) -> None:
        self.client = client
        self._details: OrderedDict[
            tuple[str, str], tuple[dict[str, Any], ...]
        ] = OrderedDict()
        self._detail_sizes: dict[tuple[str, str], int] = {}
        self._detail_cache_bytes = 0
        self._images: OrderedDict[
            tuple[str, str, str], tuple[str, str]
        ] = OrderedDict()

    async def page(
        self,
        native_session_id: str,
        *,
        wire_session_id: str | None = None,
        before: str | None,
        limit: int,
        command_aliases: Mapping[str, str] | None = None,
    ) -> DshHistoryPage:
        cache_session_id = wire_session_id or native_session_id
        payload: dict[str, Any] = {
            "sessionId": native_session_id,
            # A DSH page counts append-origin assistant/user messages rather
            # than cc-remote turns. Leave headroom for multi-step tool turns.
            "maxMessages": max(8, min(1600, limit * 8)),
        }
        if before is not None:
            payload["beforeSeq"] = parse_dsh_history_cursor(before)
        raw = await self.client.call("session.history", payload)
        if not isinstance(raw, dict):
            raise DshProtocolError("session.history did not return an object")
        entries = raw.get("events")
        has_more = raw.get("hasMore")
        if (
            not isinstance(entries, list)
            or not isinstance(has_more, bool)
            or not all(isinstance(entry, dict) for entry in entries)
        ):
            raise DshProtocolError("session.history returned an invalid page")
        if len(entries) > self.EVENTS_PER_PAGE_MAX:
            raise DshProtocolError("session.history page exceeds event limit")

        translator = DshStreamTranslator(strict_history=True)
        translated: list[dict[str, Any]] = []
        image_refs: dict[str, list[dict[str, Any]]] = {}
        image_lookups: dict[tuple[str, str], tuple[str, str]] = {}
        last_seq = -1
        for entry in entries:
            event = entry.get("event")
            if not isinstance(event, dict):
                raise DshProtocolError("session.history entry omitted event")
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                raise DshProtocolError("session.history event omitted seq")
            if seq <= last_seq:
                raise DshProtocolError("session.history events are not ordered")
            last_seq = seq
            direct_turn_id = f"dsh-msg-{seq}"
            refs: list[dict[str, Any]] = []
            if self._direct_human_message(event):
                refs, lookups = self._image_refs(direct_turn_id, event)
                if refs:
                    image_refs[direct_turn_id] = refs
                    image_lookups.update(lookups)
            try:
                command_client_id = None
                if event.get("type") in {"command/run", "command/done"}:
                    data = event.get("data")
                    command_id = (
                        data.get("commandId")
                        if isinstance(data, dict) else None
                    )
                    if isinstance(command_id, str) and command_aliases:
                        command_client_id = command_aliases.get(command_id)
                rows = translator.feed(
                    event,
                    view=entry.get("view"),
                    command_client_id=command_client_id,
                )
            except DshEventError as exc:
                raise DshProtocolError(str(exc)) from exc
            for row in rows:
                row.sid = cache_session_id
                if refs and row.type in {"user_msg", "turn_steered"}:
                    row.image_refs = refs
                translated.append(row.model_dump(mode="json"))

        groups = _group_dsh_history_events(translated)
        projected = _materialize_dsh_groups(groups)
        for turn in projected:
            turn_id = turn.get("id")
            refs = image_refs.get(turn_id) if isinstance(turn_id, str) else None
            if refs:
                turn["imageRefs"] = refs
        # The carrier can over-fetch several complete turns. Return exactly the
        # newest requested count while retaining DSH's older-page fact.
        if len(groups) > limit:
            groups = groups[-limit:]
            translated = [row for group in groups for row in group.events]
            projected = _materialize_dsh_groups(groups)
            for turn in projected:
                turn_id = turn.get("id")
                refs = image_refs.get(turn_id) if isinstance(turn_id, str) else None
                if refs:
                    turn["imageRefs"] = refs
            has_more = True

        turns = tuple(ConversationTurn.model_validate(turn) for turn in projected)
        # A page may begin with presentation-only events (for example a model
        # header) which ``materialize_history_turns`` intentionally omits.
        # Derive each cache key from that exact group instead of zipping the
        # filtered turn list, or every later detail can shift onto its neighbor.
        for group in groups:
            group_turns = _materialize_dsh_group(group)
            if len(group_turns) != 1:
                continue
            turn_id = group_turns[0].get("id")
            if not isinstance(turn_id, str):
                continue
            self._remember_detail(
                cache_session_id,
                turn_id,
                tuple(dict(row) for row in group.events),
            )
        for turn in turns:
            aliases = {turn.id}
            if turn.clientMsgId:
                aliases.add(turn.clientMsgId)
            for ref in turn.imageRefs or ():
                lookup = image_lookups.get((turn.id, ref["image_id"]))
                if lookup is None:
                    continue
                for alias in aliases:
                    self._remember_image(
                        cache_session_id, alias, ref["image_id"], lookup,
                    )
        projections = raw.get("projections")
        projection_values = (
            projections.get("values")
            if isinstance(projections, dict) else {}
        )
        projection_seq = (
            projections.get("asOfSeq")
            if isinstance(projections, dict) else -1
        )
        if (
            not isinstance(projection_values, dict)
            or not isinstance(projection_seq, int)
            or isinstance(projection_seq, bool)
            or projection_seq < -1
            or projection_seq > 9_007_199_254_740_991
        ):
            raise DshProtocolError(
                "session.history returned invalid projections"
            )
        return DshHistoryPage(
            events=tuple(translated),
            turns=turns,
            has_more=has_more,
            oldest_id=turns[0].id if turns else None,
            newest_id=turns[-1].id if turns else None,
            last_seq=last_seq,
            projections=dict(projection_values),
            projection_seq=projection_seq,
            translator=translator,
        )

    def detail(
        self, session_id: str, turn_id: str,
    ) -> tuple[dict[str, Any], ...] | None:
        key = (session_id, turn_id)
        value = self._details.get(key)
        if value is not None:
            self._details.move_to_end(key)
        return value

    async def image(
        self,
        native_session_id: str,
        turn_id: str,
        image_id: str,
        *,
        wire_session_id: str | None = None,
    ) -> DshHistoryImage | None:
        key = (wire_session_id or native_session_id, turn_id, image_id)
        lookup = self._images.get(key)
        if lookup is None:
            return None
        self._images.move_to_end(key)
        attachment_id, expected_media_type = lookup
        value = await self.client.call("session.attachment", {
            "sessionId": native_session_id,
            "attachmentId": attachment_id,
        })
        if not isinstance(value, dict):
            raise DshProtocolError("session.attachment returned an invalid value")
        attachment = value.get("attachment")
        data = value.get("data")
        if not isinstance(attachment, dict) or not isinstance(data, str):
            raise DshProtocolError("session.attachment omitted data")
        media_type = attachment.get("mediaType")
        width = attachment.get("width")
        height = attachment.get("height")
        byte_size = attachment.get("bytes")
        if (
            media_type != expected_media_type
            or media_type not in {"image/png", "image/jpeg", "image/webp"}
            or not isinstance(width, int)
            or not isinstance(height, int)
            or not isinstance(byte_size, int)
            or width < 1 or height < 1 or byte_size < 1
        ):
            raise DshProtocolError("session.attachment metadata changed")
        try:
            decoded = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise DshProtocolError("session.attachment data is not base64") from exc
        if len(decoded) != byte_size:
            raise DshProtocolError("session.attachment byte length mismatch")
        return DshHistoryImage(
            media_type=media_type,
            width=width,
            height=height,
            data=data,
        )

    def invalidate(self, session_id: str) -> None:
        for key in [key for key in self._details if key[0] == session_id]:
            self._details.pop(key, None)
            self._detail_cache_bytes -= self._detail_sizes.pop(key, 0)
        for key in [key for key in self._images if key[0] == session_id]:
            self._images.pop(key, None)

    def remember_event_images(
        self,
        session_id: str,
        event: dict[str, Any],
        *turn_aliases: str | None,
    ) -> list[dict[str, Any]]:
        """Register one live DSH image under native and client turn ids."""
        if not self._direct_human_message(event):
            return []
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return []
        native_turn_id = f"dsh-msg-{seq}"
        refs, lookups = self._image_refs(native_turn_id, event)
        aliases = {
            alias for alias in (native_turn_id, *turn_aliases)
            if isinstance(alias, str) and alias
        }
        for (turn_id, image_id), lookup in lookups.items():
            if turn_id != native_turn_id:
                continue
            for alias in aliases:
                self._remember_image(session_id, alias, image_id, lookup)
        return refs

    def _remember_detail(
        self,
        session_id: str,
        turn_id: str,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        key = (session_id, turn_id)
        try:
            size = len(json.dumps(
                events,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8", "surrogatepass"))
        except (TypeError, ValueError):
            return
        previous_size = self._detail_sizes.pop(key, 0)
        if key in self._details:
            self._details.pop(key, None)
            self._detail_cache_bytes -= previous_size
        if size > self.DETAIL_CACHE_BYTES:
            return
        self._details[key] = events
        self._detail_sizes[key] = size
        self._detail_cache_bytes += size
        self._details.move_to_end(key)
        while (
            len(self._details) > self.DETAIL_CACHE_ENTRIES
            or self._detail_cache_bytes > self.DETAIL_CACHE_BYTES
        ):
            removed_key, _ = self._details.popitem(last=False)
            self._detail_cache_bytes -= self._detail_sizes.pop(removed_key, 0)

    def _remember_image(
        self,
        session_id: str,
        turn_id: str,
        image_id: str,
        lookup: tuple[str, str],
    ) -> None:
        key = (session_id, turn_id, image_id)
        self._images[key] = lookup
        self._images.move_to_end(key)
        while len(self._images) > self.IMAGE_CACHE_ENTRIES:
            self._images.popitem(last=False)

    @staticmethod
    def _direct_human_message(event: dict[str, Any]) -> bool:
        if event.get("type") != "user/message" or event.get("surfaceOp") != "append":
            return False
        data = event.get("data")
        source = data.get("source") if isinstance(data, dict) else None
        return isinstance(source, dict) and source.get("kind") == "user"

    @staticmethod
    def _image_refs(
        turn_id: str,
        event: dict[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        dict[tuple[str, str], tuple[str, str]],
    ]:
        data = event.get("data")
        content = data.get("content") if isinstance(data, dict) else None
        if not isinstance(content, list):
            return [], {}
        refs: list[dict[str, Any]] = []
        lookups: dict[tuple[str, str], tuple[str, str]] = {}
        for block in content:
            attachment = block.get("attachment") if isinstance(block, dict) else None
            if not isinstance(attachment, dict):
                continue
            attachment_id = attachment.get("attachmentId")
            media_type = attachment.get("mediaType")
            width = attachment.get("width")
            height = attachment.get("height")
            byte_size = attachment.get("bytes")
            if (
                not isinstance(attachment_id, str)
                or not attachment_id
                or len(attachment_id.encode("utf-8", "surrogatepass"))
                > DshHistory.ATTACHMENT_ID_MAX_BYTES
                or media_type not in {"image/png", "image/jpeg", "image/webp"}
                or not isinstance(width, int) or width < 1
                or not isinstance(height, int) or height < 1
                or not isinstance(byte_size, int) or byte_size < 1
            ):
                continue
            image_id = dsh_history_image_id(turn_id, attachment_id)
            refs.append({
                "image_id": image_id,
                "media_type": media_type,
                "width": width,
                "height": height,
                "byte_size": byte_size,
            })
            lookups[(turn_id, image_id)] = (attachment_id, media_type)
        return refs, lookups
