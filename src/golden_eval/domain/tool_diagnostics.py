"""Tool-call evidence: normalize AgentLogs into the 6.2 name-set diagnostic.

Both sides of a comparison must read the platform's log the same way, otherwise "expected" and
"actual" tool names are not comparable. The whitelist below is the verified platform contract: a
log whose type is one of `TOOL_LOG_TYPES` is an execution action a business reader can see, while
pure routing (`agent`), language detection, recall and `sa` logs are not actions at all.

6.2 is diagnostic only. Nothing here scores, gates or aggregates; it produces evidence a human or
the judge worklist reads alongside the reply.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from .models import AgentLog, ToolDiagnostics

TOOL_LOG_TYPES = frozenset({"task", "skill", "tool", "internalTool", "mcp", "knowledge"})
KNOWLEDGE_TOOL_NAME = "平台知识库检索"

# Some platform `tool` logs carry only the internal call id (a UUID or a long numeric id) and no
# business name. Such a name cannot be compared with a golden tool name, so it is labelled instead
# of being passed off as a business action.
UNRESOLVED_TOOL_TEMPLATE = "未解析工具({id})"
IDENTIFIER_PATTERN = re.compile(
    r"^(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|\d{12,}|[0-9a-f]{16,})$"
)

# Raw logs carry prompts and entity payloads; evidence keeps the shape but not megabytes.
MAX_LOG_TEXT = 400
MAX_LOG_VALUE_ITEMS = 8


def log_type(raw: dict[str, Any]) -> str:
    return str(raw.get("type") or "").strip()


def log_name(raw: dict[str, Any]) -> str:
    name = str(raw.get("name") or "").strip()
    if not name:
        return KNOWLEDGE_TOOL_NAME if log_type(raw) == "knowledge" else ""
    if IDENTIFIER_PATTERN.match(name):
        return UNRESOLVED_TOOL_TEMPLATE.format(id=name[:8])
    return name


def tool_logs(raw_logs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in raw_logs
        if isinstance(item, dict) and log_type(item) in TOOL_LOG_TYPES and log_name(item)
    ]


def dedupe_calls(raw_logs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per executed action: the platform repeats the same call in several messages."""

    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tool_logs(raw_logs):
        name = log_name(item)
        kind = log_type(item)
        key = str(item.get("toolCallId") or f"{kind}:{item.get('toolId') or ''}:{name}")
        if key in seen:
            continue
        seen.add(key)
        calls.append(
            {
                "name": name,
                "log_type": kind,
                "status": _optional_text(item.get("status")),
                "cost_time_ms": _optional_int(item.get("costTime") or item.get("cost_time_ms")),
            }
        )
    return calls


def tool_names(raw_logs: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({call["name"] for call in dedupe_calls(raw_logs)})


def normalize_agent_logs(raw_logs: Iterable[dict[str, Any]]) -> list[AgentLog]:
    """Bounded, typed projection of the platform's `agentLogs` for the observation record."""

    logs: list[AgentLog] = []
    for item in raw_logs:
        if not isinstance(item, dict):
            continue
        kind = log_type(item)
        if not kind:
            continue
        logs.append(
            AgentLog(
                type=kind,
                name=_optional_text(item.get("name")),
                status=_optional_text(item.get("status")),
                message=_bounded_text(item.get("message")),
                cost_time_ms=_optional_int(item.get("costTime") or item.get("cost_time_ms")),
            )
        )
    return logs


def bounded_raw_logs(raw_logs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the platform's own log shape as evidence, with long strings and payloads trimmed."""

    entries: list[dict[str, Any]] = []
    for item in raw_logs:
        if not isinstance(item, dict):
            continue
        entries.append({str(key): _bounded_value(value) for key, value in item.items()})
    return entries


def build_tool_diagnostics(
    expected_names: Sequence[str],
    actual_names: Sequence[str],
    *,
    expected_refs: int = 0,
    actual_refs: int = 0,
) -> ToolDiagnostics:
    expected = sorted({name for name in expected_names if name})
    actual = sorted({name for name in actual_names if name})
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    refs = [f"golden.tool_call:{index}" for index in range(1, max(expected_refs, 0) + 1)]
    if not refs:
        refs = [f"golden.tool_call:{index}" for index in range(1, len(expected) + 1)]
    refs.extend(f"actual.agent_log:{index}" for index in range(1, max(actual_refs, 0) + 1))
    if not refs:
        refs = ["golden.tool_calls (empty)", "actual.agent_logs (empty)"]
    return ToolDiagnostics(
        expected_tool_names=expected,
        actual_tool_names=actual,
        missing_tool_names=missing,
        unexpected_tool_names=unexpected,
        name_set_match=set(expected) == set(actual),
        evidence_refs=refs,
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bounded_text(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    return text[:MAX_LOG_TEXT]


def _bounded_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_LOG_TEXT]
    if isinstance(value, dict):
        entries = list(value.items())[:MAX_LOG_VALUE_ITEMS]
        return {str(key): _bounded_value(item) for key, item in entries}
    if isinstance(value, list):
        return [_bounded_value(item) for item in value[:MAX_LOG_VALUE_ITEMS]]
    return value
