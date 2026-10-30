"""评估域模型。

一条 Case 是一次被冻结的原子请求，带上若干个被测输出（线上基线、候选模型…）；
成对盲评的结果结构见 pairwise.py。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(Strict):
    name: str
    arguments: dict = Field(default_factory=dict)
    status: str = "proposed"  # proposed | succeeded | failed，模型提出不等于已执行


class Output(Strict):
    """一个被测系统在这一轮的输出。"""

    side_id: str
    messages: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)


class EvalCase(Strict):
    """判定所需的全部输入，不含任何金标答案。"""

    case_id: str
    workspace_id: str = ""
    task_name: str = "mainagent"
    task_type: str = "mainagent"
    current_turn: str = ""  # 客户原话
    task_input: str = ""  # 子任务执行指令，由主 Agent 生成，不是客户原话
    prior_turns: list[dict] = Field(default_factory=list)  # 当前轮之前的真实对话
    business_rules: str = ""  # 租户下发的 system 规则
    history_tool_calls: list[ToolCall] = Field(default_factory=list)
    profile_snapshot: dict[str, str] = Field(default_factory=dict)  # 已持久化的长期画像
    session_facts: dict[str, str] = Field(default_factory=dict)  # 本次会话已知但未必已持久化
    available_tools: list[str] = Field(default_factory=list)
    tool_labels: dict[str, str] = Field(default_factory=dict)  # 工具名 → 可读标签
    outputs: list[Output] = Field(default_factory=list)

    def output_for(self, side_id: str) -> Output | None:
        return next((o for o in self.outputs if o.side_id == side_id), None)


class ModelCall(Strict):
    """一次线上模型调用，从网关日志解析而来。"""

    call_id: str
    workspace_id: str
    request: dict
    response: dict
    request_id: str
    timestamp: str = ""
    http_status: str = ""
    upstream_model: str = ""
    channel_id: str = ""
    attempt: str = "1"
    retry_count: str = "0"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    elapsed_ms: float | None = None


class Issue(Strict):
    dimension: str
    severity: Severity
    summary: str
    evidence_ref: str = ""
