"""一键补全画像金标：把每个 Session 的补全工单直接交给模型，人只做抽检。

和 rubric 起草、判定同一条路子：模型看到的输入与 `GET profile-worklist` 导出的工单**完全相同**
（同一份 system prompt、同一份逐 Turn 证据、同一个 output schema），回来之后走
`profile_enrichment.apply_results` 的同一套校验——字段白名单、`updated_from` 语义、快照递推、
Turn 覆盖完整性；一条不合格只影响那一个 Session。人不再需要逐个 Session 复制粘贴 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.models import (
    GoldenSession,
    GoldenShared,
    ProfileEnrichmentRecord,
    PromptVersion,
)
from ..integrations.structured_model import StructuredModelError, StructuredModelPort
from .profile_enrichment import (
    ProfileEnrichmentError,
    apply_results,
    build_worklist,
)

DEFAULT_MAX_SESSIONS = 20

# 模型偶尔会把结果包在这些键里；按顺序找第一个是数组的。
_RESULT_KEYS = ("results", "items", "turns", "profile_results")


@dataclass
class ProfileSessionOutcome:
    session_id: str
    status: str  # generated | failed
    detail: str = ""
    session: GoldenSession | None = None


@dataclass
class ProfileBatchOutcome:
    outcomes: list[ProfileSessionOutcome] = field(default_factory=list)
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
    records: list[ProfileEnrichmentRecord],
    requested: list[str] | None,
    force: bool,
    prompt_version: str,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> tuple[list[GoldenSession], list[str], list[str]]:
    """选出要补全的 Session，返回 (目标, 已跳过, 超出本次上限的)。"""

    wanted = set(requested) if requested else None
    done = {
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
        if not force and session.session_id in done:
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
    """给一个 Session 生成并校验画像金标；失败时抛 StructuredModelError/ProfileEnrichmentError。"""

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
    results = _normalise_results(call.payload, worklist["items"])
    return apply_results(shared=shared, session=session, results=results)


def _normalise_results(payload: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把模型返回整理成 `apply_results` 要的形状。

    实测同一份工单，模型会在这几种形状之间随机切换：包在 `results`/`items` 里的数组、裸数组、
    NDJSON（传输层已收成 `items`）、按 turn_id 建键的对象、单 Turn 时的裸结果对象。这里统一收口。
    工单是逐 Turn 的，提示词没有要求模型回抄 `turn_id`，所以按工单顺序补齐；但长度必须对得上，
    否则宁可整条失败，也不猜哪个 Turn 是哪个。
    """

    expected = [str(item.get("turn_id") or "") for item in items]
    if isinstance(payload, list):
        raw: Any = payload
    elif isinstance(payload, dict):
        raw = next(
            (payload[key] for key in _RESULT_KEYS if isinstance(payload.get(key), list)),
            None,
        )
        if raw is None and "profile_update" in payload:
            raw = [payload]  # 只有一个 Turn 时模型可能直接返回裸对象
        if raw is None and payload:
            values = list(payload.values())
            keyed = all(isinstance(value, dict) for value in values) and any(
                "profile_update" in value or "profile_snapshot" in value for value in values
            )
            if keyed:
                # 按 turn_id 建键返回：键就是 id 就用它，否则退回收到的顺序。
                known = set(expected)
                raw = [
                    {**value, "turn_id": key if key in known else ""}
                    for key, value in payload.items()
                ]
    else:
        raw = None
    if not isinstance(raw, list) or not raw:
        shape = (
            f"顶层键 {sorted(payload)[:8]}" if isinstance(payload, dict) else type(payload).__name__
        )
        raise ProfileEnrichmentError(f"模型返回里找不到逐 Turn 的结果数组（{shape}）")

    if len(raw) != len(expected):
        raise ProfileEnrichmentError(
            f"模型返回 {len(raw)} 个 Turn 的结果，工单是 {len(expected)} 个，无法一一对应"
        )
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ProfileEnrichmentError(f"第 {index + 1} 个结果不是 JSON 对象")
        entries.append(dict(entry))

    # 模型回抄了完整的 turn_id 就按 id 对齐（顺序可以乱），否则按工单顺序对齐。
    returned_ids = [str(entry.get("turn_id") or "") for entry in entries]
    if all(returned_ids) and len(set(returned_ids)) == len(returned_ids):
        unknown = sorted(set(returned_ids) - set(expected))
        if unknown:
            raise ProfileEnrichmentError(f"模型返回了工单里没有的 turn_id：{unknown}")
        position = {turn_id: index for index, turn_id in enumerate(expected)}
        entries.sort(key=lambda entry: position[str(entry["turn_id"])])
    else:
        for entry, returned, wanted in zip(entries, returned_ids, expected, strict=True):
            if returned and returned != wanted:
                raise ProfileEnrichmentError(
                    f"模型返回的 turn_id {returned} 与工单 {wanted} 不一致"
                )
    return [
        {**entry, "turn_id": expected[index]} for index, entry in enumerate(entries)
    ]


__all__ = [
    "DEFAULT_MAX_SESSIONS",
    "ProfileBatchOutcome",
    "ProfileEnrichmentError",
    "ProfileSessionOutcome",
    "StructuredModelError",
    "generate_one",
    "plan_sessions",
]
