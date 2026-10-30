"""Snapshot -> Golden Draft.

The conversion is deliberately mechanical: source turns become v0.4 sessions, turns with a human
reply never become gold turns, and third-party messages are already gone from the snapshot.
Anything semantic (rubric, profile golden, expected-answer quality) is left for annotation or the
Profile Enrichment Prompt, and every generated session is marked `needs_annotation` as a hint for
the human review pass. Publishing is gated on rubric and profile completeness, not on that marker.
Anomalies are reported instead of silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from ..domain.corpus import (
    CorpusSnapshot,
    ProfileFieldDefinition,
    SenderRole,
    SessionOutcome,
    SourceMessage,
)
from ..domain.message_shaping import EMPTY_MESSAGE, golden_content_from_source
from ..domain.models import (
    REVIEW_STATUS_NEEDS_ANNOTATION,
    BuildDraftFromSnapshotRequest,
    ConversationMessage,
    CreateGoldenDraftRequest,
    CustomerMemorySpec,
    DraftBuildAnomaly,
    GoldenAttachment,
    GoldenAvailableTool,
    GoldenDatasetHeader,
    GoldenMemoryAfter,
    GoldenMemoryBefore,
    GoldenMessage,
    GoldenProfileField,
    GoldenProfileUpdate,
    GoldenSession,
    GoldenSessionExpected,
    GoldenSessionInput,
    GoldenSessionMeta,
    GoldenShared,
    GoldenSharedContext,
    GoldenToolCall,
    GoldenToolParam,
    GoldenTurn,
    GoldenTurnExpected,
    GoldenTurnInput,
    ProfileUpdateSource,
)
from .collection import group_source_turns
from .rubric_draft import draft_turn_rubric


def build_session(
    outcome: SessionOutcome,
    *,
    snapshot_id: str,
    profile_keys: Sequence[str],
    annotated_at: str,
) -> tuple[GoldenSession | None, list[DraftBuildAnomaly]]:
    anomalies: list[DraftBuildAnomaly] = []
    grouping = group_source_turns(outcome.messages)

    for role in grouping.unsupported_roles:
        anomalies.append(
            _anomaly(outcome, "unsupported_role", f"role {int(role)} breaks the source turn")
        )
    if grouping.unsolicited_replies:
        anomalies.append(
            _anomaly(
                outcome,
                "unsolicited_reply",
                f"{len(grouping.unsolicited_replies)} replies arrived before any customer message",
            )
        )
    # 与参考实现一致的发布前检查：媒体消息缺链接无法回放，空消息说明取值异常。
    unreplayable = [
        message
        for message in outcome.messages
        if message.content_type != "text"
        and message.attachments
        and any(not str(item.get("url") or "").strip() for item in message.attachments)
    ]
    if unreplayable:
        anomalies.append(
            _anomaly(
                outcome,
                "contains_unreplayable_media",
                f"{len(unreplayable)} media messages have attachments without url",
            )
        )
    empty_messages = [
        message for message in outcome.messages if message.content == EMPTY_MESSAGE
    ]
    if empty_messages:
        anomalies.append(
            _anomaly(
                outcome,
                "contains_empty_message",
                f"{len(empty_messages)} messages have neither text nor attachments",
            )
        )
    for turn in grouping.turns:
        if turn.human_reply:
            anomalies.append(
                _anomaly(
                    outcome,
                    "human_reply_turn_excluded",
                    f"source turn {turn.seq} has a human reply; kept in source evidence only",
                )
            )
        elif not turn.answered:
            # 客户说完、Agent 还没回（通常是会话最后一段）：没有可对照的金标回复，
            # 消息仍留在 history 里，但不作为计分 Turn。
            anomalies.append(
                _anomaly(
                    outcome,
                    "no_reply_turn_excluded",
                    f"source turn {turn.seq} has no AI reply; kept in source evidence only",
                )
            )

    gold_turns = [turn for turn in grouping.turns if turn.publishable]
    if not gold_turns:
        anomalies.append(
            _anomaly(outcome, "no_publishable_turn", "session has no gold-eligible turn")
        )
        return None, anomalies

    first_profile_snapshot = {key: "" for key in profile_keys}
    profile_snapshot = dict(first_profile_snapshot)
    first_customer_at = min(message.sent_at for message in gold_turns[0].customer)
    as_of = first_customer_at.date().isoformat()
    session_id = outcome.session_id
    history, history_index_by_seq = _source_history(outcome)

    turns: list[GoldenTurn] = []
    history_texts: list[str] = []
    # 人工回复 / 没有回复的 Turn 不进金标，但平台在它们里写过的画像字段必须跟着往下带，
    # 否则后面 Turn 的起点画像会比线上真实状态少一截。
    carried_replies: list[SourceMessage] = []
    for turn in grouping.turns:
        if not turn.publishable:
            carried_replies.extend(turn.replies)
            continue
        index = len(turns) + 1
        expected_source = turn.replies
        customer_texts = [message.content for message in turn.customer]
        expected_texts = [message.content for message in expected_source]
        tool_calls = _turn_tool_calls(turn.replies)
        tool_names = [call.name for call in tool_calls]
        # 平台自己记账的画像写入直接成为本轮增量：这些字段不必（也不许）由补全模型猜。
        profile_update = _pulled_profile_updates(
            [*carried_replies, *turn.replies], profile_snapshot, allowed=set(profile_keys)
        )
        carried_replies = []
        profile_snapshot = {
            **profile_snapshot,
            **{key: update.value for key, update in profile_update.items()},
        }
        turns.append(
            GoldenTurn(
                turn_id=f"{session_id}#T{index}",
                session_id=session_id,
                seq=index,
                history_length=history_index_by_seq.get(
                    min(message.seq for message in turn.customer), 0
                ),
                input=GoldenTurnInput(
                    customer_messages=[
                        _golden_message(message, seq)
                        for seq, message in enumerate(turn.customer, start=1)
                    ]
                ),
                expected=GoldenTurnExpected(
                    messages=[
                        _golden_message(message, seq)
                        for seq, message in enumerate(expected_source, start=1)
                    ],
                    profile_update=profile_update,
                    profile_snapshot=dict(profile_snapshot),
                    state=None,
                    tool_calls=tool_calls,
                ),
                rubric=draft_turn_rubric(
                    customer_texts=customer_texts,
                    expected_texts=expected_texts,
                    history_texts=list(history_texts),
                    tool_names=tool_names,
                ),
            )
        )
        history_texts.extend(customer_texts)
        history_texts.extend(expected_texts)

    session = GoldenSession(
        session_id=session_id,
        meta=GoldenSessionMeta(
            channel="source",
            source=f"snapshot:{snapshot_id}",
            annotator="",
            annotated_at=annotated_at,
            review_status=REVIEW_STATUS_NEEDS_ANNOTATION,
            # v0.4 的 meta 字段保留位置，但自动生成阶段不编造「这条 case 想测什么」。
            design_intent="",
        ),
        input=GoldenSessionInput(
            memory_before=GoldenMemoryBefore(
                as_of=as_of,
                is_first_contact=True,
                profile_snapshot=dict(first_profile_snapshot),
                state_snapshot={},
                customer_memory="",
                note=f"由快照 {snapshot_id} 的线上历史生成，无更早记忆。",
            )
        ),
        history=history,
        turns=turns,
        expected=GoldenSessionExpected(
            memory_after=GoldenMemoryAfter(
                as_of=as_of,
                profile_snapshot=dict(profile_snapshot),
                state_snapshot={},
                customer_memory="",
            )
        ),
    )
    return session, anomalies


def build_draft_request(
    snapshot: CorpusSnapshot,
    request: BuildDraftFromSnapshotRequest,
    *,
    annotated_at: str | None = None,
) -> tuple[CreateGoldenDraftRequest, list[DraftBuildAnomaly], dict[str, int]]:
    moment = annotated_at or datetime.now(UTC).date().isoformat()
    fields = resolve_profile_fields(snapshot, request)
    profile_keys = [field.key for field in fields]
    wanted = set(request.session_ids) if request.session_ids else None
    sessions: list[GoldenSession] = []
    anomalies: list[DraftBuildAnomaly] = []
    turns_per_session: dict[str, int] = {}

    for session_outcome in _included_sessions(snapshot):
        if wanted is not None and session_outcome.session_id not in wanted:
            continue
        session, session_anomalies = build_session(
            session_outcome,
            snapshot_id=snapshot.snapshot_id,
            profile_keys=profile_keys,
            annotated_at=moment,
        )
        anomalies.extend(session_anomalies)
        if session is None:
            continue
        sessions.append(session)
        turns_per_session[session.session_id] = len(session.turns)

    draft_request = CreateGoldenDraftRequest(
        shared=_shared_from_snapshot(snapshot, request, fields=fields),
        sessions=sessions,
    )
    return draft_request, anomalies, turns_per_session


def resolve_profile_fields(
    snapshot: CorpusSnapshot, request: BuildDraftFromSnapshotRequest
) -> list[ProfileFieldDefinition]:
    """Profile fields come from the target surface pulled during collection.

    An explicit `profile_keys` list still wins: it is the manual override path for datasets whose
    snapshot predates the profile pull.
    """

    if request.profile_keys:
        return [ProfileFieldDefinition(key=key) for key in request.profile_keys]
    for workspace in snapshot.workspaces:
        if workspace.profile_contract is not None and workspace.profile_contract.fields:
            return list(workspace.profile_contract.fields)
    return []


def _turn_tool_calls(replies: Sequence[SourceMessage]) -> list[GoldenToolCall]:
    """6.2's golden side: the actions the online Agent was observed to execute in this Turn.

    These are diagnostic evidence, never a score: the publisher only checks that every name is in
    `context.available_tools`, and the judge reads them next to the actual AgentLogs.
    """

    calls: list[GoldenToolCall] = []
    seen: set[str] = set()
    for message in replies:
        for call in message.tool_calls:
            if call.name in seen:
                continue
            seen.add(call.name)
            calls.append(GoldenToolCall(name=call.name))
    return calls


def _pulled_profile_updates(
    replies: Sequence[SourceMessage], previous: dict[str, Any], *, allowed: set[str]
) -> dict[str, GoldenProfileUpdate]:
    """本轮平台日志直接拉取到的画像写入 → 本轮画像增量。

    只看本轮线上回复自己的日志（每条写日志属于产生它的那一轮），同一字段写多次以最后一条为准；
    值没有改变上一轮快照的写入不产生增量；不属于 target 画像 schema 的字段直接丢掉。
    """

    writes: dict[str, Any] = {}
    for message in replies:
        for key, value in message.profile_writes.items():
            if key in allowed:
                writes[key] = value
    updates: dict[str, GoldenProfileUpdate] = {}
    for key, value in writes.items():
        previous_value = previous.get(key)
        if previous_value == value:
            continue
        if _empty_profile_value(previous_value) and _empty_profile_value(value):
            continue
        updates[key] = GoldenProfileUpdate(
            value=value,
            updated_from=None if _empty_profile_value(previous_value) else previous_value,
            source=ProfileUpdateSource.PULLED,
        )
    return updates


def _empty_profile_value(value: Any) -> bool:
    return value is None or value == "" or value == []


OBSERVED_TOOL_NOTE = (
    "历史 AgentLog 中可观察到的执行动作。只判断是否调用，出现调用即按成功处理；不参与评分。"
)
DECLARED_TOOL_NOTE = "。调用条件以当前 Workspace skills/guidance 原文为准；未命中条件时不调用。"


def resolve_available_tools(snapshot: CorpusSnapshot) -> list[GoldenAvailableTool]:
    """Declared Workspace actions plus the actions actually observed in the collected history.

    The declared catalog is what the Workspace exposes; the observed set is what the online Agent
    really ran. Golden turns may only reference a name in this union, so a snapshot whose config
    read failed still produces a publishable dataset instead of an unvalidatable one.
    """

    tools: list[GoldenAvailableTool] = []
    seen: set[str] = set()
    for workspace in snapshot.workspaces:
        for tool in workspace.available_tools:
            if tool.name in seen:
                continue
            seen.add(tool.name)
            tools.append(
                GoldenAvailableTool(
                    name=tool.name,
                    description=tool.description + DECLARED_TOOL_NOTE,
                    params=[
                        GoldenToolParam(
                            name=str(param.get("name") or ""),
                            type=str(param.get("type") or ""),
                            required=bool(param.get("required")),
                            description=str(param.get("description") or ""),
                        )
                        for param in tool.params
                        if param.get("name")
                    ],
                )
            )
    for workspace in snapshot.workspaces:
        for session in workspace.sessions:
            if not session.included:
                continue
            for message in session.messages:
                for call in message.tool_calls:
                    if call.name in seen:
                        continue
                    seen.add(call.name)
                    tools.append(
                        GoldenAvailableTool(
                            name=call.name,
                            description=OBSERVED_TOOL_NOTE,
                            params=[],
                        )
                    )
    return tools


def _included_sessions(snapshot: CorpusSnapshot) -> list[SessionOutcome]:
    return [
        session
        for workspace in snapshot.workspaces
        for session in workspace.sessions
        if session.included
    ]


def _shared_from_snapshot(
    snapshot: CorpusSnapshot,
    request: BuildDraftFromSnapshotRequest,
    *,
    fields: list[ProfileFieldDefinition],
) -> GoldenShared:
    tools = resolve_available_tools(snapshot)
    return GoldenShared(
        dataset=GoldenDatasetHeader(
            name=request.name or f"金标草案 {snapshot.snapshot_id}",
            schema_version="golden-dialogue-v0.4",
            domain=request.domain or "待配置业务领域",
            source_config={
                "snapshot_id": snapshot.snapshot_id,
                "source_hash": snapshot.source_hash,
            },
            files={
                "shared": "golden-dialogue-shared.json",
                "cases": "golden-dialogue-cases.jsonl",
            },
        ),
        context=GoldenSharedContext(
            note=(
                f"由采集快照 {snapshot.snapshot_id} 生成；"
                "画像字段来自采集时读取的 target 配置，工具清单来自采集时的 Workspace 配置与"
                "历史 AgentLog，rubric 待人工抽检。"
            ),
            profile_schema=[
                GoldenProfileField(
                    key=field.key,
                    label=field.label,
                    type=field.type or "string",
                    # 描述留空就留空：占位符会被模型当成字段说明，比空值更有害。
                    description=field.description,
                    value_format=field.value_format,
                    examples=list(field.examples),
                    updatable=field.updatable,
                    ttl_days=field.ttl_days,
                    source=field.source,
                )
                for field in fields
            ],
            profile_schema_note=_profile_schema_note(
                snapshot, fields, manual=bool(request.profile_keys)
            ),
            available_tools=tools,
            available_tools_note=(
                "来自采集时读取的 Workspace 工具配置，并合并历史 AgentLog 中可观察到的执行动作；"
                "历史 task/知识检索等动作的参数留空。按业务确认，工具只判断是否调用，"
                "出现调用即视为成功，且不参与评分（6.2 诊断）。"
            ),
            knowledge_base_version="",
            guidance=_snapshot_guidance(snapshot),
            skill_rules=next((workspace.profile_contract.skill_rules
                              for workspace in snapshot.workspaces
                              if workspace.profile_contract), []),
        ),
        customer_memory_spec=CustomerMemorySpec(
            comment="待按 target Workspace 配置补全。",
            version="unconfigured",
            definition="待补全：客户记忆的保留范围与更新时机。",
        ),
    )


def _snapshot_guidance(snapshot: CorpusSnapshot) -> str | None:
    """采集时读到的测试空间系统指导原文（序列化），评估时进入 Judge 上下文。"""

    for workspace in snapshot.workspaces:
        contract = workspace.profile_contract
        if contract is not None and contract.guidance:
            return contract.guidance
    return None


def _profile_schema_note(
    snapshot: CorpusSnapshot,
    fields: list[ProfileFieldDefinition],
    *,
    manual: bool,
) -> str:
    """Say where the fields came from, and why nothing was usable when that is the case."""

    if manual:
        return "字段定义来自生成本草案时手填的 profile_keys，不是 target Workspace 读取结果。"
    if fields:
        return "字段定义来自采集时读取的 target Workspace 当前配置（已发布技能写入的字段）。"
    contract = next(
        (
            workspace.profile_contract
            for workspace in snapshot.workspaces
            if workspace.profile_contract is not None
        ),
        None,
    )
    if contract is None:
        return "采集快照没有 target Workspace 画像合同，请在生成草案时手填 profile_keys。"
    detail = "；".join(contract.warnings) or "合同中没有可用字段"
    return f"采集时未得到可用画像字段：{detail}"


def _golden_message(message: SourceMessage, seq: int) -> GoldenMessage:
    """Golden layer of a source message: original text, or a media label, plus attachments.

    The snapshot keeps the evidence form (text + attachment urls); the golden case keeps the
    reference implementation's form: text when present, otherwise `图片附件：<name>`.
    """

    return GoldenMessage(
        seq=seq,
        type=message.content_type,
        content=golden_content_from_source(message),
        attachments=_golden_attachments(message) or None,
    )


def _golden_attachments(message: SourceMessage) -> list[GoldenAttachment]:
    return [
        GoldenAttachment(
            name=str(item.get("name") or "attachment"),
            content_type=str(
                item.get("content_type")
                or item.get("contentType")
                or "application/octet-stream"
            ),
            url=str(item.get("url") or ""),
            size=item.get("size") if isinstance(item.get("size"), int) else None,
            stored_url=item.get("stored_url"),
            sha256=item.get("sha256"),
            stored=item.get("stored"),
        )
        for item in message.attachments
        if isinstance(item, dict)
    ]


def _source_history(
    outcome: SessionOutcome,
) -> tuple[list[ConversationMessage], dict[int, int]]:
    """完整前序对话：visitor→user，human/ai→assistant；返回历史列表与 source seq → 历史下标映射。"""

    history: list[ConversationMessage] = []
    index_by_seq: dict[int, int] = {}
    for message in outcome.messages:
        if message.role is SenderRole.VISITOR:
            role = "user"
        elif message.role in {SenderRole.HUMAN, SenderRole.AI}:
            role = "assistant"
        else:
            continue
        index_by_seq[message.seq] = len(history)
        history.append(
            ConversationMessage(
                role=role,
                content=golden_content_from_source(message),
                attachments=_golden_attachments(message),
            )
        )
    return history, index_by_seq


def _anomaly(outcome: SessionOutcome, code: str, detail: str) -> DraftBuildAnomaly:
    return DraftBuildAnomaly(session_id=outcome.session_id, code=code, detail=detail)
