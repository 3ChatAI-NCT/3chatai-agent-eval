"""6.2 工具诊断：金标侧与执行侧必须按同一套白名单读出可比较的工具名。"""

from __future__ import annotations

from datetime import UTC, datetime

from golden_eval.adapters.jsonl_corpus import JsonlCorpusStore
from golden_eval.application.golden_build import (
    build_draft_request,
    resolve_available_tools,
)
from golden_eval.domain.corpus import (
    CollectionPolicy,
    CorpusSnapshot,
    FunnelCounts,
    ObservedToolCall,
    SenderRole,
    SessionOutcome,
    SourceMessage,
    ToolDefinition,
    WorkspaceConfiguration,
    WorkspaceOutcome,
)
from golden_eval.domain.models import BuildDraftFromSnapshotRequest
from golden_eval.domain.tool_diagnostics import (
    bounded_raw_logs,
    build_tool_diagnostics,
    normalize_agent_logs,
    tool_names,
)

NOW = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)
KNOWLEDGE = "平台知识库检索"


def _source_message(seq: int, role: SenderRole, content: str, tools: list[str]) -> SourceMessage:
    return SourceMessage(
        seq=seq,
        role=role,
        content=content,
        text=content,
        sent_at=NOW,
        tool_calls=[ObservedToolCall(name=name, log_type="task") for name in tools],
    )


def _snapshot() -> CorpusSnapshot:
    messages = [
        _source_message(1, SenderRole.VISITOR, "券怎么验", []),
        _source_message(2, SenderRole.AI, "请把券码发我", ["小管家-团购验券与进场指引"]),
        _source_message(3, SenderRole.VISITOR, "现在还能进场吗", []),
        _source_message(
            4,
            SenderRole.AI,
            "可以，帮您登记",
            ["小管家-团购验券与进场指引", "配置企微标签"],
        ),
    ]
    return CorpusSnapshot(
        snapshot_id="snap-tools",
        created_at=NOW,
        policy=CollectionPolicy(),
        source_hash="hash-tools",
        session_count=1,
        counts=FunnelCounts(scanned=1, included=1),
        workspaces=[
            WorkspaceOutcome(
                workspace_id="ws-source",
                configuration=WorkspaceConfiguration(
                    workspace_id="ws-source", config_hash="cfg-1", captured_at=NOW
                ),
                available_tools=[
                    ToolDefinition(name="配置企微标签", description="写入企微标签"),
                    ToolDefinition(name="发送门店地址的具体定位", description="发送定位"),
                ],
                target_count=1,
                selected_count=1,
                counts=FunnelCounts(scanned=1, included=1),
                sessions=[
                    SessionOutcome(
                        workspace_id="ws-source",
                        session_id="GD-TOOLS-0001",
                        included=True,
                        publishable_turn_count=2,
                        first_customer_message_at=NOW,
                        messages=messages,
                    )
                ],
            )
        ],
    )


def test_agent_logs_map_to_comparable_tool_names() -> None:
    logs = [
        {"type": "task", "name": "配置企微标签", "status": None, "costTime": 900},
        {"type": "task", "name": "配置企微标签", "costTime": 900},
        {"type": "knowledge", "name": None},
        {"type": "agent", "name": "mainagent"},
        {"type": "language", "message": "简体中文"},
        {"type": "recall", "name": None},
    ]

    assert tool_names(logs) == [KNOWLEDGE, "配置企微标签"]
    normalized = normalize_agent_logs(logs)
    assert [log.type for log in normalized] == [
        "task",
        "task",
        "knowledge",
        "agent",
        "language",
        "recall",
    ]
    assert normalized[0].cost_time_ms == 900


def test_tool_diagnostics_is_a_name_set_and_never_a_score() -> None:
    diagnostic = build_tool_diagnostics(
        ["配置企微标签", KNOWLEDGE],
        ["配置企微标签"],
        expected_refs=2,
        actual_refs=1,
    )

    assert diagnostic.expected_tool_names == [KNOWLEDGE, "配置企微标签"]
    assert diagnostic.missing_tool_names == [KNOWLEDGE]
    assert diagnostic.unexpected_tool_names == []
    assert diagnostic.name_set_match is False
    assert diagnostic.evidence_refs == [
        "golden.tool_call:1",
        "golden.tool_call:2",
        "actual.agent_log:1",
    ]
    assert "score" not in diagnostic.model_dump()
    assert "status" not in diagnostic.model_dump()


def test_raw_logs_are_bounded_before_they_become_evidence() -> None:
    bounded = bounded_raw_logs([{"type": "task", "instructionPrompt": "长" * 2000}])

    assert len(bounded[0]["instructionPrompt"]) == 400
    assert bounded[0]["type"] == "task"


def test_platform_internal_ids_are_not_passed_off_as_tool_names() -> None:
    """平台只回传内部调用 id 时，6.2 必须标成未解析，而不是拿 UUID 当业务工具名。"""

    logs = [
        {"type": "tool", "name": "b7bf2358-4c35-4b89-9906-41939af44d6f", "message": "b7bf2358"},
        {"type": "tool", "name": "163040328155766785"},
        {"type": "task", "name": "小管家-欢迎承接"},
    ]

    assert tool_names(logs) == ["小管家-欢迎承接", "未解析工具(16304032)", "未解析工具(b7bf2358)"]
def test_golden_draft_carries_expected_tools_and_a_publishable_catalog() -> None:
    request, _, _ = build_draft_request(_snapshot(), BuildDraftFromSnapshotRequest())

    session = request.sessions[0]
    assert [call.name for call in session.turns[0].expected.tool_calls] == [
        "小管家-团购验券与进场指引"
    ]
    assert [call.name for call in session.turns[1].expected.tool_calls] == [
        "小管家-团购验券与进场指引",
        "配置企微标签",
    ]
    assert (
        "【工具】必须观察到调用：小管家-团购验券与进场指引；只判断是否调用。"
        in session.turns[0].rubric
    )

    names = [tool.name for tool in request.shared.context.available_tools]
    # 配置里声明的工具与历史里真实发生的动作合并成可用工具清单，金标工具名才能通过发布校验。
    assert names == ["配置企微标签", "发送门店地址的具体定位", "小管家-团购验券与进场指引"]
    assert resolve_available_tools(_snapshot())[0].description.startswith("写入企微标签")
    # 发布时的共享合同校验必须在金标工具名上通过，否则带工具的草案一律发不出去。
    JsonlCorpusStore._validate_shared_contract(request.shared, session)
