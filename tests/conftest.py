"""Suite-wide isolation from a developer's real Codex account registry."""

from __future__ import annotations

import pytest


_CODEX_PROFILE_ENV = (
    "CODEX_HOME",
    "CC_REMOTE_CODEX_PROFILES_JSON",
    "CC_REMOTE_CODEX_PROFILES_FILE",
)

_MISSING_WORKSPACE_RUNTIME = "/__cc_remote_test_workspace_runtime_missing__"


@pytest.fixture(autouse=True)
def _isolate_codex_profile_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make every test opt in to account/profile configuration explicitly.

    ``WrapperConfig`` and ``CodexProfileRegistry`` intentionally inherit the
    launching shell in production.  Unit tests must not inherit those values:
    otherwise a developer running pytest from a secondary ``CODEX_HOME`` gets
    a different registry and dozens of unrelated mock-signature failures.
    """
    for key in _CODEX_PROFILE_ENV:
        monkeypatch.delenv(key, raising=False)
    # Tests must not change shape based on whether the developer has installed
    # the Codex desktop app. Individual workspace-runtime tests opt in with a
    # complete temporary bundle.
    monkeypatch.setenv(
        "CC_REMOTE_CODEX_WORKSPACE_RUNTIME", _MISSING_WORKSPACE_RUNTIME)
