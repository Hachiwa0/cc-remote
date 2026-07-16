"""Bounded, display-safe discovery of real engine extensions.

Codex exposes first-class app-server inventory RPCs. Claude currently exposes
plugins through its CLI and skills as local manifests, so discovery is read-only
and deliberately avoids importing settings, credentials, schemas or plugin code.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from cc_remote.wrapper.child_env import sanitized_child_env
from cc_remote.wrapper.claude_runtime import resolve_claude_cli
from cc_remote.wrapper.codex_rpc import CodexRpcOutcomeUnknown, codex_rpc

_COMPONENT_TIMEOUT = 8.0
_MAX_ITEMS = 500
_MAX_MANIFEST_BYTES = 128 * 1024


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _source_kind(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return _text(value.get("type"), 256)


def _public_url(value: Any) -> str | None:
    value = _text(value, 4096)
    if not value:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme == "https" and parsed.hostname else None


async def _codex_component(method: str, params: dict[str, Any], cwd: str):
    return await asyncio.wait_for(
        codex_rpc(method, params, cwd=cwd), timeout=_COMPONENT_TIMEOUT)


async def codex_capabilities(cwd: str, space: str) -> tuple[list[dict], list[str], list[str]]:
    requests = {
        "skills": ("skills/list", {"cwds": [cwd], "forceReload": False}),
        "plugins": ("plugin/list", {"cwds": [cwd]}),
        "apps": ("app/list", {"limit": _MAX_ITEMS}),
        "mcp": ("mcpServerStatus/list", {}),
    }
    results = await asyncio.gather(*(
        _codex_component(method, params, cwd)
        for method, params in requests.values()
    ), return_exceptions=True)
    values = dict(zip(requests, results))
    items: list[dict] = []
    errors: list[str] = []
    notes: list[str] = []

    raw_skills = values["skills"]
    if isinstance(raw_skills, Exception):
        errors.append("skills: app-server request failed")
    elif isinstance(raw_skills, dict):
        for entry in raw_skills.get("data") or []:
            if not isinstance(entry, dict):
                continue
            for skill in entry.get("skills") or []:
                if not isinstance(skill, dict) or len(items) >= _MAX_ITEMS:
                    break
                name = _text(skill.get("name"), 512)
                if not name:
                    continue
                interface = skill.get("interface") if isinstance(skill.get("interface"), dict) else {}
                items.append({
                    "kind": "skill", "id": name, "name": name,
                    "description": (_text(interface.get("shortDescription"), 16 * 1024)
                                    or _text(skill.get("shortDescription"), 16 * 1024)
                                    or _text(skill.get("description"), 16 * 1024)),
                    "enabled": bool(skill.get("enabled")),
                    "scope": _text(skill.get("scope"), 256),
                })

    raw_plugins = values["plugins"]
    if isinstance(raw_plugins, Exception):
        errors.append("plugins: app-server request failed")
    elif isinstance(raw_plugins, dict):
        for market in raw_plugins.get("marketplaces") or []:
            if not isinstance(market, dict):
                continue
            market_name = _text(market.get("name"), 256)
            for plugin in market.get("plugins") or []:
                if not isinstance(plugin, dict) or len(items) >= _MAX_ITEMS * 2:
                    break
                plugin_id = _text(plugin.get("id"), 512)
                name = _text(plugin.get("name"), 512) or plugin_id
                if not plugin_id or not name:
                    continue
                interface = plugin.get("interface") if isinstance(plugin.get("interface"), dict) else {}
                items.append({
                    "kind": "plugin", "id": plugin_id, "name": name,
                    "description": (_text(interface.get("shortDescription"), 16 * 1024)
                                    or _text(interface.get("longDescription"), 16 * 1024)),
                    "enabled": bool(plugin.get("enabled")),
                    "installed": bool(plugin.get("installed")),
                    "status": _text(plugin.get("availability"), 256),
                    "scope": market_name,
                    "source": _source_kind(plugin.get("source")),
                })

    raw_apps = values["apps"]
    if isinstance(raw_apps, Exception):
        errors.append("apps: app-server request failed")
    elif isinstance(raw_apps, dict):
        for app in (raw_apps.get("data") or [])[:_MAX_ITEMS]:
            if not isinstance(app, dict):
                continue
            app_id = _text(app.get("id"), 512)
            name = _text(app.get("name"), 512) or app_id
            if not app_id or not name:
                continue
            items.append({
                "kind": "app", "id": app_id, "name": name,
                "description": _text(app.get("description"), 16 * 1024),
                "enabled": bool(app.get("isEnabled", True)),
                "status": "accessible" if app.get("isAccessible") else "link required",
                "source": _text(app.get("distributionChannel"), 256),
                "install_url": _public_url(app.get("installUrl")),
            })

    raw_mcp = values["mcp"]
    if isinstance(raw_mcp, Exception):
        errors.append("mcp: app-server request failed")
    elif isinstance(raw_mcp, dict):
        for server in (raw_mcp.get("data") or [])[:_MAX_ITEMS]:
            if not isinstance(server, dict):
                continue
            name = _text(server.get("name"), 512)
            if not name:
                continue
            info = server.get("serverInfo") if isinstance(server.get("serverInfo"), dict) else {}
            tools = server.get("tools") if isinstance(server.get("tools"), dict) else {}
            resources = server.get("resources") if isinstance(server.get("resources"), list) else []
            templates = server.get("resourceTemplates") if isinstance(server.get("resourceTemplates"), list) else []
            items.append({
                "kind": "mcp", "id": name, "name": _text(info.get("title"), 512) or name,
                "description": _text(info.get("description"), 16 * 1024),
                "status": _text(server.get("authStatus"), 256),
                "tool_count": len(tools),
                "resource_count": len(resources) + len(templates),
            })

    if space == "work":
        notes.append("Work 中的实际可用范围仍受私有目录、禁网和权限策略限制。")
    return items[:2000], errors[:32], notes


def _manifest_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_BYTES:
            return None, None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end < 0:
        return None, None
    frontmatter = text[3:end]
    name = re.search(r"(?m)^name:\s*[\"']?([^\n\"']+)", frontmatter)
    description = re.search(r"(?m)^description:\s*[\"']?([^\n\"']+)", frontmatter)
    return (
        _text(name.group(1), 512) if name else None,
        _text(description.group(1), 16 * 1024) if description else None,
    )


def _claude_skills(cwd: str) -> list[dict]:
    roots = [Path.home() / ".claude" / "skills", Path(cwd) / ".claude" / "skills"]
    items: list[dict] = []
    seen: set[str] = set()
    for root, scope in ((roots[0], "user"), (roots[1], "project")):
        try:
            candidates = list(root.iterdir())[:_MAX_ITEMS]
        except OSError:
            continue
        for candidate in candidates:
            manifest = candidate / "SKILL.md"
            name, description = _manifest_metadata(manifest)
            name = name or _text(candidate.name, 512)
            if not name or name in seen:
                continue
            seen.add(name)
            items.append({
                "kind": "skill", "id": name, "name": name,
                "description": description, "enabled": True, "scope": scope,
            })
    return items


async def _claude_plugins(binary: str) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        binary, "plugin", "list", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=sanitized_child_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), _COMPONENT_TIMEOUT)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0 or len(stdout) > 4 * 1024 * 1024:
        raise RuntimeError("claude plugin list failed")
    raw = json.loads(stdout)
    if not isinstance(raw, list):
        raise RuntimeError("claude plugin list returned invalid data")
    items: list[dict] = []
    for plugin in raw[:_MAX_ITEMS]:
        if not isinstance(plugin, dict):
            continue
        plugin_id = _text(plugin.get("id") or plugin.get("name"), 512)
        name = _text(plugin.get("name") or plugin_id, 512)
        if not plugin_id or not name:
            continue
        items.append({
            "kind": "plugin", "id": plugin_id, "name": name,
            "description": _text(plugin.get("description"), 16 * 1024),
            "enabled": bool(plugin.get("enabled", True)), "installed": True,
            "scope": _text(plugin.get("scope"), 256), "source": "claude-cli",
        })
    return items


async def _codex_plugin_inventory(cwd: str) -> AsyncIterator[tuple[dict, dict]]:
    raw = await _codex_component("plugin/list", {"cwds": [cwd]}, cwd)
    if not isinstance(raw, dict):
        return
    for marketplace in raw.get("marketplaces") or []:
        if not isinstance(marketplace, dict):
            continue
        for plugin in marketplace.get("plugins") or []:
            if isinstance(plugin, dict):
                yield marketplace, plugin


async def _codex_plugin_state(plugin_id: str, cwd: str):
    async for marketplace, plugin in _codex_plugin_inventory(cwd):
        if _text(plugin.get("id"), 512) == plugin_id:
            return marketplace, plugin
    raise ValueError("Codex 插件不存在或当前目录不可见")


async def _manage_codex_plugin(plugin_id: str, action: str, cwd: str) -> None:
    marketplace, plugin = await _codex_plugin_state(plugin_id, cwd)
    installed = bool(plugin.get("installed"))
    if installed == (action == "install"):
        return
    if action == "uninstall":
        method, params = "plugin/uninstall", {"pluginId": plugin_id}
    else:
        name = _text(plugin.get("name"), 512)
        if not name:
            raise ValueError("Codex 插件缺少安装名称")
        params: dict[str, Any] = {"pluginName": name}
        marketplace_path = _text(marketplace.get("path"), 4096)
        marketplace_name = _text(marketplace.get("name"), 512)
        if marketplace_path:
            params["marketplacePath"] = marketplace_path
        elif marketplace_name:
            params["remoteMarketplaceName"] = marketplace_name
        method = "plugin/install"
    try:
        await codex_rpc(method, params, cwd=cwd)
    except CodexRpcOutcomeUnknown:
        _, current = await _codex_plugin_state(plugin_id, cwd)
        if bool(current.get("installed")) != (action == "install"):
            raise


async def _manage_claude_plugin(
    plugin_id: str, action: str, binary: str
) -> None:
    verb = "install" if action == "install" else "uninstall"
    proc = await asyncio.create_subprocess_exec(
        binary, "plugin", verb, plugin_id,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env=sanitized_child_env(),
    )
    try:
        await asyncio.wait_for(proc.wait(), 60.0)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise ValueError(f"Claude 插件{('安装' if action == 'install' else '卸载')}失败")


async def manage_engine_plugin(
    engine: str,
    plugin_id: str,
    action: str,
    cwd: str,
    *,
    space: str = "code",
    claude_bin: str = "",
) -> None:
    if space == "work":
        raise ValueError("Work 不允许修改引擎插件")
    target = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(target):
        raise ValueError("插件目录不存在")
    if engine == "codex":
        await _manage_codex_plugin(plugin_id, action, target)
    else:
        binary, _ = resolve_claude_cli(claude_bin)
        await _manage_claude_plugin(plugin_id, action, binary)


async def claude_capabilities(
    cwd: str, space: str, claude_bin: str = ""
) -> tuple[list[dict], list[str], list[str]]:
    if space == "work":
        return [], [], [
            "Claude Work 为防止 Code 配置泄漏，明确禁用了用户/项目技能、插件、Hook 与 MCP。"
        ]
    items = await asyncio.to_thread(_claude_skills, cwd)
    errors: list[str] = []
    try:
        binary, _ = resolve_claude_cli(claude_bin)
        items.extend(await _claude_plugins(binary))
    except Exception:
        errors.append("plugins: claude CLI request failed")
    return items[:2000], errors, []


async def engine_capabilities(
    engine: str, cwd: str, space: str, claude_bin: str = ""
):
    target = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(target):
        raise ValueError("capability cwd does not exist")
    if engine == "codex":
        return await codex_capabilities(target, space)
    return await claude_capabilities(target, space, claude_bin)
