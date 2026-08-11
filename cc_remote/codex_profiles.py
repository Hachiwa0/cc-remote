"""Validated local Codex account/profile registry.

Each profile is a complete ``CODEX_HOME`` boundary.  The registry deliberately
keeps filesystem paths private while providing stable public ids and labels for
the browser. A single profile keeps native session ids for compatibility. Once
more than one profile is configured, every account uses the opaque
``<profile>@<native-id>`` routing form so the same native UUID can never change
meaning when a default account is reordered or switched.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator


_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# ``@`` is reserved for the browser routing namespace (``profile@native``).
# Official Codex thread ids are UUIDs; accepting the separator inside a native
# id would make encode/decode non-bijective and could select the wrong account.
_NATIVE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_WIRE_ID_CHARS = 128
# Keep the registry bounded, but leave room beyond the twelve named browser
# ribbons so the documented ``more`` fallback is reachable instead of dead UI.
_MAX_PROFILES = 32
_MAX_LABEL_CHARS = 48
_MAX_HOME_BYTES = 4096
_TOPOLOGY_VERSION = 2
_TOPOLOGY_MAX_BYTES = 64 * 1024
_TRANSITION_VERSION = 1


@dataclass(frozen=True)
class CodexProfile:
    id: str
    label: str
    home: Path
    is_default: bool = False

    def public(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass(frozen=True)
class CodexProfileTopologyTransition:
    """One durable mapping from the previous account topology to the next.

    ``revision`` is written into every local store in the same atomic write as
    its transformed keys.  A wrapper crash can therefore replay only stores
    that did not finish the transition instead of applying a profile-id swap a
    second time.
    """

    revision: int
    previous: tuple[tuple[str, str], ...]
    previous_default_id: str | None
    current: tuple[tuple[str, str], ...]
    current_default_id: str
    remaps: dict[str, str]

    @property
    def previous_is_multi(self) -> bool:
        return len(self.previous) > 1

    @property
    def current_is_multi(self) -> bool:
        return len(self.current) > 1

    def _previous_owner_id(self) -> str:
        if self.previous_default_id is not None:
            return self.remaps.get(
                self.previous_default_id, self.previous_default_id)
        return self.current_default_id

    @property
    def legacy_profile_id(self) -> str:
        """Profile that owns pre-profile/unprefixed durable state."""
        if len(self.previous) == 1:
            return self.remaps.get(self.previous[0][0], self.previous[0][0])
        return self._previous_owner_id()

    def wire_session_id(self, session_id: str) -> str:
        """Translate one persisted routing id without changing native UUIDs."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("invalid persisted Codex session id")
        if "@" in session_id:
            old_profile_id, native_id = session_id.split("@", 1)
            profile_id = self.remaps.get(old_profile_id, old_profile_id)
        else:
            native_id = session_id
            profile_id = self.legacy_profile_id
        if not _NATIVE_SESSION_ID.fullmatch(native_id):
            raise ValueError("invalid persisted Codex native session id")
        if self.current_is_multi:
            routed = f"{profile_id}@{native_id}"
            if len(routed) > _MAX_WIRE_ID_CHARS:
                raise ValueError("persisted Codex session id is too long")
            return routed
        if profile_id == self.current_default_id:
            return native_id
        # State from a temporarily unavailable sibling stays dormant instead
        # of being attributed to the only currently configured account.
        routed = f"{profile_id}@{native_id}"
        if len(routed) > _MAX_WIRE_ID_CHARS:
            raise ValueError("persisted Codex session id is too long")
        return routed

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": _TRANSITION_VERSION,
            "revision": self.revision,
            "previous": [
                {"id": profile_id, "home": home}
                for profile_id, home in self.previous
            ],
            "previous_default_id": self.previous_default_id,
            "current": [
                {"id": profile_id, "home": home}
                for profile_id, home in self.current
            ],
            "current_default_id": self.current_default_id,
        }


@dataclass(frozen=True)
class _StoredTopology:
    format_version: int
    revision: int
    profiles: tuple[tuple[str, str], ...]
    default_id: str | None


class CodexProfileRegistry:
    """Ordered, immutable account registry with wire-id translation."""

    def __init__(self, profiles: tuple[CodexProfile, ...]) -> None:
        if not profiles:
            raise ValueError("Codex profiles must not be empty")
        self._profiles = profiles
        self._by_id = {profile.id: profile for profile in profiles}
        defaults = [profile for profile in profiles if profile.is_default]
        if len(defaults) != 1:
            raise ValueError("Codex profiles must contain exactly one default")
        self.default = defaults[0]

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        default_home: str | os.PathLike[str] | None = None,
    ) -> "CodexProfileRegistry":
        """Parse ``CC_REMOTE_CODEX_PROFILES_JSON`` or build legacy default."""
        if not isinstance(raw, str):
            raise ValueError("CC_REMOTE_CODEX_PROFILES_JSON must be a JSON object")
        if not raw.strip():
            fallback = default_home
            if fallback is None:
                fallback = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
            home = cls._home(fallback)
            return cls((CodexProfile(
                id="primary",
                label="默认账号",
                home=home,
                is_default=True,
            ),))
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "CC_REMOTE_CODEX_PROFILES_JSON must be valid JSON"
            ) from exc
        if not isinstance(payload, dict) or not payload:
            raise ValueError(
                "CC_REMOTE_CODEX_PROFILES_JSON must be a non-empty object"
            )
        if len(payload) > _MAX_PROFILES:
            raise ValueError(
                f"CC_REMOTE_CODEX_PROFILES_JSON supports at most {_MAX_PROFILES} profiles"
            )

        profiles: list[CodexProfile] = []
        homes: set[Path] = set()
        for profile_id, entry in payload.items():
            if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
                raise ValueError("CC_REMOTE_CODEX_PROFILES_JSON contains an invalid profile id")
            if not isinstance(entry, dict):
                raise ValueError("Codex profile entries must be objects")
            unknown = set(entry) - {"label", "home", "default"}
            if unknown:
                raise ValueError("Codex profile entry contains unknown fields")
            label = entry.get("label")
            if (
                not isinstance(label, str)
                or not label.strip()
                or label != label.strip()
                or len(label) > _MAX_LABEL_CHARS
                or any(ord(char) < 32 for char in label)
            ):
                raise ValueError("Codex profile labels must be printable and non-empty")
            home = cls._home(entry.get("home"))
            if home in homes:
                raise ValueError("Codex profile homes must be unique after realpath resolution")
            homes.add(home)
            is_default = entry.get("default", False)
            if not isinstance(is_default, bool):
                raise ValueError("Codex profile default must be a boolean")
            profiles.append(CodexProfile(
                id=profile_id,
                label=label,
                home=home,
                is_default=is_default,
            ))
        return cls(tuple(profiles))

    @staticmethod
    def _home(value: Any) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("Codex profile home must be an absolute path")
        raw = os.fspath(value)
        if (
            not raw
            or "\x00" in raw
            or len(raw.encode("utf-8", errors="surrogatepass")) > _MAX_HOME_BYTES
        ):
            raise ValueError("Codex profile home must be a bounded absolute path")
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            raise ValueError("Codex profile home must be an absolute path")
        return expanded.resolve(strict=False)

    def __iter__(self) -> Iterator[CodexProfile]:
        return iter(self._profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    @property
    def is_multi_profile(self) -> bool:
        return len(self._profiles) > 1

    def get(self, profile_id: str | None = None) -> CodexProfile:
        if profile_id is None:
            return self.default
        try:
            return self._by_id[profile_id]
        except KeyError as exc:
            raise ValueError(f"unknown Codex profile: {profile_id}") from exc

    def public_profiles(self) -> list[dict[str, str]]:
        return [profile.public() for profile in self._profiles]

    def wire_session_id(self, profile_id: str, native_session_id: str) -> str:
        profile = self.get(profile_id)
        if (
            not isinstance(native_session_id, str)
            or not _NATIVE_SESSION_ID.fullmatch(native_session_id)
        ):
            raise ValueError("invalid native Codex session id")
        if not self.is_multi_profile:
            return native_session_id
        routed = f"{profile.id}@{native_session_id}"
        if len(routed) > _MAX_WIRE_ID_CHARS:
            raise ValueError("native Codex session id is too long for its profile")
        return routed

    def resolve_wire_session_id(
        self,
        wire_session_id: str,
    ) -> tuple[CodexProfile, str]:
        if (
            not isinstance(wire_session_id, str)
            or not wire_session_id
            or len(wire_session_id) > _MAX_WIRE_ID_CHARS
        ):
            raise ValueError("invalid Codex wire session id")
        if "@" not in wire_session_id:
            if self.is_multi_profile:
                raise ValueError(
                    "multi-profile Codex session ids must be namespaced")
            profile = self.default
            native_session_id = wire_session_id
        else:
            if not self.is_multi_profile:
                raise ValueError(
                    "single-profile Codex session ids must not be namespaced")
            profile_id, native_session_id = wire_session_id.split("@", 1)
            profile = self.get(profile_id)
        if not _NATIVE_SESSION_ID.fullmatch(native_session_id):
            raise ValueError("invalid native Codex session id")
        return profile, native_session_id


class CodexProfileTopologyStore:
    """Local profile-id continuity keyed by the private CODEX_HOME realpath."""

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.path = Path(state_dir) / "codex-profile-topology.json"
        self.pending_path = Path(state_dir) / "codex-profile-transition.json"
        self.legacy_restart_path = (
            Path(state_dir) / "codex-legacy-restart-profile.json")

    def legacy_restart_profile_id(
        self,
        registry: CodexProfileRegistry,
        transition: CodexProfileTopologyTransition | None,
        *,
        profile_revision: int,
    ) -> str | None:
        """Return the durable owner of the unprofiled restart marker."""
        stored: str | None = None
        stored_revision = 0
        if self.legacy_restart_path.exists():
            try:
                raw = json.loads(self.legacy_restart_path.read_text("utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("invalid legacy restart profile file") from exc
            if (
                not isinstance(raw, dict)
                or set(raw) != {"version", "profile_id", "profile_revision"}
                or raw.get("version") != 1
                or isinstance(raw.get("profile_revision"), bool)
                or not isinstance(raw.get("profile_revision"), int)
                or raw["profile_revision"] < 1
                or (
                    raw.get("profile_id") is not None
                    and (
                        not isinstance(raw.get("profile_id"), str)
                        or not _PROFILE_ID.fullmatch(raw["profile_id"])
                    )
                )
            ):
                raise ValueError("invalid legacy restart profile file")
            stored = raw.get("profile_id")
            stored_revision = raw["profile_revision"]
        elif transition is not None:
            # legacy_profile_id is already translated into the target registry.
            stored = transition.legacy_profile_id
            stored_revision = profile_revision
        else:
            stored = registry.default.id
            stored_revision = profile_revision

        if stored_revision > profile_revision:
            raise ValueError("legacy restart profile revision is ahead")
        if stored_revision < profile_revision and transition is None:
            raise ValueError("legacy restart profile revision is stale")
        if (
            transition is not None
            and stored_revision < profile_revision
            and stored_revision != profile_revision - 1
        ):
            raise ValueError("legacy restart profile missed a transition")
        if (
            transition is not None
            and stored is not None
            and stored_revision < profile_revision
        ):
            stored = transition.remaps.get(stored, stored)
        active_ids = {profile.id for profile in registry}
        owner = stored if stored in active_ids else None
        self._atomic_write(self.legacy_restart_path, {
            "version": 1,
            "profile_id": owner,
            "profile_revision": profile_revision,
        })
        return owner

    def prepare(
        self, registry: CodexProfileRegistry,
    ) -> CodexProfileTopologyTransition | None:
        """Persist one target before any independent store is transformed."""
        transition = self.transition(registry)
        pending = self._load_pending()
        if transition is None:
            if pending is not None:
                current = tuple(
                    (profile.id, str(profile.home)) for profile in registry)
                if (
                    pending.get("current") != [
                        {"id": profile_id, "home": home}
                        for profile_id, home in current
                    ]
                    or pending.get("current_default_id") != registry.default.id
                ):
                    raise ValueError(
                        "Codex profile transition targets another registry")
                self._clear_pending()
            return None
        payload = transition.as_dict()
        if pending is not None:
            if pending != payload:
                raise ValueError(
                    "Codex profile transition targets another registry")
            return transition
        self._atomic_write(self.pending_path, payload)
        return transition

    def complete(
        self,
        registry: CodexProfileRegistry,
        transition: CodexProfileTopologyTransition,
    ) -> None:
        pending = self._load_pending()
        if pending != transition.as_dict():
            raise ValueError("Codex profile transition marker is missing")
        self.persist(registry, revision=transition.revision)
        self._clear_pending()

    @staticmethod
    def _remaps(
        previous: tuple[tuple[str, str], ...],
        registry: CodexProfileRegistry,
    ) -> dict[str, str]:
        old_by_home = {home: profile_id for profile_id, home in previous}
        remaps = {
            old_by_home[str(profile.home)]: profile.id
            for profile in registry
            if str(profile.home) in old_by_home
            and old_by_home[str(profile.home)] != profile.id
        }
        # Renaming A into an id formerly owned by a now-inactive B must not
        # retain B's controls under the new active identity or collide with B's
        # Work row. Complete every injective rename chain into a permutation by
        # moving the displaced tail into the head id that the chain vacates:
        # A->B becomes A->B, B->A; A->B, B->C becomes a three-way cycle. The
        # stores already apply remaps simultaneously, so this preserves dormant
        # state while keeping the active CODEX_HOME attached to its own data.
        old_ids = {profile_id for profile_id, _home in previous}
        targets = set(remaps.values())
        for start in tuple(remaps):
            if start in targets:
                continue
            tail = start
            seen: set[str] = set()
            while tail in remaps and tail not in seen:
                seen.add(tail)
                tail = remaps[tail]
            if tail in old_ids and tail not in remaps:
                remaps[tail] = start
        return remaps

    @staticmethod
    def _validate_home_replacements(
        previous: tuple[tuple[str, str], ...],
        registry: CodexProfileRegistry,
        remaps: dict[str, str],
    ) -> None:
        """Reject an id silently changing which account it represents.

        A changed id is safe only when the home-based remap moves its old
        state to another identity (for example a rename, swap, or displaced
        rename chain).  Otherwise persisted state under that id would be
        interpreted as belonging to the replacement CODEX_HOME.
        """
        old_by_id = dict(previous)
        for profile in registry:
            old_home = old_by_id.get(profile.id)
            if (
                old_home is not None
                and old_home != str(profile.home)
                and profile.id not in remaps
            ):
                raise ValueError(
                    "Codex profile id cannot replace its CODEX_HOME without "
                    "preserving the previous account under another id"
                )

    def transition(
        self, registry: CodexProfileRegistry,
    ) -> CodexProfileTopologyTransition | None:
        previous = self._load()
        current = tuple(
            (profile.id, str(profile.home)) for profile in registry)
        if previous is not None and previous.format_version == _TOPOLOGY_VERSION and (
            previous.profiles == current
            and (
                previous.default_id is None
                or previous.default_id == registry.default.id
            )
        ):
            return None
        old_profiles = previous.profiles if previous is not None else ()
        old_default = previous.default_id if previous is not None else None
        if old_default is None and len(old_profiles) == 1:
            old_default = old_profiles[0][0]
        remaps = self._remaps(old_profiles, registry)
        self._validate_home_replacements(old_profiles, registry, remaps)
        return CodexProfileTopologyTransition(
            revision=(previous.revision + 1 if previous is not None else 1),
            previous=old_profiles,
            previous_default_id=old_default,
            current=current,
            current_default_id=registry.default.id,
            remaps=remaps,
        )

    def revision(self, registry: CodexProfileRegistry) -> int:
        transition = self.transition(registry)
        if transition is not None:
            return transition.revision
        previous = self._load()
        return previous.revision if previous is not None else 1

    def remaps(self, registry: CodexProfileRegistry) -> dict[str, str]:
        transition = self.transition(registry)
        return dict(transition.remaps) if transition is not None else {}

    def persist(
        self,
        registry: CodexProfileRegistry,
        *,
        revision: int | None = None,
    ) -> None:
        if revision is None:
            previous = self._load()
            revision = previous.revision if previous is not None else 1
        if isinstance(revision, bool) or not isinstance(revision, int) \
                or revision < 1:
            raise ValueError("Codex profile topology revision is invalid")
        payload = {
            "version": _TOPOLOGY_VERSION,
            "revision": revision,
            "default_id": registry.default.id,
            "profiles": [
                {"id": profile.id, "home": str(profile.home)}
                for profile in registry
            ],
        }
        self._atomic_write(self.path, payload)

    @staticmethod
    def _decode_profiles(
        value: Any,
        *,
        allow_empty: bool = False,
    ) -> tuple[tuple[str, str], ...]:
        if (
            not isinstance(value, list)
            or (not value and not allow_empty)
            or len(value) > _MAX_PROFILES
        ):
            raise ValueError("invalid Codex profile topology file")
        result: list[tuple[str, str]] = []
        ids: set[str] = set()
        homes: set[str] = set()
        for entry in value:
            if not isinstance(entry, dict) or set(entry) != {"id", "home"}:
                raise ValueError("invalid Codex profile topology file")
            profile_id = entry.get("id")
            if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
                raise ValueError("invalid Codex profile topology file")
            home = str(CodexProfileRegistry._home(entry.get("home")))
            if profile_id in ids or home in homes:
                raise ValueError("invalid Codex profile topology file")
            ids.add(profile_id)
            homes.add(home)
            result.append((profile_id, home))
        return tuple(result)

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _TOPOLOGY_MAX_BYTES:
            raise ValueError("Codex profile topology is too large")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _load_pending(self) -> dict[str, Any] | None:
        try:
            info = self.pending_path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > _TOPOLOGY_MAX_BYTES
        ):
            raise ValueError("invalid Codex profile transition file")
        try:
            payload = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("invalid Codex profile transition file") from exc
        expected = {
            "version", "revision", "previous", "previous_default_id",
            "current", "current_default_id",
        }
        if not isinstance(payload, dict) or set(payload) != expected \
                or isinstance(payload.get("version"), bool) \
                or payload.get("version") != _TRANSITION_VERSION:
            raise ValueError("invalid Codex profile transition file")
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) \
                or revision < 1:
            raise ValueError("invalid Codex profile transition file")
        previous = self._decode_profiles(
            payload.get("previous"), allow_empty=True)
        current = self._decode_profiles(payload.get("current"))
        previous_default = payload.get("previous_default_id")
        current_default = payload.get("current_default_id")
        if (
            previous_default is not None
            and previous_default not in {profile_id for profile_id, _ in previous}
        ) or current_default not in {profile_id for profile_id, _ in current}:
            raise ValueError("invalid Codex profile transition file")
        return payload

    def _clear_pending(self) -> None:
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            return
        directory_fd = os.open(self.pending_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load(self) -> _StoredTopology | None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size > _TOPOLOGY_MAX_BYTES:
            raise ValueError("invalid Codex profile topology file")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("invalid Codex profile topology file") from exc
        if (
            not isinstance(payload, dict)
            or isinstance(payload.get("version"), bool)
            or payload.get("version") not in {1, _TOPOLOGY_VERSION}
            or not isinstance(payload.get("profiles"), list)
            or len(payload["profiles"]) > _MAX_PROFILES
        ):
            raise ValueError("invalid Codex profile topology file")
        version = payload["version"]
        expected = (
            {"version", "profiles"}
            if version == 1
            else {"version", "revision", "default_id", "profiles"}
        )
        if set(payload) != expected:
            raise ValueError("invalid Codex profile topology file")
        revision = payload.get("revision", 1)
        default_id = payload.get("default_id")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or (
                default_id is not None
                and (
                    not isinstance(default_id, str)
                    or not _PROFILE_ID.fullmatch(default_id)
                )
            )
        ):
            raise ValueError("invalid Codex profile topology file")
        result = self._decode_profiles(payload["profiles"])
        ids = {profile_id for profile_id, _home in result}
        if default_id is not None and default_id not in ids:
            raise ValueError("invalid Codex profile topology file")
        return _StoredTopology(
            format_version=version,
            revision=revision,
            profiles=result,
            default_id=default_id,
        )
