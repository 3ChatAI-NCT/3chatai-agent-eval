"""Collection domain: source candidates, the filtering funnel and immutable snapshots.

The funnel wording is fixed: every exclusion has a stable reason code so the dataset can explain why
a session did not enter it. Golden files never live here; a snapshot only holds source evidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SenderRole(IntEnum):
    """Platform sender types. Values are part of the source contract, not an internal choice."""

    UNKNOWN = -1
    SYSTEM = 0
    VISITOR = 1
    HUMAN = 2
    AI = 3
    THIRD_PARTY = 4


class ExclusionReason(StrEnum):
    VISITOR_MISSING = "visitor_missing"
    BOT_USER_MISMATCH = "bot_user_mismatch"
    CONTACT_FILTER_MISMATCH = "contact_filter_mismatch"
    GROUP_CHAT = "group_chat"
    SANDBOX = "sandbox"
    INCOMPLETE_HISTORY = "incomplete_history"
    EMPTY_HISTORY = "empty_history"
    NO_VISITOR_MESSAGE = "no_visitor_message"
    MISSING_FIRST_CUSTOMER_MESSAGE_TIME = "missing_first_customer_message_time"
    TOO_FEW_TURNS = "too_few_turns"
    OUTSIDE_QUOTA = "outside_latest_trigger_quota"


SESSION_STAGE_REASONS = frozenset(
    {
        ExclusionReason.VISITOR_MISSING,
        ExclusionReason.BOT_USER_MISMATCH,
        ExclusionReason.CONTACT_FILTER_MISMATCH,
        ExclusionReason.GROUP_CHAT,
        ExclusionReason.SANDBOX,
    }
)

HISTORY_STAGE_REASONS = frozenset(
    {
        ExclusionReason.INCOMPLETE_HISTORY,
        ExclusionReason.EMPTY_HISTORY,
        ExclusionReason.NO_VISITOR_MESSAGE,
        ExclusionReason.MISSING_FIRST_CUSTOMER_MESSAGE_TIME,
        ExclusionReason.TOO_FEW_TURNS,
    }
)


class ObservedToolCall(BaseModel):
    """One observable action the Agent took, normalized from a platform AgentLog entry."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    log_type: str = ""
    status: str | None = None


class SourceMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    role: SenderRole
    content: str
    # 原始纯文本（不含附件 URL）；参考数据内容用它，空则回落到媒体标签。
    text: str = ""
    content_type: str = "text"
    sent_at: datetime
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    # 可观察执行动作（AgentLog 归一）。它是证据，不是金标结论。
    tool_calls: list[ObservedToolCall] = Field(default_factory=list)
    # 平台直接报告的联系人画像写入（`internalTool: visitor`），字段 → 新值。它是平台事实，
    # 金标构建时直接当成本轮画像增量，画像补全模型不得覆盖这些字段。
    profile_writes: dict[str, Any] = Field(default_factory=dict)


class ToolDefinition(BaseModel):
    """A tool/action declared by the Workspace configuration, as read from the source surface."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    params: list[dict[str, Any]] = Field(default_factory=list)


class FilterOperator(StrEnum):
    """平台条件查询支持的运算符（`ConditionOperator` 的取值）。"""

    EQ = "EQ"
    NE = "NE"
    LIKE = "LIKE"
    NOTLIKE = "NOTLIKE"
    IN = "IN"
    NIN = "NIN"
    ISNULL = "ISNULL"
    NOTNULL = "NOTNULL"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"


NO_VALUE_OPERATORS = frozenset({FilterOperator.ISNULL, FilterOperator.NOTNULL})


class FilterScope(StrEnum):
    """字段来源：联系人字段会自动拼成 `visitorId.<field>`，会话字段按原样查询。"""

    CONTACT = "contact"
    SESSION = "session"


class ContactFilter(BaseModel):
    """一条采集过滤条件：字段 + 逻辑符 + 值。

    不同空间的联系人字段不一样，所以字段名由调用方填写：`contact` 作用域拼成 `visitorId.<field>`，
    `session` 作用域（或写成点分路径，如 `channelId.type`）按原样传递。
    """

    model_config = ConfigDict(extra="forbid")

    scope: FilterScope = FilterScope.CONTACT
    field: str = Field(min_length=1, max_length=120)
    operator: FilterOperator = FilterOperator.EQ
    value: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_value(self) -> ContactFilter:
        self.field = self.field.strip()
        if not self.field:
            raise ValueError("filter field must not be blank")
        if self.operator not in NO_VALUE_OPERATORS and not self.value.strip():
            raise ValueError(f"{self.operator.value} requires a value")
        return self


class SessionCandidate(BaseModel):
    """One session index row plus the routing fields the session-stage funnel needs."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    updated_at: datetime
    session_type: str = ""
    subject_type: str | None = None
    chat_type: str | None = None
    channel_type: str | None = None
    channel_source_type: str | None = None
    visitor_found: bool = True
    bot_user_id: str | None = None
    im_bot_id: str | None = None


class SessionHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    complete: bool
    expected_message_total: int | None = Field(default=None, ge=0)
    messages: list[SourceMessage] = Field(default_factory=list)


class WorkspaceConfiguration(BaseModel):
    """Configuration evidence only: no raw payload, no secrets, just an auditable hash."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    captured_at: datetime


class ProfileFieldDefinition(BaseModel):
    """A target Workspace profile field, as read from the target configuration."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    label: str = ""
    type: str = "string"
    description: str = ""
    value_format: str = ""
    examples: list[Any] = Field(default_factory=list)
    updatable: bool = True
    ttl_days: int | None = None
    source: Literal["visitor_field", "custom_field"] = "custom_field"


class ProfileContract(BaseModel):
    """Executable profile surface for a run: which fields the target actually exposes.

    Mirrors the verified contract: golden fields ∩ target-defined fields (including built-in visitor
    fields) ∩ fields the published system skills actually write.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    target_config_hash: str = Field(min_length=1)
    fields: list[ProfileFieldDefinition] = Field(default_factory=list)
    excluded_fields: dict[str, list[str]] = Field(default_factory=dict)
    guidance_available: bool = False
    guidance: str | None = None
    skill_rules: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    captured_at: datetime


class CollectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_ids: list[str] | None = None
    start: datetime | None = None
    end: datetime | None = None
    quota_per_workspace: int = Field(default=20, ge=1, le=100)
    min_turns: int = Field(default=3, ge=1, le=50)
    # 采集过滤条件：字段由调用方按空间自行填写（不同空间联系人字段不同），不再写死 botUserId。
    contact_filters: list[ContactFilter] = Field(default_factory=list)
    selection_method: Literal["latest_first_customer_message"] = (
        "latest_first_customer_message"
    )
    # Scanning is driven by the final quota: keep paging until enough *eligible* sessions are found.
    # This cap only stops a run from walking the whole workspace forever.
    max_scanned_sessions: int = Field(default=10000, ge=1, le=20000)
    progress_every: int = Field(default=25, ge=1, le=1000)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_keys(cls, data: Any) -> Any:
        """兼容旧载荷：`scan_buffer` 已废弃，`bot_user_id` 变成一条 botUserId 过滤条件。"""

        if not isinstance(data, dict):
            return data
        cleaned = {key: value for key, value in data.items() if key != "scan_buffer"}
        legacy_bot = cleaned.pop("bot_user_id", None)
        if legacy_bot:
            filters = list(cleaned.get("contact_filters") or [])
            filters.append({"field": "botUserId", "operator": "EQ", "value": str(legacy_bot)})
            cleaned["contact_filters"] = filters
        return cleaned

    @model_validator(mode="after")
    def validate_window(self) -> CollectionPolicy:
        if self.start and self.end and self.start >= self.end:
            raise ValueError("collection window start must be earlier than end")
        if self.workspace_ids is not None:
            normalized = [item for item in self.workspace_ids if item.strip()]
            if not normalized:
                raise ValueError("workspace_ids must contain at least one non-empty id")
            object.__setattr__(self, "workspace_ids", normalized)
        return self


class SessionOutcome(BaseModel):
    """Per-session funnel result. Messages are kept only for included sessions."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    included: bool
    reasons: list[ExclusionReason] = Field(default_factory=list)
    first_customer_message_at: datetime | None = None
    publishable_turn_count: int = Field(default=0, ge=0)
    human_reply_turn_count: int = Field(default=0, ge=0)
    removed_third_party_message_count: int = Field(default=0, ge=0)
    bot_user_id: str | None = None
    im_bot_id: str | None = None
    messages: list[SourceMessage] = Field(default_factory=list)


class FunnelCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scanned: int = Field(default=0, ge=0)
    included: int = Field(default=0, ge=0)
    excluded: dict[str, int] = Field(default_factory=dict)
    removed_third_party_messages: int = Field(default=0, ge=0)
    removed_human_reply_turns: int = Field(default=0, ge=0)
    quota_shortfall: int = Field(default=0, ge=0)

    def count_exclusion(self, reason: ExclusionReason, amount: int = 1) -> None:
        self.excluded[reason.value] = self.excluded.get(reason.value, 0) + amount

    @classmethod
    def merged(cls, items: list[FunnelCounts]) -> FunnelCounts:
        merged = cls()
        for item in items:
            merged.scanned += item.scanned
            merged.included += item.included
            merged.removed_third_party_messages += item.removed_third_party_messages
            merged.removed_human_reply_turns += item.removed_human_reply_turns
            merged.quota_shortfall += item.quota_shortfall
            for reason, count in item.excluded.items():
                merged.excluded[reason] = merged.excluded.get(reason, 0) + count
        return merged


class WorkspaceOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    configuration: WorkspaceConfiguration
    profile_contract: ProfileContract | None = None
    available_tools: list[ToolDefinition] = Field(default_factory=list)
    target_count: int = Field(ge=1)
    selected_count: int = Field(ge=0)
    counts: FunnelCounts
    sessions: list[SessionOutcome] = Field(default_factory=list)


class CorpusSnapshot(BaseModel):
    """Immutable collection result. Later stages may only read it."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    # 标签，来自采集任务名；改名只换标签，不重算 source_hash，也不改任何证据。
    name: str = Field(default="", max_length=120)
    created_at: datetime
    policy: CollectionPolicy
    source_hash: str = Field(min_length=1)
    session_count: int = Field(ge=0)
    counts: FunnelCounts
    workspaces: list[WorkspaceOutcome] = Field(default_factory=list)


class SnapshotSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    name: str = ""
    created_at: datetime
    session_count: int = Field(ge=0)
    source_hash: str
    policy: CollectionPolicy
    counts: FunnelCounts
    workspace_ids: list[str] = Field(default_factory=list)
    profile_field_keys: list[str] = Field(default_factory=list)
    profile_contract_warnings: list[str] = Field(default_factory=list)
