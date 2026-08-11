"""Bounded discovery of cwd-aware Codex named permission profiles."""

from __future__ import annotations

from typing import Any

from cc_remote.wrapper.codex_rpc import codex_rpc


MAX_PERMISSION_PROFILES = 128
MAX_PERMISSION_PROFILE_ID = 256
MAX_PERMISSION_PROFILE_DESCRIPTION = 2048


def normalize_permission_profiles(result: Any) -> list[dict[str, Any]]:
    """Validate the public subset of ``permissionProfile/list``."""
    raw_profiles = result.get("data") if isinstance(result, dict) else None
    if not isinstance(raw_profiles, list):
        raise RuntimeError(
            "codex app-server returned an invalid permission profile catalog")
    profiles: list[dict[str, Any]] = []
    for raw in raw_profiles[:MAX_PERMISSION_PROFILES]:
        if not isinstance(raw, dict):
            continue
        profile_id = raw.get("id")
        allowed = raw.get("allowed")
        if (not isinstance(profile_id, str) or not profile_id
                or len(profile_id) > MAX_PERMISSION_PROFILE_ID
                or not isinstance(allowed, bool)):
            continue
        description = raw.get("description")
        if not isinstance(description, str):
            description = None
        elif len(description) > MAX_PERMISSION_PROFILE_DESCRIPTION:
            description = description[:MAX_PERMISSION_PROFILE_DESCRIPTION]
        profiles.append({
            "id": profile_id,
            "description": description,
            "allowed": allowed,
        })
    if not profiles and raw_profiles:
        raise RuntimeError(
            "codex app-server returned no valid permission profiles")
    return profiles


async def codex_permission_profiles(
    cwd: str,
    *,
    codex_home: str | None = None,
) -> list[dict[str, Any]]:
    """Probe the control plane without creating a thread or spending tokens."""
    params = {"cwd": cwd, "limit": MAX_PERMISSION_PROFILES}
    if codex_home is None:
        # Preserve the long-standing call shape for the default profile and for
        # embedders which wrap the one-shot RPC helper.
        result = await codex_rpc("permissionProfile/list", params, cwd=cwd)
    else:
        result = await codex_rpc(
            "permissionProfile/list",
            params,
            cwd=cwd,
            codex_home=codex_home,
        )
    return normalize_permission_profiles(result)
