#!/usr/bin/env python3
"""Validate that a web build and backend use the same wire protocol."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


class ProtocolBundleError(ValueError):
    pass


def backend_protocol(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ProtocolBundleError(f"cannot read backend protocol from {path}") from exc
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id == "PROTOCOL_VERSION":
            value = ast.literal_eval(statement.value)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
            break
    raise ProtocolBundleError(f"valid PROTOCOL_VERSION not found in {path}")


def web_protocol(path: Path) -> int:
    try:
        manifest: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProtocolBundleError(f"cannot read web protocol from {path}") from exc
    value = manifest.get("protocol") if isinstance(manifest, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProtocolBundleError(f"valid protocol not found in {path}")
    return value


def validate_protocol_bundle(backend: Path, manifest: Path) -> int:
    backend_value = backend_protocol(backend)
    web_value = web_protocol(manifest)
    if backend_value != web_value:
        raise ProtocolBundleError(
            f"protocol mismatch: backend v{backend_value}, web v{web_value}"
        )
    return backend_value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backend", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        version = validate_protocol_bundle(args.backend, args.manifest)
    except ProtocolBundleError as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
