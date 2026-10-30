"""一键起草 rubric：把每个 Session 的 rubric 工单直接交给模型，人只做抽检。

和判定一样，模型看到的输入与导出的工单**完全相同**（同一份 system prompt、同一份证据、
同一个 output schema）；回来之后走 `apply_results` 的同一套校验与编译，一条不合格只影响那一个
Session，其余照常写入。人不再需要逐个 Session 复制粘贴 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import (
    GoldenSession,
    GoldenShared,
    PromptVersion,
    RubricDraftRecord,
    RubricTurnSections,
)
from ..integrations.structured_model import StructuredModelError, StructuredModelPort
from .rubric_build import (
    RubricBuildError,
    apply_results,
    build_worklist,
)

DEFAULT_MAX_SESSIONS = 20


@dataclass
class RubricSessionOutcome:
    session_id: str
    status: str  # generated | failed
    detail: str = ""
    session: GoldenSession | None = None


@dataclass
class RubricBatchOutcome:
    outcomes: list[RubricSessionOutcome] = field(default_factory=list)
    latency_ms: int = 0
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def generated(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "generated")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "failed")


def plan_sessions(
    *,
    sessions: list[GoldenSession],
    records: list[RubricDraftRecord],
    requested: list[str] | None,
    force: bool,
    prompt_version: str,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> tuple[list[GoldenSession], list[str], list[str]]:
    """选出要起草的 Session，返回 (目标, 已跳过, 超出本次上限的)。"""

    wanted = set(requested) if requested else None
    drafted = {
        item.session_id
        for item in records
        if item.prompt_version == prompt_version and not force
    }
    targets: list[GoldenSession] = []
    skipped: list[str] = []
    deferred: list[str] = []
    for session in sessions:
        if wanted is not None and session.session_id not in wanted:
            continue
        if not force and session.session_id in drafted:
            skipped.append(session.session_id)
            continue
        if len(targets) >= max_sessions:
            deferred.append(session.session_id)
            continue
        targets.append(session)
    return targets, skipped, deferred


def generate_one(
    *,
    session: GoldenSession,
    shared: GoldenShared,
    draft_id: str,
    revision: int,
    prompt: PromptVersion,
    model: StructuredModelPort,
) -> GoldenSession:
    """给一个 Session 生成并编译 rubric；失败时抛 StructuredModelError/RubricBuildError。"""

    worklist = build_worklist(
        draft_id=draft_id,
        revision=revision,
        shared=shared,
        sessions=[session],
        prompt=prompt,
    )
    call = model.generate(
        prompt_version=prompt.version_label,
        system_prompt=prompt.content,
        input_document={key: value for key, value in worklist.items() if key != "system_prompt"},
        output_schema=worklist["output_schema"],
        model_profile=getattr(model, "model", ""),
    )
    payload = call.payload
    session_id = str(payload.get("session_id") or "").strip()
    if session_id and session_id != session.session_id:
        raise RubricBuildError(
            f"模型返回的 session_id {session_id} 与工单 {session.session_id} 不一致"
        )
    raw_turns = payload.get("turns")
    if not isinstance(raw_turns, list):
        raise RubricBuildError("模型返回缺少 turns 数组")
    try:
        turns = [RubricTurnSections.model_validate(item) for item in raw_turns]
    except ValueError as exc:
        raise RubricBuildError(f"rubric 结构不合法：{str(exc)[:300]}") from exc
    return apply_results(session=session, turns=turns)


__all__ = [
    "DEFAULT_MAX_SESSIONS",
    "RubricBatchOutcome",
    "RubricSessionOutcome",
    "StructuredModelError",
    "generate_one",
    "plan_sessions",
]
