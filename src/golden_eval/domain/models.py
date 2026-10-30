from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .corpus import CollectionPolicy


class ConnectionPurpose(StrEnum):
    SOURCE_READONLY = "source_readonly"
    TARGET_TEST = "target_test"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class ConnectionAuthMode(StrEnum):
    """How the platform authenticates us.

    `platform_identity` is the production path: the platform resolves the user's session into
    tenant/staff identity and forwards those headers, so no user credential ever reaches us.
    `bearer_token` stays available for local and standalone testing.
    """

    BEARER_TOKEN = "bearer_token"
    PLATFORM_IDENTITY = "platform_identity"


class ConnectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    name: str
    workspace_id_hash: str
    purpose: ConnectionPurpose
    status: Literal["configured", "unconfigured"]
    credential_source: Literal["secret_reference", "fixture", "frontend_inline"]
    auth_mode: ConnectionAuthMode = ConnectionAuthMode.BEARER_TOKEN
    base_url: str | None = None
    # 填写的 Workspace / Tenant（留空时按 token 声明补上，便于"只粘 token"）。
    workspace_id: str | None = None
    # 归档 = 从选择列表里隐藏，但历史 Run 仍能解释；默认连接在新建任务时预选。
    archived: bool = False
    is_default: bool = False
    # 被多少个 Run 引用（由 API 层统计，用于删除保护）。
    used_by_runs: int = 0
    workspace_label: str | None = None
    staff_id: str | None = None
    # 空间对（强绑定）：source 与它的 target 共享同一个 pair_id；列表按它分组就是「项目」。
    pair_id: str | None = None
    has_token: bool = False
    token_hint: str | None = None
    token_fingerprint: str | None = None
    last_verified_at: datetime | None = None


class ConnectionConfig(ConnectionSummary):
    token_ref: str | None = None


class ConnectionVerifyResult(BaseModel):
    """Verify never returns or echoes the credential, only a probe verdict."""

    model_config = ConfigDict(extra="forbid")

    connection: ConnectionSummary
    probe_implemented: bool
    message: str


class ConnectionUpdateRequest(BaseModel):
    """Frontend-entered connection settings.

    `token` is write-only: it is stored server-side and never returned by any endpoint. Omitting it
    keeps the stored token, so a form can be re-saved without re-typing the secret.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    # 空间对：同一个 source 与它的 target 共享一个 pair_id（强绑定）。留空时按名称推导。
    pair_id: str | None = Field(default=None, max_length=80)
    purpose: ConnectionPurpose
    auth_mode: ConnectionAuthMode = ConnectionAuthMode.BEARER_TOKEN
    base_url: str | None = Field(default=None, max_length=300)
    workspace_id: str | None = Field(default=None, max_length=200)
    workspace_label: str | None = Field(default=None, max_length=120)
    staff_id: str | None = Field(default=None, max_length=80)
    token: str | None = Field(default=None, max_length=4096)
    clear_token: bool = False

    @model_validator(mode="after")
    def validate_base_url(self) -> ConnectionUpdateRequest:
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        if self.token is not None and not self.token.strip():
            raise ValueError("token must not be blank when provided")
        if self.clear_token and self.token is not None:
            raise ValueError("provide either token or clear_token, not both")
        if self.auth_mode is ConnectionAuthMode.PLATFORM_IDENTITY and not (
            self.workspace_id or ""
        ).strip():
            raise ValueError("platform_identity requires workspace_id (the tenant id)")
        return self


class ConnectionCreateRequest(ConnectionUpdateRequest):
    """新建连接：其余字段与编辑一致，另外可以指定 connection_id（缺省自动生成）。"""

    connection_id: str | None = Field(default=None, min_length=1, max_length=64)

    def to_update_request(self) -> ConnectionUpdateRequest:
        return ConnectionUpdateRequest.model_validate(
            self.model_dump(exclude={"connection_id"})
        )


class RunIdentity(BaseModel):
    """Identity captured when a Run is created, replayed for background work.

    The platform proves who the user is; we only carry the resolved tenant/staff forward. No session
    cookie, no user token.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    staff_id: str | None = Field(default=None, max_length=80)
    request_id: str | None = Field(default=None, max_length=120)
    source: Literal["platform_header", "connection_settings"]


class JudgeProviderKind(StrEnum):
    UNSET = "unset"
    OPENAI_COMPATIBLE = "openai_compatible"
    INTERNAL_GATEWAY = "internal_gateway"


class ExecutionSettings(BaseModel):
    """执行引擎的并发设置。

    `worker_concurrency`（1..10）就是当前生效值：worker 每轮能同时执行的任务数上限，
    也是单个 Run 并发的天花板（Run 自己的 `max_concurrency` 只在只有一个活跃 Run 时生效）。
    没保存过时取自 `GOLDEN_EVAL_WORKER_CONCURRENCY`（部署默认值），保存过就按设置页里的值来。
    """

    model_config = ConfigDict(extra="forbid")

    worker_concurrency: int = Field(default=4, ge=1, le=10)
    updated_at: datetime | None = None


class ExecutionSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_concurrency: int = Field(ge=1, le=10)


class JudgeSettings(BaseModel):
    """Judge provider configuration shell.

    配置分两层：`.env` 是部署默认值（模板见 `.env.example`），设置页可以逐项覆盖。响应里的字段都是
    **生效值**，`overridden_fields` 说明哪几项来自设置页、哪几项还是 `.env` 的值。
    """

    model_config = ConfigDict(extra="forbid")

    provider: JudgeProviderKind = JudgeProviderKind.UNSET
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    json_mode: bool = True
    max_retries: int = Field(default=2, ge=0, le=5)
    # 这三项来自哪个环境变量：前端据此标注"改 .env 后重启后端"。
    provider_env_var: str = "GOLDEN_EVAL_JUDGE_PROVIDER"
    model_env_var: str = "GOLDEN_EVAL_JUDGE_MODEL"
    base_url_env_var: str = "GOLDEN_EVAL_JUDGE_BASE_URL"
    # 哪几项被设置页覆盖了（其余仍是 .env 的值）。
    overridden_fields: list[str] = Field(default_factory=list)
    enabled: bool = False
    has_api_key: bool = False
    api_key_hint: str | None = None
    api_key_fingerprint: str | None = None
    judge_runnable: bool = False
    # 不能运行的缺项，前端直接显示「还差什么」。
    judge_blockers: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class JudgeSettingsUpdate(BaseModel):
    """设置页提交**全量**判定配置；留空/未提供的项表示"跟随 .env"。

    `provider=unset`、`base_url=""`、`model=""`、`timeout_seconds=null`、`json_mode=null`、
    `max_retries=null` 都会清掉对应的设置页覆盖，回落到 `.env`（或内置默认）。
    """

    model_config = ConfigDict(extra="forbid")

    provider: JudgeProviderKind = JudgeProviderKind.UNSET
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=120)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    json_mode: bool | None = None
    max_retries: int | None = Field(default=None, ge=0, le=5)
    enabled: bool = False
    api_key: str | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_key_update(self) -> JudgeSettingsUpdate:
        if self.base_url is not None and self.base_url.strip() and not self.base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("base_url must start with http:// or https://")
        if self.api_key is not None and not self.api_key.strip():
            raise ValueError("api_key must not be blank when provided")
        if self.clear_api_key and self.api_key is not None:
            raise ValueError("provide either api_key or clear_api_key, not both")
        return self


class EnvironmentSettingEntry(BaseModel):
    """`.env` 目录里的一项：键、当前值、默认值与说明；值里不含任何密钥。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    value: str
    default: str
    description: str
    overridden: bool


class EnvironmentSettings(BaseModel):
    """只读的部署环境摘要，供设置页展示"这个值从哪来"。"""

    model_config = ConfigDict(extra="forbid")

    env_file: str
    env_file_found: bool
    example_file: str
    entries: list[EnvironmentSettingEntry]


class PromptKind(StrEnum):
    """Editable internal prompts."""

    JUDGE = "judge"
    PROFILE_ENRICHMENT = "profile_enrichment"
    RUBRIC_DRAFT = "rubric_draft"


class PromptSource(StrEnum):
    BUILTIN = "builtin"
    EDITED = "edited"


class PromptVersionSummary(BaseModel):
    """Version metadata only; the body is fetched separately so lists stay small."""

    model_config = ConfigDict(extra="forbid")

    prompt_kind: PromptKind
    version_label: str
    source: PromptSource
    content_sha256: str
    source_file_sha256: str | None = None
    notes: str | None = None
    is_active: bool = False
    char_count: int = Field(ge=0)
    created_at: datetime


class PromptVersion(PromptVersionSummary):
    content: str


class PromptSaveRequest(BaseModel):
    """Saving always creates a new immutable version; nothing is overwritten."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=200_000)
    version_label: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=500)
    activate: bool = True


class ContactLifecycleStage(StrEnum):
    """How far a Turn got through the isolated-contact lifecycle."""

    PLANNED = "planned"
    CREATED = "created"
    ROUTING_VERIFIED = "routing_verified"
    RESPONSE_OBSERVED = "response_observed"
    PROFILE_SETTLED = "profile_settled"


class ObservationStatus(StrEnum):
    COMPLETED = "completed"
    EXECUTION_ERROR = "execution_error"
    DELIVERY_UNKNOWN = "delivery_unknown"


class TurnSample(BaseModel):
    """一个 Turn 的坐标：会话 + 这个会话里的第几轮。"""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    seq: int = Field(ge=1)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["collect", "evaluate", "judge"]
    # 自定义名称，方便后续在任务列表里按名字找；留空时服务端按类型生成可读默认名。
    name: str | None = Field(default=None, max_length=120)
    dataset_id: str | None = Field(default=None, min_length=1)
    source_connection_id: str | None = Field(default=None, min_length=1)
    target_connection_id: str | None = Field(default=None, min_length=1)
    execution_target: Literal["fixture", "platform"] = "fixture"
    repeats: int = Field(default=1, ge=1, le=5)
    max_concurrency: int = Field(default=4, ge=1, le=50)
    collection_policy: CollectionPolicy | None = None
    # 只跑选中的 Turn（session + 第几轮），留空表示整个数据集；重复次数仍由 repeats 决定。
    sample_scope: list[TurnSample] | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> CreateRunRequest:
        if self.kind == "collect" and self.source_connection_id is None:
            raise ValueError("collect runs require source_connection_id")
        if self.kind == "evaluate" and (
            self.dataset_id is None or self.target_connection_id is None
        ):
            raise ValueError("evaluate runs require dataset_id and target_connection_id")
        if self.kind == "judge" and self.dataset_id is None:
            raise ValueError("judge runs require dataset_id")
        if self.collection_policy is not None and self.kind != "collect":
            raise ValueError("collection_policy is only valid for collect runs")
        if self.sample_scope is not None:
            if self.kind == "collect":
                raise ValueError("sample_scope is only valid for evaluate/judge runs")
            if not self.sample_scope:
                raise ValueError("sample_scope must not be empty when provided")
            seen = {(item.session_id, item.seq) for item in self.sample_scope}
            if len(seen) != len(self.sample_scope):
                raise ValueError("sample_scope contains duplicate turns")
        if self.execution_target == "platform":
            if self.kind == "collect" and self.source_connection_id is None:
                raise ValueError("platform collect runs require source_connection_id")
            if self.kind == "evaluate" and self.target_connection_id is None:
                raise ValueError("platform evaluate runs require target_connection_id")
        return self


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    kind: Literal["collect", "evaluate", "judge"]
    name: str = ""
    status: RunStatus
    request: CreateRunRequest
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    identity: RunIdentity | None = None


class RunRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class RunConcurrencyUpdate(BaseModel):
    """改单个 Run 的并发上限；创建时的取值在 `CreateRunRequest.max_concurrency`。"""

    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(ge=1, le=50)


class JudgeModelRequest(BaseModel):
    """让平台自己调用判定模型；`max_items` 限制单次调用判多少 Turn。"""

    model_config = ConfigDict(extra="forbid")

    max_items: int = Field(default=6, ge=1, le=40)


class RubricModelRequest(BaseModel):
    """一键起草 rubric：默认给所有尚未用当前提示词起草过的 Session 生成。"""

    model_config = ConfigDict(extra="forbid")

    session_ids: list[str] | None = None
    force: bool = False
    max_sessions: int = Field(default=20, ge=1, le=100)


class RubricSessionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: Literal["generated", "skipped", "failed", "deferred"]
    detail: str = ""


class RubricModelResponse(BaseModel):
    """一次批量起草的结果：写了几个、跳过几个、失败原因与调用审计。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    revision: int
    model: str
    prompt_version: str
    prompt_sha256: str
    sessions_total: int
    generated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    deferred: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    outcomes: list[RubricSessionOutcome] = Field(default_factory=list)


class ProfileModelRequest(BaseModel):
    """一键补全画像金标：默认给所有尚未用当前提示词补全过的 Session 生成。"""

    model_config = ConfigDict(extra="forbid")

    session_ids: list[str] | None = None
    force: bool = False
    max_sessions: int = Field(default=20, ge=1, le=100)


class ProfileSessionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: Literal["generated", "skipped", "failed", "deferred"]
    detail: str = ""


class ProfileModelResponse(BaseModel):
    """一次批量补全的结果：补了几个、跳过几个、失败原因与调用审计。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    revision: int
    model: str
    prompt_version: str
    prompt_sha256: str
    sessions_total: int
    generated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    deferred: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    outcomes: list[ProfileSessionOutcome] = Field(default_factory=list)


class AutoModelRequest(BaseModel):
    """一键生成 rubric + 画像：两个模型各跑一次，各自校验、各自落库。"""

    model_config = ConfigDict(extra="forbid")

    session_ids: list[str] | None = None
    force: bool = False
    max_sessions: int = Field(default=20, ge=1, le=100)


class AutoModelResponse(BaseModel):
    """一次联合生成的结果：先画像后 rubric，两部分的结果各自独立、互不覆盖。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    revision: int
    model: str
    sessions_total: int
    generated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    deferred: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    profile: ProfileModelResponse
    rubric: RubricModelResponse


class JudgeBatchError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["batch", "item"]
    code: str
    detail: str
    turn_id: str | None = None
    repeat_index: int | None = None


class JudgeModelResponse(BaseModel):
    """一次模型判定的结果：判了多少、还剩多少、失败原因与调用审计信息。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    model: str
    prompt_version: str
    prompt_sha256: str
    requested: int = Field(ge=0)
    judged: int = Field(ge=0)
    remaining: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)
    usage: dict[str, Any] = Field(default_factory=dict)
    raw_excerpt: str = ""
    errors: list[JudgeBatchError] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    report_updated: bool = False


class SnapshotRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class DraftRenameRequest(BaseModel):
    """草案改名：只动 shared.dataset 的名称与领域，仍按 revision 走并发控制。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    domain: str | None = Field(default=None, max_length=120)


class EvaluationNotesRequest(BaseModel):
    """这个空间的评估口径例外：人写一句话，随金标冻结，优先级高于通用口径与 guidance。"""

    model_config = ConfigDict(extra="forbid")

    evaluation_notes: str = Field(default="", max_length=2000)


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    seq: int = Field(ge=1)
    event_type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    task_key: str
    stage: str
    status: TaskStatus
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class GoldenAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    content_type: str
    url: str
    size: int | None = Field(default=None, ge=0)
    # 留证副本：平台 CDN 链接会过期，stored_url 指向本平台对象存储里的字节。
    stored_url: str | None = None
    sha256: str | None = None
    stored: bool | None = None


class GoldenMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    type: str = "text"
    content: str
    attachments: list[GoldenAttachment] | None = None


class GoldenTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_messages: list[GoldenMessage] = Field(min_length=1)


class ProfileUpdateSource(StrEnum):
    """一条画像增量是怎么来的。字段级记录，前端据此区分「平台拉到的」与「模型补的」。"""

    # 采集时从平台日志直接拉到（`internalTool: visitor`）；是平台事实，补全模型不得覆盖。
    PULLED = "pulled"
    # 画像补全（外部模型或一键补全）写回。
    AI = "ai"
    # 人在金标编辑器里改的。
    MANUAL = "manual"


class GoldenProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    updated_from: Any | None = None
    # 旧草案没有这个字段，一律按「AI 补全」解释，与它们当初的来源一致。
    source: ProfileUpdateSource = ProfileUpdateSource.AI


class GoldenToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    params: dict[str, Any] | None = None
    result: Any | None = None


class GoldenState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    changed_this_turn: bool | None
    prev: str | None
    reason: str


class GoldenTurnExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[GoldenMessage] = Field(default_factory=list)
    profile_update: dict[str, GoldenProfileUpdate] = Field(default_factory=dict)
    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    state: GoldenState | None = None
    tool_calls: list[GoldenToolCall] = Field(default_factory=list)


class GoldenTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    session_id: str
    seq: int = Field(ge=1)
    input: GoldenTurnInput
    expected: GoldenTurnExpected
    rubric: str
    # 完整前序历史里，先于本轮客户消息的消息条数；teacher forcing 用它截取 history。
    history_length: int | None = None

    @model_validator(mode="after")
    def validate_sequences(self) -> GoldenTurn:
        for label, messages in (
            ("input", self.input.customer_messages),
            ("expected", self.expected.messages),
        ):
            seqs = [message.seq for message in messages]
            if seqs != list(range(1, len(messages) + 1)):
                raise ValueError(f"{self.turn_id} {label} message seq must be contiguous from 1")
        expected_turn_id = f"{self.session_id}#T{self.seq}"
        if self.turn_id != expected_turn_id:
            raise ValueError(f"turn_id must be {expected_turn_id}")
        return self


class GoldenSessionMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    source: str
    annotator: str
    annotated_at: str
    review_status: str
    design_intent: str


REVIEW_STATUS_NEEDS_ANNOTATION = "needs_annotation"
RUBRIC_PLACEHOLDER = "【待人工补全】"


class GoldenMemoryBefore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: str
    is_first_contact: bool
    inherited_from: str | None = None
    profile_snapshot: dict[str, Any]
    state_snapshot: dict[str, Any]
    customer_memory: str
    note: str


class GoldenMemoryAfter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: str
    profile_snapshot: dict[str, Any]
    state_snapshot: dict[str, Any]
    customer_memory: str


class GoldenSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_before: GoldenMemoryBefore


class GoldenSessionExpected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_after: GoldenMemoryAfter


class GoldenSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    meta: GoldenSessionMeta
    input: GoldenSessionInput
    turns: list[GoldenTurn] = Field(min_length=1)
    expected: GoldenSessionExpected
    # 完整前序对话（visitor→user，human/ai→assistant），含人工回复 Turn；不进 v0.4 turns，
    # 只在 teacher forcing 时作为上下文，不参与评分。
    history: list[ConversationMessage] | None = None

    @model_validator(mode="after")
    def validate_turns(self) -> GoldenSession:
        ordered = sorted(self.turns, key=lambda turn: turn.seq)
        if [turn.seq for turn in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError(f"session {self.session_id} turn seq must be contiguous from 1")
        if any(turn.session_id != self.session_id for turn in ordered):
            raise ValueError(f"session {self.session_id} contains a foreign turn")
        profile_before = dict(self.input.memory_before.profile_snapshot)
        for turn in ordered:
            expected_snapshot = dict(profile_before)
            for key, update in turn.expected.profile_update.items():
                if key in profile_before and profile_before[key] not in ("", []):
                    if update.updated_from is None:
                        raise ValueError(
                            f"{turn.turn_id} profile_update.{key}.updated_from is required"
                        )
                    if update.updated_from != profile_before[key]:
                        raise ValueError(
                            f"{turn.turn_id} profile_update.{key}.updated_from mismatch"
                        )
                expected_snapshot[key] = update.value
            if turn.expected.profile_snapshot != expected_snapshot:
                raise ValueError(
                    f"{turn.turn_id} profile snapshot does not match prior state + update"
                )
            profile_before = dict(turn.expected.profile_snapshot)
        if self.expected.memory_after.profile_snapshot != profile_before:
            raise ValueError(f"session {self.session_id} memory_after profile mismatch")
        return self


class GoldenDatasetHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    schema_version: Literal["golden-dialogue-v0.4"]
    domain: str
    source_config: dict[str, Any]
    files: dict[str, str]

    @model_validator(mode="after")
    def validate_files(self) -> GoldenDatasetHeader:
        if self.files != {
            "shared": "golden-dialogue-shared.json",
            "cases": "golden-dialogue-cases.jsonl",
        }:
            raise ValueError("dataset.files must reference the canonical v0.4 filenames")
        return self


class GoldenSopStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    definition: str
    entry: str | None = None
    exit: str | None = None
    typical_actions: list[str] | None = None
    terminal: bool | None = None
    off_main_path: bool | None = None
    note: str | None = None


class GoldenProfileField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    # 中文名与来源来自冻结的 target 配置；字段 key 本身不能解释业务语义。
    label: str = ""
    type: str
    description: str
    value_format: str
    examples: list[Any]
    updatable: bool
    ttl_days: int | None = Field(default=None, ge=0)
    source: Literal["visitor_field", "custom_field"] = "custom_field"


class GoldenToolParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: str
    required: bool
    description: str
    format: str | None = None
    enum: list[Any] | None = None


class GoldenAvailableTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str
    params: list[GoldenToolParam]


class GoldenSharedContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str
    sop_stages: list[GoldenSopStage] | None = None
    sop_stages_note: str | None = None
    profile_schema: list[GoldenProfileField]
    profile_schema_note: str
    available_tools: list[GoldenAvailableTool]
    available_tools_note: str
    knowledge_base_version: str
    guidance: str | None = None
    skill_rules: list[dict[str, str]] = Field(default_factory=list)
    # 评估范围与观测限制；不覆盖空间业务禁令，不证明未观察到的动作已完成。
    evaluation_notes: str = ""
    # 发布/审核流程不自动证明参考答案已获业务确认；旧数据保持未知。
    reference_status: Literal["unknown", "generated", "confirmed"] = "unknown"

    @model_validator(mode="after")
    def validate_unique_definitions(self) -> GoldenSharedContext:
        profile_keys = [field.key for field in self.profile_schema]
        if len(profile_keys) != len(set(profile_keys)):
            raise ValueError("context.profile_schema keys must be unique")
        tool_names = [tool.name for tool in self.available_tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("context.available_tools names must be unique")
        if self.sop_stages is not None:
            stage_ids = [stage.id for stage in self.sop_stages]
            if len(stage_ids) != len(set(stage_ids)):
                raise ValueError("context.sop_stages ids must be unique")
        return self


class CustomerMemorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str
    version: str
    definition: str


class GoldenShared(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: GoldenDatasetHeader
    context: GoldenSharedContext
    customer_memory_spec: CustomerMemorySpec


class DraftStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class CreateGoldenDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shared: GoldenShared
    sessions: list[GoldenSession] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_sessions(self) -> CreateGoldenDraftRequest:
        session_ids = [session.session_id for session in self.sessions]
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("draft session_id values must be unique")
        return self


class BuildDraftFromSnapshotRequest(BaseModel):
    """Mechanical snapshot -> draft conversion. Semantic fields stay for human annotation."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    domain: str | None = Field(default=None, min_length=1)
    profile_keys: list[str] = Field(default_factory=list)
    session_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_unique_keys(self) -> BuildDraftFromSnapshotRequest:
        if len(self.profile_keys) != len(set(self.profile_keys)):
            raise ValueError("profile_keys must be unique")
        return self


class DeriveDraftFromDatasetRequest(BaseModel):
    """Start a new editable draft from an already published Dataset Version."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    domain: str | None = Field(default=None, min_length=1)


class DraftBuildAnomaly(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    code: str
    detail: str


class BuildDraftFromSnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    draft_id: str
    revision: int = Field(ge=1)
    session_count: int = Field(ge=1)
    turns_per_session: dict[str, int] = Field(default_factory=dict)
    anomalies: list[DraftBuildAnomaly] = Field(default_factory=list)


class ProfileResultItem(BaseModel):
    """One Turn's enrichment result, as produced from the worklist."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1)
    profile_update: dict[str, Any] = Field(default_factory=dict)
    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class ProfileResultsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    results: list[ProfileResultItem] = Field(min_length=1)


class ProfileEnrichmentRecord(BaseModel):
    """Platform-side bookkeeping for the profile enrichment step.

    The v0.4 golden contract has no room for platform state, so «this Session's profile golden came
    out of the enrichment step» is recorded next to the draft instead of inside the cases. The
    fingerprint pins the profile part of the Session, so editing only the rubric keeps the record
    valid while editing profile values invalidates it.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    prompt_kind: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    applied_at: datetime


def profile_golden_fingerprint(session: GoldenSession) -> str:
    """Hash only the profile part of a Session: recursion inputs, per-Turn updates and snapshots.

    `updated_from` is annotation metadata whose recursion is already checked by the shared contract,
    so it is left out: re-annotating it (or omitting it for an empty previous value) must not be
    reported as a changed profile golden.
    """

    material = {
        "memory_before": session.input.memory_before.profile_snapshot,
        "turns": [
            {
                "turn_id": turn.turn_id,
                "profile_update": {
                    key: value.value
                    for key, value in sorted(turn.expected.profile_update.items())
                },
                "profile_snapshot": turn.expected.profile_snapshot,
            }
            for turn in sorted(session.turns, key=lambda item: item.seq)
        ],
        "memory_after": session.expected.memory_after.profile_snapshot,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RubricDraftRecord(BaseModel):
    """Platform-side bookkeeping for the AI rubric draft of one Session.

    The v0.4 cases keep the rubric as a fixed-heading string, so which prompt version produced it
    is recorded next to the draft instead of inside the golden set.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    prompt_kind: str = ""
    prompt_version: str = ""
    prompt_sha256: str = ""
    turn_count: int = Field(default=0, ge=0)
    applied_at: datetime


class RubricTurnSections(BaseModel):
    """One Turn's rubric as the external model returns it (structured, not a heading string)."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1)
    must_cover: list[str] = Field(min_length=1, max_length=12)
    forbidden: list[str] = Field(min_length=1, max_length=12)
    lead_action: str = Field(min_length=1, max_length=300)
    tools: str = Field(min_length=1, max_length=300)
    knowledge: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=600)

    @field_validator("must_cover", "forbidden", mode="before")
    @classmethod
    def accept_single_string(cls, value: Any) -> Any:
        """模型常把"只有一条"的小节写成字符串而不是数组；语义相同，直接包成一项。"""

        return _as_list(value)


class RubricResultsRequest(BaseModel):
    """Structured rubric results for one Session, applied through the rubric worklist round-trip."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    turns: list[RubricTurnSections] = Field(min_length=1)


class GoldenDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    revision: int = Field(ge=1)
    status: DraftStatus
    shared: GoldenShared
    sessions: list[GoldenSession] = Field(default_factory=list)
    profile_enrichment: list[ProfileEnrichmentRecord] = Field(default_factory=list)
    rubric_drafts: list[RubricDraftRecord] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class DraftModelRun(BaseModel):
    """一键起草/补全的后台任务记录：跨刷新可定位，进度与结果都持久化。"""

    model_config = ConfigDict(extra="forbid")

    model_run_id: str
    draft_id: str
    kind: Literal["rubric", "profile", "auto"]
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    prompt_version: str = ""
    prompt_sha256: str = ""
    progress_current: int = Field(default=0, ge=0)
    progress_total: int = Field(default=0, ge=0)
    result_json: str = "{}"
    error: str | None = None
    created_at: datetime
    updated_at: datetime


# 进程重启后残留的模型任务统一用这条说明收尾，前端据此提示「可以重新发起」。
MODEL_RUN_INTERRUPTED = "模型任务被中断（服务重启或进程退出），请重新发起"


class GoldenDraftSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    revision: int = Field(ge=1)
    status: DraftStatus
    name: str
    schema_version: Literal["golden-dialogue-v0.4"]
    session_count: int = Field(ge=0)
    # 这份草案是从哪张快照生成的（导入/空白新建时为 None）；项目页据此认领归属。
    snapshot_id: str | None = None
    updated_at: datetime


class DraftValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    code: str
    message: str


class DraftValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    revision: int
    valid: bool
    issues: list[DraftValidationIssue] = Field(default_factory=list)


class DatasetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    draft_id: str
    draft_revision: int
    schema_version: Literal["golden-dialogue-v0.4"]
    session_count: int = Field(ge=1)
    manifest: dict[str, Any]
    published_at: datetime


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str
    attachments: list[GoldenAttachment] = Field(default_factory=list)


class ConversationRequest(BaseModel):
    """Safe projection sent to the tested Agent; golden expectations cannot cross this seam."""

    model_config = ConfigDict(extra="forbid")

    account_id: str
    messages: list[ConversationMessage] = Field(min_length=1)
    profile_before: dict[str, Any] = Field(default_factory=dict)
    bot_user_id: str
    im_bot_id: str



def _as_list(value: Any) -> Any:
    """模型把列表小节写成字符串时按单元素列表处理；空值按空列表。"""

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return value


class AgentLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    name: str | None = None
    status: str | None = None
    message: str | None = None
    cost_time_ms: int | None = None


class ConversationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ConversationMessage] = Field(default_factory=list)
    agent_logs: list[AgentLog] = Field(default_factory=list)
    profile_after: dict[str, Any] = Field(default_factory=dict)
    execution_status: Literal["completed", "failed"] = "completed"


class ConversationReply(BaseModel):
    """What one chat call returned: the reply, its dialog id, and the platform's own AgentLogs."""

    model_config = ConfigDict(extra="forbid")

    messages: list[ConversationMessage] = Field(default_factory=list)
    dialog_id: str | None = None
    agent_logs: list[dict[str, Any]] = Field(default_factory=list)
    chat_transport: str = "sync"
    # 网关从响应头回写的 request_id；用于确认模型调用日志按哪个 ID 关联
    request_id_echoed: str | None = None


class TurnTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    turn_id: str
    seq: int
    repeat_index: int
    account_id: str
    profile_before: dict[str, Any]
    messages: list[ConversationMessage]
    expected: GoldenTurnExpected
    rubric: str
    bot_user_id: str
    im_bot_id: str

    def to_conversation_request(self) -> ConversationRequest:
        return ConversationRequest(
            account_id=self.account_id,
            messages=self.messages,
            profile_before=self.profile_before,
            bot_user_id=self.bot_user_id,
            im_bot_id=self.im_bot_id,
        )


class ToolDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_tool_names: list[str]
    actual_tool_names: list[str]
    missing_tool_names: list[str]
    unexpected_tool_names: list[str]
    name_set_match: bool
    evidence_refs: list[str] = Field(min_length=1)


class TurnObservation(BaseModel):
    """Immutable record of one executed Turn: what was sent, what came back, what was read back."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str
    run_id: str
    session_id: str
    turn_id: str
    seq: int
    repeat_index: int
    status: ObservationStatus
    stage_reached: ContactLifecycleStage
    account_id: str
    execution_target: Literal["fixture", "platform"]
    request_messages: list[ConversationMessage] = Field(default_factory=list)
    response_messages: list[ConversationMessage] = Field(default_factory=list)
    profile_before: dict[str, Any] = Field(default_factory=dict)
    profile_observed_before: dict[str, Any] | None = None
    profile_after: dict[str, Any] | None = None
    profile_immediate_after: dict[str, Any] | None = None
    profile_readback: dict[str, Any] = Field(default_factory=dict)
    profile_writes: dict[str, Any] = Field(default_factory=dict)
    bot_user_id: str | None = None
    im_bot_id: str | None = None
    routing_verified: bool = False
    routing_evidence: dict[str, Any] = Field(default_factory=dict)
    chat_transport: str = "unknown"
    tool_diagnostics: ToolDiagnostics | None = None
    agent_logs: list[AgentLog] = Field(default_factory=list)
    raw_agent_logs: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_detail: str | None = None
    # 对话请求头里的 request_id 与网关回写值，模型调用链路据此到网关日志关联
    replay_request_id: str | None = None
    replay_request_id_echoed: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def new_observation_id() -> str:
    return f"obs_{uuid4().hex}"


JudgeStatus = Literal["pass", "fail", "unavailable"]


class JudgeCheck(BaseModel):
    """One sub-check of a metric (for example one must_cover checkpoint).

    The judge prompt asks for check ids but does not pin the key name, so `check` is accepted as an
    alias of `check_id`; per-check evidence is kept when the model supplies it.
    """

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(
        min_length=1, validation_alias=AliasChoices("check_id", "check", "id", "checkpoint")
    )
    status: JudgeStatus
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class JudgeRatio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = None


class JudgeMetricResult(BaseModel):
    """One metric verdict for one Turn, as returned by the judge model."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str = Field(min_length=1)
    status: JudgeStatus
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    checks: list[JudgeCheck] = Field(default_factory=list)
    ratio: JudgeRatio | None = None
    diagnostic_code: str | None = None
    # 人工复核改判留下的痕迹：原始判定仍在 judge_results 里，报告上用这个标记覆盖过。
    overridden: bool = False


class JudgeTurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1)
    repeat_index: int = Field(ge=1)
    metrics: list[JudgeMetricResult] = Field(min_length=1)


class JudgeResultsRequest(BaseModel):
    """Structured judge results for one evaluate run."""

    model_config = ConfigDict(extra="forbid")

    items: list[JudgeTurnResult] = Field(min_length=1)


class JudgeMetricSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: str
    name: str
    # 界面用的顺序编号（1 起）；不输出的历史维度为 None。
    display_index: int | None = None
    mode: str
    pass_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    denominator: int = Field(ge=0)
    pass_rate: float | None = None
    mean_ratio: float | None = None


class JudgeTurnReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    seq: int = Field(ge=1)
    repeat_index: int = Field(ge=1)
    status: Literal["judged", "missing_judgement"]
    metrics: list[JudgeMetricResult] = Field(default_factory=list)
    turn_status: Literal["pass", "fail", "unavailable"]
    turn_pass: bool | None = None
    failed_metric_ids: list[str] = Field(default_factory=list)
    tool_diagnostics: ToolDiagnostics | None = None


class JudgeAttemptReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repeat_index: int = Field(ge=1)
    turns: list[JudgeTurnReport] = Field(default_factory=list)
    session_status: Literal["pass", "fail", "unavailable"]
    session_pass: bool | None = None
    first_failure_turn: int | None = None


class JudgeSessionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    attempts: list[JudgeAttemptReport] = Field(default_factory=list)
    passed_attempt_count: int = Field(ge=0)
    pass_at_k: dict[str, float | None] = Field(default_factory=dict)


class JudgeReport(BaseModel):
    """Code-aggregated evaluation report for one evaluate run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    dataset_id: str
    judge_contract_version: str
    judge_prompt_version: str
    judge_prompt_sha256: str
    scoring_policy: str
    repeats: int = Field(ge=1)
    judged_turn_count: int = Field(ge=0)
    missing_turn_count: int = Field(ge=0)
    metric_summaries: list[JudgeMetricSummary] = Field(default_factory=list)
    sessions: list[JudgeSessionReport] = Field(default_factory=list)
    first_failure_turn_distribution: dict[str, int] = Field(default_factory=dict)
    batch_pass_at_k: dict[str, float | None] = Field(default_factory=dict)
    quality_warnings: list[str] = Field(default_factory=list)
    review_revision_count: int = Field(default=0, ge=0)
    reviewed_metric_count: int = Field(default=0, ge=0)
    updated_at: datetime


class ReviewOverride(BaseModel):
    """One human override of a judge verdict (per Turn and metric)."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1)
    repeat_index: int = Field(ge=1)
    metric_id: str = Field(min_length=1)
    status: JudgeStatus
    reason: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    """Append a review revision: overrides plus who did it and why."""

    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(default="reviewer", min_length=1, max_length=120)
    note: str = Field(default="", max_length=1000)
    overrides: list[ReviewOverride] = Field(min_length=1)


class ReviewRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    revision_index: int = Field(ge=1)
    reviewer: str
    note: str = ""
    overrides: list[ReviewOverride] = Field(default_factory=list)
    created_at: datetime


def new_run_id() -> str:
    return f"run_{uuid4().hex}"
