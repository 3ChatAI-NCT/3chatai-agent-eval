"""Collection funnel: turn source candidates into an auditable CorpusSnapshot.

Exclusions are decided by code, never by a model, and carry a stable reason code.
Turn grouping follows the verified source semantics: one customer input plus the
replies that follow it form one Turn, and a Turn whose replies include a human
agent is not gold-eligible even though its messages stay in the history.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.corpus import (
    CollectionPolicy,
    ContactFilter,
    CorpusSnapshot,
    ExclusionReason,
    FilterOperator,
    FunnelCounts,
    SenderRole,
    SessionCandidate,
    SessionHistory,
    SessionOutcome,
    SourceMessage,
    ToolDefinition,
    WorkspaceOutcome,
)
from ..ports import CorpusSourcePort


def session_stage_reasons(
    candidate: SessionCandidate, policy: CollectionPolicy
) -> list[ExclusionReason]:
    """Session-level funnel: cheap decisions that need no message history."""

    reasons: list[ExclusionReason] = []
    if not candidate.visitor_found:
        # The route is unknown for a missing visitor, so no further session-stage check applies.
        return [ExclusionReason.VISITOR_MISSING]
    for contact_filter in policy.contact_filters:
        matched = _matches_candidate(candidate, contact_filter)
        if matched is None or matched:
            # 本地拿不到这个字段（例如自定义画像字段）时交给平台条件查询，不在漏斗里猜测。
            continue
        reasons.append(
            ExclusionReason.BOT_USER_MISMATCH
            if contact_filter.field == "botUserId"
            else ExclusionReason.CONTACT_FILTER_MISMATCH
        )
    if (
        candidate.session_type == "group"
        or (candidate.subject_type or "").upper() == "GROUP"
        or candidate.chat_type == "group"
    ):
        reasons.append(ExclusionReason.GROUP_CHAT)
    if candidate.channel_type == "sandbox" or candidate.channel_source_type == "sandbox":
        reasons.append(ExclusionReason.SANDBOX)
    return reasons


# 会话索引行里真正能拿到的联系人/会话字段；其余字段只能由平台条件查询过滤。
CANDIDATE_FIELDS: dict[str, Callable[[SessionCandidate], str | None]] = {
    "botUserId": lambda item: item.bot_user_id,
    "imBotId": lambda item: item.im_bot_id,
    "sessionType": lambda item: item.session_type,
    "subjectType": lambda item: item.subject_type,
    "chatType": lambda item: item.chat_type,
    "channelType": lambda item: item.channel_type,
    "channelSourceType": lambda item: item.channel_source_type,
    "visitorFound": lambda item: "true" if item.visitor_found else "false",
}


def _matches_candidate(
    candidate: SessionCandidate, contact_filter: ContactFilter
) -> bool | None:
    """按同一条过滤条件判断候选会话；字段本地不可知时返回 None（交给平台查询）。

    注意：会话索引行只回传部分字段（例如 `subjectType` 只在查询条件里可用，不在响应里），
    取不到值时必须当作"本地无从判定"，否则会把平台已经筛过的会话整批误判为不符合。
    """

    reader = CANDIDATE_FIELDS.get(_leaf_field(contact_filter.field))
    if reader is None:
        return None
    actual = reader(candidate)
    if actual is None or str(actual).strip() == "":
        return None
    return _matches_operator(contact_filter.operator, str(actual), contact_filter.value)


def _leaf_field(field: str) -> str:
    return field.rsplit(".", 1)[-1]


def _matches_operator(operator: FilterOperator, actual: str, expected: str) -> bool:
    """与平台条件查询一致的语义；只用于本地漏斗复核。"""

    text = actual
    wanted = expected.strip()
    if operator is FilterOperator.ISNULL:
        return text == ""
    if operator is FilterOperator.NOTNULL:
        return text != ""
    if operator is FilterOperator.EQ:
        return text == wanted
    if operator is FilterOperator.NE:
        return text != wanted
    if operator is FilterOperator.LIKE:
        return wanted in text
    if operator is FilterOperator.NOTLIKE:
        return wanted not in text
    values = [item.strip() for item in wanted.split(",") if item.strip()]
    if operator is FilterOperator.IN:
        return text in values
    if operator is FilterOperator.NIN:
        return text not in values
    try:
        left, right = float(text), float(wanted)
    except ValueError:
        return True  # 非数值字段上不猜测大小关系，交给平台判定
    return {
        FilterOperator.GT: left > right,
        FilterOperator.GTE: left >= right,
        FilterOperator.LT: left < right,
        FilterOperator.LTE: left <= right,
    }[operator]


def partition_third_party(
    messages: Iterable[SourceMessage],
) -> tuple[list[SourceMessage], int]:
    kept: list[SourceMessage] = []
    removed = 0
    for message in messages:
        if message.role is SenderRole.THIRD_PARTY:
            removed += 1
            continue
        kept.append(message)
    return kept, removed


def first_customer_message_at(messages: Iterable[SourceMessage]) -> datetime | None:
    moments = [
        message.sent_at for message in messages if message.role is SenderRole.VISITOR
    ]
    return min(moments) if moments else None


@dataclass(frozen=True, slots=True)
class SourceTurn:
    """One customer input plus the replies that follow it."""

    seq: int
    customer: tuple[SourceMessage, ...] = ()
    replies: tuple[SourceMessage, ...] = ()

    @property
    def human_reply(self) -> bool:
        return any(message.role is SenderRole.HUMAN for message in self.replies)

    @property
    def answered(self) -> bool:
        """是否至少有一条回复；只有客户消息、Agent 没回的 Turn 不能当金标。"""

        return any(message.role is SenderRole.AI for message in self.replies)

    @property
    def publishable(self) -> bool:
        return bool(self.customer) and self.answered and not self.human_reply


@dataclass(frozen=True, slots=True)
class TurnGrouping:
    turns: tuple[SourceTurn, ...]
    unsolicited_replies: tuple[SourceMessage, ...]
    unsupported_roles: tuple[SenderRole, ...]


def group_source_turns(messages: Iterable[SourceMessage]) -> TurnGrouping:
    """Split a source history into turns, reporting anomalies instead of hiding them.

    Replies that arrive before any customer message are `unsolicited_replies`; roles outside
    customer/human/AI break the current turn.
    """

    turns: list[SourceTurn] = []
    unsolicited: list[SourceMessage] = []
    unsupported: list[SenderRole] = []
    customers: list[SourceMessage] = []
    replies: list[SourceMessage] = []
    seq = 0

    def flush() -> None:
        nonlocal customers, replies, seq
        if not customers and not replies:
            return
        if not customers:
            unsolicited.extend(replies)
            customers, replies = [], []
            return
        seq += 1
        turns.append(
            SourceTurn(seq=seq, customer=tuple(customers), replies=tuple(replies))
        )
        customers, replies = [], []

    for message in messages:
        if message.role is SenderRole.VISITOR:
            if replies:
                flush()
            customers.append(message)
        elif message.role in {SenderRole.HUMAN, SenderRole.AI}:
            replies.append(message)
        else:
            flush()
            unsupported.append(message.role)
    flush()
    return TurnGrouping(
        turns=tuple(turns),
        unsolicited_replies=tuple(unsolicited),
        unsupported_roles=tuple(dict.fromkeys(unsupported)),
    )


def turn_actor_groups(
    messages: Iterable[SourceMessage],
) -> list[tuple[bool, set[SenderRole]]]:
    """Compatibility view over `group_source_turns` for the funnel counters."""

    return [
        (True, {message.role for message in turn.replies})
        for turn in group_source_turns(messages).turns
    ]


def count_publishable_turns(messages: Iterable[SourceMessage]) -> int:
    return sum(
        SenderRole.HUMAN not in actors and SenderRole.AI in actors
        for _, actors in turn_actor_groups(messages)
    )


def count_human_reply_turns(messages: Iterable[SourceMessage]) -> int:
    return sum(SenderRole.HUMAN in actors for _, actors in turn_actor_groups(messages))


def history_stage_reasons(
    history: SessionHistory, policy: CollectionPolicy, kept: list[SourceMessage]
) -> list[ExclusionReason]:
    reasons: list[ExclusionReason] = []
    if not history.complete:
        reasons.append(ExclusionReason.INCOMPLETE_HISTORY)
    if not kept:
        reasons.append(ExclusionReason.EMPTY_HISTORY)
        return reasons
    if not any(message.role is SenderRole.VISITOR for message in kept):
        reasons.append(ExclusionReason.NO_VISITOR_MESSAGE)
    if first_customer_message_at(kept) is None:
        reasons.append(ExclusionReason.MISSING_FIRST_CUSTOMER_MESSAGE_TIME)
    if count_publishable_turns(kept) < policy.min_turns:
        reasons.append(ExclusionReason.TOO_FEW_TURNS)
    return reasons


def select_by_trigger(
    outcomes: list[SessionOutcome], quota: int
) -> tuple[list[SessionOutcome], list[SessionOutcome]]:
    """Newest first-customer-message first; the rest are reported as outside quota."""

    ordered = sorted(
        outcomes,
        key=lambda item: (
            -(item.first_customer_message_at or datetime.min.replace(tzinfo=UTC)).timestamp(),
            item.session_id,
        ),
    )
    return ordered[:quota], ordered[quota:]


class CorpusCollector:
    """Run the funnel for every workspace and freeze the result into one snapshot."""

    def __init__(
        self, source: CorpusSourcePort, *, on_progress: Callable[[dict], None] | None = None
    ) -> None:
        self._source = source
        self._on_progress = on_progress

    def collect(
        self, snapshot_id: str, policy: CollectionPolicy, *, now: datetime | None = None
    ) -> CorpusSnapshot:
        moment = now or datetime.now(UTC)
        workspace_ids = list(policy.workspace_ids or self._source.list_workspace_ids())
        workspaces = [
            self._collect_workspace(workspace_id, policy) for workspace_id in workspace_ids
        ]
        counts = FunnelCounts.merged([item.counts for item in workspaces])
        snapshot = CorpusSnapshot(
            snapshot_id=snapshot_id,
            created_at=moment,
            policy=policy,
            source_hash=_source_hash(policy, workspaces, workspace_ids),
            session_count=counts.included,
            counts=counts,
            workspaces=workspaces,
        )
        return snapshot

    def _collect_workspace(
        self, workspace_id: str, policy: CollectionPolicy
    ) -> WorkspaceOutcome:
        counts = FunnelCounts()
        eligible: list[SessionOutcome] = []
        excluded: list[SessionOutcome] = []

        # Scanning is driven by the final quota: keep pulling candidates until enough *eligible*
        # sessions exist, or the scan cap is hit. Stopping at a fixed candidate count would silently
        # return short datasets whenever the newest sessions are filtered out.
        quota = policy.quota_per_workspace
        for candidate in self._source.iter_candidates(workspace_id, policy):
            counts.scanned += 1
            session_reasons = session_stage_reasons(candidate, policy)
            if session_reasons:
                for reason in session_reasons:
                    counts.count_exclusion(reason)
                excluded.append(
                    SessionOutcome(
                        workspace_id=workspace_id,
                        session_id=candidate.session_id,
                        included=False,
                        reasons=session_reasons,
                        bot_user_id=candidate.bot_user_id,
                        im_bot_id=candidate.im_bot_id,
                    )
                )
                self._report_progress(workspace_id, counts, eligible, quota, policy)
                if self._scan_complete(counts, eligible, quota, policy):
                    break
                continue

            history = self._source.hydrate(candidate)
            kept, removed_third_party = partition_third_party(history.messages)
            history_reasons = history_stage_reasons(history, policy, kept)
            if history_reasons:
                for reason in history_reasons:
                    counts.count_exclusion(reason)
                counts.removed_third_party_messages += removed_third_party
                excluded.append(
                    SessionOutcome(
                        workspace_id=workspace_id,
                        session_id=candidate.session_id,
                        included=False,
                        reasons=history_reasons,
                        first_customer_message_at=first_customer_message_at(kept),
                        publishable_turn_count=count_publishable_turns(kept),
                        human_reply_turn_count=count_human_reply_turns(kept),
                        removed_third_party_message_count=removed_third_party,
                        bot_user_id=candidate.bot_user_id,
                        im_bot_id=candidate.im_bot_id,
                    )
                )
                self._report_progress(workspace_id, counts, eligible, quota, policy)
                if self._scan_complete(counts, eligible, quota, policy):
                    break
                continue

            counts.removed_third_party_messages += removed_third_party
            counts.removed_human_reply_turns += count_human_reply_turns(kept)
            eligible.append(
                SessionOutcome(
                    workspace_id=workspace_id,
                    session_id=candidate.session_id,
                    included=False,
                    first_customer_message_at=first_customer_message_at(kept),
                    publishable_turn_count=count_publishable_turns(kept),
                    human_reply_turn_count=count_human_reply_turns(kept),
                    removed_third_party_message_count=removed_third_party,
                    bot_user_id=candidate.bot_user_id,
                    im_bot_id=candidate.im_bot_id,
                    messages=kept,
                )
            )
            self._report_progress(workspace_id, counts, eligible, quota, policy)
            if self._scan_complete(counts, eligible, quota, policy):
                break

        selected, overflow = select_by_trigger(eligible, policy.quota_per_workspace)
        selected = [item.model_copy(update={"included": True}) for item in selected]
        overflow = [
            item.model_copy(
                update={
                    "included": False,
                    "reasons": [ExclusionReason.OUTSIDE_QUOTA],
                    "messages": [],
                }
            )
            for item in overflow
        ]
        for _ in overflow:
            counts.count_exclusion(ExclusionReason.OUTSIDE_QUOTA)
        counts.included = len(selected)
        counts.quota_shortfall = max(policy.quota_per_workspace - len(selected), 0)

        return WorkspaceOutcome(
            workspace_id=workspace_id,
            configuration=self._source.current_configuration(workspace_id),
            available_tools=_declared_tools(self._source, workspace_id),
            target_count=policy.quota_per_workspace,
            selected_count=len(selected),
            counts=counts,
            sessions=[*selected, *overflow, *excluded],
        )

    @staticmethod
    def _scan_complete(
        counts: FunnelCounts,
        eligible: list[SessionOutcome],
        quota: int,
        policy: CollectionPolicy,
    ) -> bool:
        if len(eligible) >= quota:
            return True
        return counts.scanned >= policy.max_scanned_sessions

    def _report_progress(
        self,
        workspace_id: str,
        counts: FunnelCounts,
        eligible: list[SessionOutcome],
        quota: int,
        policy: CollectionPolicy,
    ) -> None:
        if self._on_progress is None:
            return
        if counts.scanned % policy.progress_every != 0 and len(eligible) < quota:
            return
        self._on_progress(
            {
                "workspace_id": workspace_id,
                "scanned": counts.scanned,
                "eligible": len(eligible),
                "quota": quota,
                "excluded": dict(counts.excluded),
                "max_scanned_sessions": policy.max_scanned_sessions,
                # 采集区间按 start/end（会话创建时间），这里显示当前扫到哪个时间分片。
                "scan_window": _scan_window(self._source),
            }
        )


def _declared_tools(source: CorpusSourcePort, workspace_id: str) -> list[ToolDefinition]:
    """Read the Workspace tool catalog when the adapter supports it; never fail the collection."""

    reader = getattr(source, "available_tools", None)
    if reader is None:
        return []
    try:
        return list(reader(workspace_id))
    except Exception:  # a catalog read is evidence, not a precondition for collecting
        return []



def _scan_window(source: CorpusSourcePort) -> dict[str, str] | None:
    """当前扫描的时间分片（平台适配器会边扫边更新）；Fixture 之类的源没有这个概念。"""

    position = getattr(source, "scan_position", None)
    if not position:
        return None
    lower, upper = position
    return {
        "start": datetime.fromtimestamp(lower / 1000, UTC).isoformat(),
        "end": datetime.fromtimestamp(upper / 1000, UTC).isoformat(),
    }

def _source_hash(
    policy: CollectionPolicy, workspaces: list[WorkspaceOutcome], workspace_ids: list[str]
) -> str:
    """Tool catalogs are configuration evidence, so they belong in the snapshot hash."""

    material = {
        "policy": policy.model_dump(mode="json"),
        "workspace_ids": workspace_ids,
        "workspaces": [
            {
                "workspace_id": item.workspace_id,
                "config_hash": item.configuration.config_hash,
                "available_tools": [
                    tool.model_dump(mode="json") for tool in item.available_tools
                ],
                "counts": item.counts.model_dump(mode="json"),
                "included": [
                    {
                        "session_id": session.session_id,
                        "first_customer_message_at": (
                            session.first_customer_message_at.isoformat()
                            if session.first_customer_message_at
                            else None
                        ),
                        "publishable_turn_count": session.publishable_turn_count,
                        "messages": [
                            message.model_dump(mode="json") for message in session.messages
                        ],
                    }
                    for session in item.sessions
                    if session.included
                ],
            }
            for item in workspaces
        ],
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
