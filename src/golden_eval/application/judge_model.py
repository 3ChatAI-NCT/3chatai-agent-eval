"""让平台自己调用判定模型：工单 → 模型 → 逐条校验 → 合并进已存判定。

这一层刻意做薄：模型使用同一份业务工单、system prompt 和 output schema；检查项在传输时
使用短编号，返回后精确还原原文。回来之后逐条跑 `validate_results`：
一条不合格只丢那一条，其余照常入库。模型的结论永远不直接进报告，门控、Session 状态、
指标汇总与 pass^k 仍由代码重算。

失败不落库、不伪造：调用失败或某条判定不合法时，那些 Turn 保持"未判定"，响应里带错误码供前端重试。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..domain.models import (
    JudgeMetricResult,
    JudgeTurnResult,
    PromptVersion,
    RunSummary,
    TurnObservation,
)
from ..integrations.structured_model import (
    StructuredModelError,
    StructuredModelPort,
)
from .judge_report import (
    JudgeResultError,
    build_worklist,
    validate_check_contract,
    validate_results,
)

DEFAULT_MAX_ITEMS = 6
MAX_MAX_ITEMS = 40


@dataclass
class JudgeBatchOutcome:
    requested: list[tuple[str, int]] = field(default_factory=list)
    judged: list[JudgeTurnResult] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    raw_excerpt: str = ""
    attempts: int = 1
    input_format: str = "canonical"

    @property
    def requested_count(self) -> int:
        return len(self.requested)

    @property
    def judged_count(self) -> int:
        return len(self.judged)


def pending_observations(
    observations: list[TurnObservation],
    results: list[JudgeTurnResult],
    *,
    limit: int = DEFAULT_MAX_ITEMS,
) -> list[TurnObservation]:
    """挑出还没判定的 Turn，按 (session, seq, repeat) 稳定排序后取前 N 条。"""

    judged = {(item.turn_id, item.repeat_index) for item in results}
    pending = [
        item
        for item in sorted(
            observations, key=lambda entry: (entry.session_id, entry.seq, entry.repeat_index)
        )
        if (item.turn_id, item.repeat_index) not in judged
    ]
    return pending[: max(1, limit)]


def judge_batch(
    *,
    run: RunSummary,
    sessions: dict[str, Any],
    pending: list[TurnObservation],
    prompt: PromptVersion,
    model: StructuredModelPort,
    shared: Any | None = None,
    model_profile: str = "",
    validation_feedback: list[dict[str, Any]] | None = None,
    execution_evidence: dict[str, dict[str, Any]] | None = None,
) -> JudgeBatchOutcome:
    """调用一次模型判定这一批 Turn；只返回可入库的判定与错误清单。"""

    outcome = JudgeBatchOutcome(
        requested=[(item.turn_id, item.repeat_index) for item in pending],
        model=model_profile,
    )
    if not pending:
        return outcome

    worklist = build_worklist(
        run=run, sessions=sessions, observations=pending, prompt=prompt, shared=shared,
        execution_evidence=execution_evidence,
    )
    if validation_feedback:
        worklist["validation_feedback"] = validation_feedback
    check_aliases = None
    model_worklist = worklist
    if prompt.version_label in {
        "full-evaluation-v12", "full-evaluation-v13", "full-evaluation-v14"}:
        model_worklist, check_aliases = _short_check_worklist(worklist)
        outcome.input_format = "short-check-ids-v1"
    try:
        call = model.generate(
            prompt_version=prompt.version_label,
            system_prompt=prompt.content,
            input_document={
                key: value
                for key, value in model_worklist.items()
                if key not in {"system_prompt"}
            },
            output_schema=worklist["output_schema"],
            model_profile=model_profile,
        )
    except StructuredModelError as exc:
        outcome.errors.append(
            {"scope": "batch", "code": exc.code, "detail": str(exc)[:500]}
        )
        return outcome

    outcome.latency_ms = call.latency_ms
    outcome.usage = dict(call.usage)
    outcome.raw_excerpt = call.raw_excerpt
    outcome.attempts = call.attempts
    outcome.model = call.model or model_profile
    _absorb_items(outcome, pending=pending, payload=call.payload,
                  check_aliases=check_aliases,
                  calibrated_profiles=prompt.version_label in {
                      "full-evaluation-v11", "full-evaluation-v12",
                      "full-evaluation-v13", "full-evaluation-v14"},
                  check_contracts={
                      (item["turn_id"], item["repeat_index"]): (
                          item["input"]["compiled_rubric"]["must_cover"],
                          set(item["input"]["allowed_evidence_refs"]))
                      for item in worklist["items"]
                  } if prompt.version_label in {
                      "full-evaluation-v12", "full-evaluation-v13", "full-evaluation-v14"}
                  else None,
                  required_profile_fields={
                      (item["turn_id"], item["repeat_index"]):
                      (item["input"]["golden"].get("profile_required_fields", [])
                       if prompt.version_label not in {"full-evaluation-v13", "full-evaluation-v14"}
                       or worklist["shared_context"].get("reference_status") == "confirmed"
                       else [])
                      for item in worklist["items"]})
    return outcome


def _short_check_worklist(
    worklist: dict[str, Any],
) -> tuple[dict[str, Any], dict[tuple[str, int], dict[str, str]]]:
    """仅替换传输编号；业务原文、原工单及最终结果保持不变。"""
    items, aliases = [], {}
    for item in worklist["items"]:
        compiled = item["input"]["compiled_rubric"]
        criteria = compiled["must_cover"]
        prefix = "C"
        while any(f"{prefix}{i}" in criteria for i in range(1, len(criteria) + 1)):
            prefix = "_" + prefix
        mapping = {f"{prefix}{i}": text for i, text in enumerate(criteria, start=1)}
        aliases[(item["turn_id"], item["repeat_index"])] = mapping
        items.append({**item, "input": {**item["input"], "compiled_rubric": {
            **compiled, "must_cover": list(mapping), "must_cover_details": mapping}}})
    return {**worklist, "items": items, "check_id_encoding": {
        "version": "short-check-ids-v1",
        "instruction": "For metric 5, evaluate must_cover_details requirements and return "
                       "their exact short identifiers from must_cover as check_id."}}, aliases


def _absorb_items(
    outcome: JudgeBatchOutcome,
    *,
    pending: list[TurnObservation],
    payload: dict[str, Any],
    calibrated_profiles: bool = False,
    required_profile_fields: dict[tuple[str, int], list[str]] | None = None,
    check_contracts: dict[tuple[str, int], tuple[list[str], set[str]]] | None = None,
    check_aliases: dict[tuple[str, int], dict[str, str]] | None = None,
) -> None:
    """逐条校验模型返回的判定：一条不合法不影响其余，也不写进库里。"""

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        outcome.errors.append(
            {
                "scope": "batch",
                "code": "model_missing_items",
                "detail": "模型返回的 JSON 里没有 items 数组",
            }
        )
        return
    by_key = {(item.turn_id, item.repeat_index): item for item in pending}
    seen: set[tuple[str, int]] = set()
    reported: set[tuple[str, int]] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            outcome.errors.append(
                {"scope": "item", "code": "model_item_not_object", "detail": str(raw)[:200]}
            )
            continue
        try:
            candidate = JudgeTurnResult.model_validate(_sanitise_judge_result(raw))
        except ValueError as exc:
            outcome.errors.append(
                {
                    "scope": "item",
                    "code": "model_item_invalid",
                    "detail": json.dumps(raw, ensure_ascii=False)[:300],
                    "error": str(exc)[:300],
                }
            )
            continue
        key = (candidate.turn_id, candidate.repeat_index)
        reported.add(key)
        observation = by_key.get(key)
        if observation is None:
            outcome.errors.append(
                {
                    "scope": "item",
                    "code": "model_item_unknown_turn",
                    "detail": f"{candidate.turn_id}#r{candidate.repeat_index} 不在本批工单里",
                }
            )
            continue
        if key in seen:
            outcome.errors.append(
                {
                    "scope": "item",
                    "code": "model_item_duplicate",
                    "detail": f"{candidate.turn_id}#r{candidate.repeat_index} 重复返回",
                }
            )
            continue
        seen.add(key)
        aliases = (check_aliases or {}).get(key, {})
        short_ids = [check.check_id for metric in candidate.metrics if metric.metric_id == "5"
                     for check in metric.checks if check.check_id in aliases]
        if aliases:
            candidate = candidate.model_copy(update={"metrics": [
                metric.model_copy(update={"checks": [
                    check.model_copy(update={
                        "check_id": aliases.get(check.check_id, check.check_id)})
                    for check in metric.checks]}) if metric.metric_id == "5" else metric
                for metric in candidate.metrics]})
        try:
            if len(short_ids) != len(set(short_ids)):
                raise JudgeResultError("duplicate_short_check_id: use each short identifier once")
            normalised = validate_results(observations=[observation], results=[candidate])
            _validate_consistency(candidate)
            if check_contracts is not None:
                required_checks, refs = check_contracts[key]
                validate_check_contract(candidate, required_checks, refs)
            if calibrated_profiles and any(
                len(metric.reason) > 250 or re.search(
                    r"(?:^|[。；\n])\s*(?:修正|更正|重新判断)[：:，]", metric.reason)
                for metric in candidate.metrics
            ):
                raise JudgeResultError(
                    "final_reason_required: return one final justification per metric in at most "
                    "200 Chinese characters; no draft, self-correction or contradictory verdicts."
                )
            profile_failures = [metric for metric in candidate.metrics
                                if metric.metric_id in {"2", "3"} and metric.status == "fail"]
            if calibrated_profiles and profile_failures and (
                len({metric.diagnostic_code for metric in profile_failures}) != 1
                or profile_failures[0].diagnostic_code not in {
                    "profile_missing_update", "profile_invalid_update"}
            ):
                raise JudgeResultError(
                    "profile_failure_basis: metrics 2/3 must use the same diagnostic_code: "
                    "profile_missing_update (absence) or profile_invalid_update "
                    "(observed wrong value); use unavailable for conflicting/missing rules."
                )
        except JudgeResultError as exc:
            constraint = re.match(r"^([a-z_]+):", str(exc))
            outcome.errors.append(
                {
                    "scope": "item",
                    "code": "model_item_contract_violation",
                    "detail": str(exc)[:400],
                    "constraint": constraint.group(1) if constraint else "result_contract",
                    "turn_id": candidate.turn_id,
                    "repeat_index": candidate.repeat_index,
                }
            )
            continue
        result = normalised[0]
        missing_required = any(
            (observation.profile_after or {}).get(field) in (None, "", [], {})
            for field in (required_profile_fields or {}).get(key, []))
        invalid_update = any(metric.diagnostic_code == "profile_invalid_update"
                             for metric in profile_failures)
        missing_async = (calibrated_profiles
                         and not invalid_update
                         and observation.profile_readback.get("completion_verified") is not True
                         and (missing_required or (profile_failures
                              and profile_failures[0].diagnostic_code == "profile_missing_update")))
        if (observation.profile_readback.get("settled") is False and not invalid_update
                or missing_async):
            # 只约束证据可用性；保留原始观测和其余维度的语义判定。
            result = result.model_copy(update={"metrics": [
                JudgeMetricResult(metric_id=metric.metric_id, status="unavailable",
                    reason=("观察窗口内未确认异步画像任务完成，尚未观察到的更新不能直接判为遗漏。"
                            if missing_async else
                            "画像回读未稳定，末次快照仅供诊断，本轮画像指标无法判断。"),
                    evidence_refs=[f"{observation.turn_id}#execution"],
                    diagnostic_code=("profile_completion_unverified" if missing_async
                                     else "profile_readback_unsettled"))
                if metric.metric_id in {"2", "3"} else metric for metric in result.metrics
            ]})
        outcome.judged.append(result)

    for turn_id, repeat_index in by_key:
        if (turn_id, repeat_index) in reported:
            continue
        outcome.errors.append(
            {
                "scope": "item",
                "code": "model_item_missing",
                "detail": f"{turn_id}#r{repeat_index} 没有出现在模型返回里",
                "turn_id": turn_id,
                "repeat_index": repeat_index,
            }
        )


_EXPLICIT_VERDICT = re.compile(
    r"(?:^|[，,。；;！？!?\n])\s*(?:(?:因此|所以|综上|最终|故)\s*)?"
    r"(?:(?:指标|Metric\s*)?\s*(?P<ids>[2345689](?:\s*[/、和与及]\s*[2345689])*)\s*)?"
    r"(?:(?:本项|该项|本指标)\s*)?(?:都|均|也)?\s*"
    r"(?:应(?:该)?(?:判(?:定)?为|为|是)?|判(?:定)?为|结论为|结果为|为|是)\s*"
    r"[`'\"“]?(?P<status>unavailable|fail|pass|不通过|未通过|无法判断|通过)"
    r"[`'\"”]?\s*[。.!！;；]?\s*$",
    re.IGNORECASE,
)


def _validate_consistency(candidate: JudgeTurnResult) -> None:
    """只门控新模型输出中的明确矛盾；不改分，也不重新校验历史报告。"""

    statuses = {"通过": "pass", "不通过": "fail", "未通过": "fail", "无法判断": "unavailable"}
    for metric in candidate.metrics:
        match = _EXPLICIT_VERDICT.search(metric.reason)
        if match and (not match["ids"] or metric.metric_id in match["ids"]):
            stated = match["status"].lower()
            stated = statuses.get(stated, stated)
            if stated != metric.status:
                raise JudgeResultError(
                    f"reason_status_conflict: metric {metric.metric_id} status={metric.status}, "
                    f"reason explicitly concludes {stated}; reassess this metric from evidence "
                    "and return one consistent final justification, without self-correction."
                )
        if metric.status == "pass" and any(check.status != "pass" for check in metric.checks):
            raise JudgeResultError(
                f"reason_status_conflict: metric {metric.metric_id} passes with non-pass checks"
            )
    profile = {metric.metric_id: metric.status for metric in candidate.metrics}
    if profile.get("2") != profile.get("3"):
        raise JudgeResultError("reason_status_conflict: metrics 2/3 must share one profile verdict")


def _sanitise_judge_result(raw: dict[str, Any]) -> dict[str, Any]:
    """模型偶尔会「创新」字段（如 status_note）；合同只认白名单，其余丢弃再校验。"""

    metric_keys = {
        "metric_id",
        "status",
        "reason",
        "evidence_refs",
        "checks",
        "ratio",
        "diagnostic_code",
    }
    result = dict(raw)
    metrics = result.get("metrics")
    if isinstance(metrics, list):
        result["metrics"] = [
            {key: value for key, value in metric.items() if key in metric_keys}
            if isinstance(metric, dict)
            else metric
            for metric in metrics
        ]
    return result
