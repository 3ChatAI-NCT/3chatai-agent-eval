"""L1 确定性检查：机械可判的事实先由代码判掉，不交给模型。

结果作为 rule_checks 写进盲评 packet，让评审器在同一份事实基础上只做语义判断，
同时在报告中单独展示。
"""

from __future__ import annotations

import re

from .models import EvalCase, Issue, Severity, ToolCall

_PUNCT = re.compile(r"[\s,.;:!?，。；：！？、“”\"'()（）]+")
QUERY_KEYS = ("query", "q", "question", "keyword")


def normalize(text: str) -> str:
    return _PUNCT.sub("", text).lower()


def _query_of(call: ToolCall) -> str:
    for key in QUERY_KEYS:
        value = call.arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


PROFILE_TOOL_PREFIX = "tool_update_visitor"


def _profile_fields(call: ToolCall) -> dict[str, str]:
    """取出这次调用要写入的画像字段。

    画像写入工具的命名是 `tool_update_visitor_<字段>`，参数键是内部字段名；
    而上下文卡片里的字段名是中文展示名，两者对不上，所以键和值都要参与比对。
    """

    if not call.name.startswith(PROFILE_TOOL_PREFIX):
        return {}
    return {
        key: str(value).strip()
        for key, value in call.arguments.items()
        if isinstance(value, (str, int, float)) and str(value).strip()
    }


def duplicate_writes(call: ToolCall, snapshot: dict[str, str]) -> dict[str, str]:
    """返回 {写入字段: 与之重复的已有字段}。

    先按字段名比对；字段名对不上时按值比对（值太短容易误判，所以要求归一化后至少 2 个字符）。
    """

    existing = {key: normalize(value) for key, value in snapshot.items()}
    found: dict[str, str] = {}
    for key, value in _profile_fields(call).items():
        normalized = normalize(value)
        if not normalized:
            continue
        if key in existing and existing[key] == normalized:
            found[key] = key
            continue
        if len(normalized) < 2:
            continue
        match = next((name for name, seen in existing.items() if seen == normalized), None)
        if match is not None:
            found[key] = match
    return found


def run_checks(case: EvalCase, side_id: str) -> list[Issue]:
    output = case.output_for(side_id)
    if output is None:
        return []
    issues: list[Issue] = []
    available = set(case.available_tools)
    history_queries = {
        (call.name, normalize(_query_of(call)))
        for call in case.history_tool_calls
        if _query_of(call)
    }
    seen_queries: set[tuple[str, str]] = set()

    for index, call in enumerate(output.tool_calls):
        ref = f"/outputs/{side_id}/tool_calls/{index}"

        if available and call.name not in available:
            issues.append(
                Issue(
                    dimension="tool-contract",
                    severity=Severity.CRITICAL,
                    summary=f"调用了请求 tools 中不存在的工具 {call.name}",
                    evidence_ref=ref,
                )
            )

        repeated = duplicate_writes(call, case.profile_snapshot)
        if repeated:
            fields = "、".join(
                key if key == match else f"{key}（与已有「{match}」相同）"
                for key, match in sorted(repeated.items())
            )
            issues.append(
                Issue(
                    dimension="duplicate-action",
                    severity=Severity.MAJOR,
                    summary=f"画像写入 {fields} 的值与已有快照相同",
                    evidence_ref=ref,
                )
            )

        query = _query_of(call)
        if query:
            key = (call.name, normalize(query))
            if key in history_queries:
                issues.append(
                    Issue(
                        dimension="duplicate-action",
                        severity=Severity.MAJOR,
                        summary=f"{call.name} 的检索 query 与历史调用完全一致",
                        evidence_ref=ref,
                    )
                )
            elif key in seen_queries:
                issues.append(
                    Issue(
                        dimension="duplicate-action",
                        severity=Severity.MAJOR,
                        summary=f"本轮重复发起了相同的 {call.name} 检索",
                        evidence_ref=ref,
                    )
                )
            seen_queries.add(key)

    return issues
