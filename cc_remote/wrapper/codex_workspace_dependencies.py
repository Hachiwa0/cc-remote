"""Read-only bridge to the workspace runtime installed by the Codex desktop app.

The desktop app exposes these paths through the experimental app-server dynamic
tool ``codex_app.load_workspace_dependencies``.  Code sessions created by
cc-remote register the same narrow tool when a complete, locally installed
runtime can be verified.  The runtime remains owned and updated by the desktop
app; cc-remote neither installs packages nor mutates it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any, Optional


_RUNTIME_ENV = "CC_REMOTE_CODEX_WORKSPACE_RUNTIME"
_DEFAULT_RUNTIME = Path("~/.cache/codex-runtimes/codex-primary-runtime")
_MANIFEST_MAX_BYTES = 64 * 1024
_PACKAGE_MANIFEST_MAX_BYTES = 64 * 1024
_SUPPORTED_BUNDLE_FORMATS = frozenset({2})

WORKSPACE_DEPENDENCY_NAMESPACE = "codex_app"
WORKSPACE_DEPENDENCY_TOOL = "load_workspace_dependencies"
WORKSPACE_DEPENDENCY_REQUEST = "item/tool/call"

WORKSPACE_DEPENDENCY_DYNAMIC_TOOLS: list[dict[str, Any]] = [{
    "type": "namespace",
    "name": WORKSPACE_DEPENDENCY_NAMESPACE,
    "description": "Tools provided by the Codex app.",
    "tools": [{
        "type": "function",
        "name": WORKSPACE_DEPENDENCY_TOOL,
        "description": (
            "Locate the configured bundled workspace dependency runtime paths "
            "for this local desktop thread, including Node.js, Python, and "
            "useful libraries for working with spreadsheets, slide decks, "
            "Word documents, and PDFs. This is read-only and takes no arguments."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }],
}]


class WorkspaceDependencyError(RuntimeError):
    """The desktop workspace runtime is absent, incomplete, or untrusted."""


@dataclass(frozen=True)
class WorkspaceDependencies:
    root: Path
    bundle_version: str
    node: Path
    node_modules: Path
    python: Path
    python_packages: Path
    override_bin: Path
    fallback_bin: Path
    git: Optional[Path]
    pnpm: Optional[Path]

    def tool_text(self) -> str:
        lines = [
            "### Workspace Dependencies",
            f"- Bundle version: `{self.bundle_version}`",
        ]
        if self.git is not None:
            lines.append(f"- Git executable: `{self.git}`")
        lines.extend([
            f"- Node.js executable: `{self.node}`",
            f"- Node.js packages: `{self.node_modules}`",
        ])
        if self.pnpm is not None:
            lines.append(f"- pnpm executable: `{self.pnpm}`")
        lines.extend([
            f"- Python executable: `{self.python}`",
            f"- Python packages: `{self.python_packages}`",
            f"- Override binaries: `{self.override_bin}`",
            f"- Fallback binaries: `{self.fallback_bin}`",
        ])
        return "\n".join(lines)


def _read_json(path: Path, *, maximum: int) -> dict[str, Any]:
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size <= 0 or stat.st_size > maximum:
            raise WorkspaceDependencyError(f"invalid runtime metadata: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except WorkspaceDependencyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceDependencyError(
            f"cannot read runtime metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise WorkspaceDependencyError(f"invalid runtime metadata: {path.name}")
    return value


def _inside(root: Path, candidate: Path, *, executable: bool = False) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkspaceDependencyError(
            f"workspace runtime path escapes its root: {candidate.name}") from exc
    if not resolved.is_file():
        raise WorkspaceDependencyError(
            f"workspace runtime file is missing: {candidate.name}")
    if executable and not os.access(resolved, os.X_OK):
        raise WorkspaceDependencyError(
            f"workspace runtime file is not executable: {candidate.name}")
    # Return the stable in-bundle path rather than dereferencing benign internal
    # symlinks such as python3 -> python3.12 in the tool's user-visible response.
    return candidate


def _inside_dir(root: Path, candidate: Path) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkspaceDependencyError(
            f"workspace runtime path escapes its root: {candidate.name}") from exc
    if not resolved.is_dir():
        raise WorkspaceDependencyError(
            f"workspace runtime directory is missing: {candidate.name}")
    return candidate


def _expected_platform() -> tuple[str, str]:
    if sys.platform == "darwin":
        system = "darwin"
    elif sys.platform.startswith("linux"):
        system = "linux"
    elif sys.platform == "win32":
        system = "win32"
    else:
        system = sys.platform
    machine = platform.machine().lower()
    arch = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "x64",
        "x86_64": "x64",
    }.get(machine, machine)
    return system, arch


def _optional_executable(root: Path, path: Path) -> Optional[Path]:
    try:
        return _inside(root, path, executable=True)
    except WorkspaceDependencyError:
        return None


def workspace_runtime_root() -> Path:
    configured = os.environ.get(_RUNTIME_ENV)
    if configured is not None and not configured.strip():
        raise WorkspaceDependencyError("workspace runtime path is empty")
    selected = Path(configured) if configured is not None else _DEFAULT_RUNTIME
    try:
        return selected.expanduser().resolve(strict=True)
    except OSError as exc:
        raise WorkspaceDependencyError("workspace runtime is not installed") from exc


def load_workspace_dependencies(
    runtime_root: Optional[Path] = None,
) -> WorkspaceDependencies:
    """Validate and describe one desktop workspace runtime without executing it."""
    try:
        root = (
            runtime_root.expanduser().resolve(strict=True)
            if runtime_root is not None
            else workspace_runtime_root()
        )
    except OSError as exc:
        raise WorkspaceDependencyError("workspace runtime is not installed") from exc
    if not root.is_dir():
        raise WorkspaceDependencyError("workspace runtime root is not a directory")
    manifest_path = _inside(root, root / "runtime.json")
    manifest = _read_json(manifest_path, maximum=_MANIFEST_MAX_BYTES)
    bundle_format = manifest.get("bundleFormatVersion")
    if bundle_format not in _SUPPORTED_BUNDLE_FORMATS:
        raise WorkspaceDependencyError("unsupported workspace runtime format")
    bundle_version = manifest.get("bundleVersion")
    if (
        not isinstance(bundle_version, str)
        or not bundle_version
        or len(bundle_version) > 128
    ):
        raise WorkspaceDependencyError("invalid workspace runtime version")
    expected_platform, expected_arch = _expected_platform()
    if (
        manifest.get("targetPlatform") != expected_platform
        or manifest.get("targetArch") != expected_arch
    ):
        raise WorkspaceDependencyError(
            "workspace runtime does not match this platform")

    dependencies = _inside_dir(root, root / "dependencies")
    node_root = _inside_dir(root, dependencies / "node")
    node = _inside(
        root,
        node_root / "bin" / ("node.exe" if sys.platform == "win32" else "node"),
        executable=True,
    )
    node_modules = _inside_dir(root, node_root / "node_modules")
    artifact_manifest_path = _inside(
        root, node_modules / "@oai" / "artifact-tool" / "package.json")
    artifact_manifest = _read_json(
        artifact_manifest_path,
        maximum=_PACKAGE_MANIFEST_MAX_BYTES,
    )
    if artifact_manifest.get("name") != "@oai/artifact-tool":
        raise WorkspaceDependencyError("workspace artifact tool is invalid")
    expected_artifact_version = manifest.get("artifactToolVersion")
    if (
        not isinstance(expected_artifact_version, str)
        or not expected_artifact_version
        or len(expected_artifact_version) > 128
    ):
        raise WorkspaceDependencyError(
            "workspace artifact tool version is missing")
    if artifact_manifest.get("version") != expected_artifact_version:
        raise WorkspaceDependencyError(
            "workspace artifact tool version mismatch")

    python_root = _inside_dir(root, dependencies / "python")
    python = _inside(
        root,
        python_root / "bin" / ("python.exe" if sys.platform == "win32" else "python3"),
        executable=True,
    )
    bin_root = _inside_dir(root, dependencies / "bin")
    override_bin = _inside_dir(root, bin_root / "override")
    fallback_bin = _inside_dir(root, bin_root / "fallback")
    executable_suffix = ".exe" if sys.platform == "win32" else ""
    git = _optional_executable(
        root, fallback_bin / f"git{executable_suffix}")
    pnpm = _optional_executable(
        root, fallback_bin / f"pnpm{executable_suffix}")

    return WorkspaceDependencies(
        root=root,
        bundle_version=bundle_version,
        node=node,
        node_modules=node_modules,
        python=python,
        python_packages=python_root,
        override_bin=override_bin,
        fallback_bin=fallback_bin,
        git=git,
        pnpm=pnpm,
    )


def discover_workspace_dependencies() -> Optional[WorkspaceDependencies]:
    """Return a verified runtime, or ``None`` when desktop support is absent."""
    try:
        return load_workspace_dependencies()
    except WorkspaceDependencyError:
        return None


def workspace_dependency_response(
    dependencies: Optional[WorkspaceDependencies],
) -> dict[str, Any]:
    if dependencies is None:
        return {
            "success": False,
            "contentItems": [{
                "type": "inputText",
                "text": (
                    "Workspace dependencies are unavailable for this local "
                    "thread. The Codex desktop workspace runtime is not installed "
                    "or did not pass validation."
                ),
            }],
        }
    return {
        "success": True,
        "contentItems": [{
            "type": "inputText",
            "text": dependencies.tool_text(),
        }],
    }
