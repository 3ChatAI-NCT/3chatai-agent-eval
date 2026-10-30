import hashlib
import ipaddress
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Mode(StrEnum):
    ATOMIC = "atomic_replay"
    GOLDEN = "golden_regression"


class DatasetIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    mode: Mode
    items: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    shared: dict[str, Any] = {}
    source_snapshot_id: str | None = None


class AtomicCaptureIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    start: datetime
    end: datetime
    size: int = Field(ge=1, le=300)
    model: str | None = None
    terms: list[str] = Field(default_factory=list, max_length=5)


class GoldenCaptureIn(Strict):
    name: str = Field(min_length=1, max_length=120)
    policy: dict[str, Any]


class GoldenCasePatch(Strict):
    turn_index: int = Field(ge=0)
    rubric: str = Field(min_length=1, max_length=10000)
    expected_messages: list[str | None] = Field(min_length=1)
    expected_profile_update: dict[str, Any] | None = None


def public_https(value: str) -> str:
    # Worker 在集群内发请求：只允许公网 HTTPS 域名，避免借平台访问集群内部服务
    url = urlsplit(value.strip())
    host = url.hostname or ""
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    internal = host.endswith((".local", ".svc", ".internal", ".localhost"))
    if (url.scheme != "https" or "." not in host or literal_ip or internal
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("接口地址必须是公网 HTTPS 域名，例如 https://api.example.com/v1")
    path = url.path.rstrip("/").removesuffix("/responses")
    return f"https://{url.netloc}{path}"


# 候选接口地址：只接受公网 HTTPS 域名（见 public_https）
PublicEndpoint = Annotated[str, Field(min_length=1, max_length=300), AfterValidator(public_https)]
ModelName = Annotated[str, Field(min_length=1, max_length=120)]
GatewayModel = Annotated[str, Field(max_length=100, pattern=r"^[A-Za-z0-9._:/-]+$")]


class CandidateIn(Strict):
    """临时填写的候选模型接口；key 只加密保存到评测结束。"""

    base_url: PublicEndpoint
    model: ModelName
    api_key: SecretStr = Field(min_length=1, max_length=500)


class CandidatePresetIn(Strict):
    """设置页维护的候选接口预设；更新时 api_key 留空表示沿用原 key。"""

    name: str = Field(min_length=1, max_length=60)
    base_url: PublicEndpoint
    model: ModelName
    api_key: SecretStr | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=300)
    revision: int = Field(default=0, ge=0)


class CandidateProbeIn(Strict):
    preset_id: str | None = None
    base_url: PublicEndpoint
    model: ModelName
    api_key: SecretStr | None = Field(default=None, max_length=500)


class ExperimentIn(Strict):
    """原子回放的候选来源三选一：3Chat 网关模型、已保存的接口预设、临时填写。"""

    name: str = Field(min_length=1, max_length=120)
    dataset_version_id: str
    candidate: CandidateIn | None = None
    candidate_preset_id: str | None = None
    candidate_gateway_model: GatewayModel | None = None
    repeats: int = Field(default=1, ge=1, le=10)
    execution_target: Literal["platform", "fixture"] = "platform"
    sample_scope: list[str] | None = None
    reuse_experiment_id: str | None = None
    # 试跑用于调参、验证链路，结束满 7 天自动归档
    trial: bool = False

    @model_validator(mode="after")
    def one_candidate_source(self):
        sources = [self.candidate, self.candidate_preset_id, self.candidate_gateway_model]
        if sum(source is not None for source in sources) > 1:
            raise ValueError("候选模型只能从网关模型、接口预设、临时填写中选一种")
        return self


class BatchIn(Strict):
    """多个空间按统一条件采样，逐轮使用各空间配置，单步使用同一候选模型。"""

    name: str = Field(min_length=1, max_length=100)
    space_ids: list[str] = Field(min_length=1, max_length=30)
    mode: Mode = Mode.ATOMIC
    min_turns: int = Field(default=3, ge=1, le=50)
    repeats: int = Field(default=3, ge=1, le=5)
    start: datetime
    end: datetime
    size: int = Field(ge=1, le=300)
    candidate_gateway_model: GatewayModel | None = None
    candidate_preset_id: str | None = None
    trial: bool = False

    @model_validator(mode="after")
    def valid_batch(self):
        if self.mode == Mode.GOLDEN:
            if self.candidate_gateway_model or self.candidate_preset_id:
                raise ValueError("逐轮回放使用各 EVAL 空间配置，无需选择候选模型")
            if self.size > 100:
                raise ValueError("逐轮采样每个空间最多 100 个会话")
        elif (self.candidate_gateway_model is None) == (self.candidate_preset_id is None):
            raise ValueError("请选择一种候选模型：3Chat 网关模型或已保存的接口")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("请提供带时区的有效采集时间窗")
        if len(set(self.space_ids)) != len(self.space_ids):
            raise ValueError("评测空间不能重复")
        return self


class BatchReplayIn(Strict):
    name: str = Field(min_length=1, max_length=100)
    candidate_gateway_model: GatewayModel | None = None
    candidate_preset_id: str | None = None

    @model_validator(mode="after")
    def one_candidate(self):
        if (self.candidate_gateway_model is None) == (self.candidate_preset_id is None):
            raise ValueError("请选择一种候选模型：3Chat 网关模型或已保存的接口")
        return self


class SpaceModelIn(Strict):
    """为一个或多个 EVAL 空间设置执行模型；model 为空表示恢复默认。"""

    space_ids: list[str] = Field(min_length=1, max_length=200)
    model: GatewayModel | None = None

    @model_validator(mode="after")
    def unique_spaces(self):
        if len(set(self.space_ids)) != len(self.space_ids):
            raise ValueError("评测空间不能重复")
        return self


class ArchiveIn(Strict):
    ids: list[str] = Field(min_length=1, max_length=200)
    archived: bool = True


class JudgeConfig(Strict):
    """全局 Judge 配置；创建评测时冻结进 manifest，之后修改不影响已创建的评测。"""

    model: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:/-]+$")
    timeout_seconds: int = Field(default=180, ge=30, le=600)
    format_attempts: int = Field(default=3, ge=1, le=5)


class JudgeConfigIn(Strict):
    config: JudgeConfig
    revision: int = Field(ge=0)


class ReportConfig(Strict):
    """原子回放的人工盲评：创建评测时冻结进 manifest，之后修改只影响新评测。

    评测结束后按样本量抽取随机样本与风险样本，揭晓前所有人看不到 AI 结论与模型身份。
    """

    blind_review: bool = True
    random_percent: int = Field(default=10, ge=1, le=100)
    min_random: int = Field(default=5, ge=1, le=300)
    risk_percent: int = Field(default=5, ge=0, le=100)


class ReportConfigIn(Strict):
    config: ReportConfig
    revision: int = Field(ge=0)


class BlindReviewIn(Strict):
    """盲评只提交 A/B 选择，由服务端换成相对候选的结论；看不到 AI 评审，不能归因于评审错误。"""

    choice: Literal["A", "B", "S", "U"]
    issue_kind: Literal["none", "agent_issue", "environment_issue"] = "none"
    note: str = Field(default="", max_length=4000)
    outputs_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(default=0, ge=0)


class RevealIn(Strict):
    reason: str = Field(default="", max_length=500)


class ReviewIn(Strict):
    actor_type: Literal["human", "ai_assisted"] = "human"
    label: Literal["G", "S", "B", "U", "pass", "fail", "unavailable"]
    issue_kind: Literal["none", "gold_error", "judge_error", "agent_issue",
                        "environment_issue"] = "none"
    note: str = Field(default="", max_length=4000)
    outputs_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(default=0, ge=0)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()
