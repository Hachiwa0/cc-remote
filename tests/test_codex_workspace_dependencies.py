from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cc_remote.wrapper import codex_workspace_dependencies as workspace


def _executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "codex-primary-runtime"
    system, arch = workspace._expected_platform()
    root.mkdir()
    (root / "runtime.json").write_text(json.dumps({
        "artifactToolVersion": "2.8.43",
        "bundleFormatVersion": 2,
        "bundleVersion": "26.813.12317",
        "targetArch": arch,
        "targetPlatform": system,
    }), encoding="utf-8")
    _executable(root / "dependencies/node/bin/node")
    _executable(root / "dependencies/python/bin/python3")
    _executable(root / "dependencies/bin/fallback/git")
    _executable(root / "dependencies/bin/fallback/pnpm")
    (root / "dependencies/bin/override").mkdir(parents=True)
    package = root / "dependencies/node/node_modules/@oai/artifact-tool/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({
        "name": "@oai/artifact-tool",
        "version": "2.8.43",
    }), encoding="utf-8")
    return root


def test_workspace_runtime_matches_desktop_tool_contract(tmp_path):
    root = _runtime(tmp_path)
    dependencies = workspace.load_workspace_dependencies(root)

    assert dependencies.bundle_version == "26.813.12317"
    text = dependencies.tool_text()
    assert text.startswith(
        "### Workspace Dependencies\n- Bundle version: `26.813.12317`")
    assert f"- Node.js executable: `{root}/dependencies/node/bin/node`" in text
    assert (
        f"- Node.js packages: `{root}/dependencies/node/node_modules`" in text)
    assert f"- Python executable: `{root}/dependencies/python/bin/python3`" in text
    assert f"- Python packages: `{root}/dependencies/python`" in text
    assert f"- Override binaries: `{root}/dependencies/bin/override`" in text
    assert f"- Fallback binaries: `{root}/dependencies/bin/fallback`" in text
    response = workspace.workspace_dependency_response(dependencies)
    assert response == {
        "success": True,
        "contentItems": [{"type": "inputText", "text": text}],
    }


def test_workspace_runtime_discovery_is_optional(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setenv("CC_REMOTE_CODEX_WORKSPACE_RUNTIME", str(missing))
    assert workspace.discover_workspace_dependencies() is None
    response = workspace.workspace_dependency_response(None)
    assert response["success"] is False
    assert response["contentItems"][0]["type"] == "inputText"


def test_workspace_runtime_rejects_artifact_version_mismatch(tmp_path):
    root = _runtime(tmp_path)
    package = root / "dependencies/node/node_modules/@oai/artifact-tool/package.json"
    package.write_text(json.dumps({
        "name": "@oai/artifact-tool",
        "version": "0.0.0",
    }), encoding="utf-8")

    with pytest.raises(
        workspace.WorkspaceDependencyError,
        match="version mismatch",
    ):
        workspace.load_workspace_dependencies(root)


def test_workspace_runtime_requires_pinned_artifact_version(tmp_path):
    root = _runtime(tmp_path)
    metadata = json.loads((root / "runtime.json").read_text(encoding="utf-8"))
    metadata.pop("artifactToolVersion")
    (root / "runtime.json").write_text(
        json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        workspace.WorkspaceDependencyError,
        match="version is missing",
    ):
        workspace.load_workspace_dependencies(root)


def test_workspace_runtime_rejects_symlink_escape(tmp_path):
    root = _runtime(tmp_path)
    outside = tmp_path / "outside-package.json"
    outside.write_text(json.dumps({
        "name": "@oai/artifact-tool",
        "version": "2.8.43",
    }), encoding="utf-8")
    package = root / "dependencies/node/node_modules/@oai/artifact-tool/package.json"
    package.unlink()
    os.symlink(outside, package)

    with pytest.raises(
        workspace.WorkspaceDependencyError,
        match="escapes its root",
    ):
        workspace.load_workspace_dependencies(root)


def test_workspace_dynamic_tool_definition_is_narrow():
    assert workspace.WORKSPACE_DEPENDENCY_DYNAMIC_TOOLS == [{
        "type": "namespace",
        "name": "codex_app",
        "description": "Tools provided by the Codex app.",
        "tools": [{
            "type": "function",
            "name": "load_workspace_dependencies",
            "description": (
                "Locate the configured bundled workspace dependency runtime "
                "paths for this local desktop thread, including Node.js, "
                "Python, and useful libraries for working with spreadsheets, "
                "slide decks, Word documents, and PDFs. This is read-only and "
                "takes no arguments."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }],
    }]
