"""Judge worklist, result validation and code-side aggregation.

The judge model only answers pass/fail/unavailable per metric with a reason and evidence; every
number in the report — the Turn gate, session status, metric summaries and pass^k — is recomputed
here, so a model can never change the aggregation by wording.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal

from ..domain.evaluation import (
    JUDGE_CONTRACT_VERSION,
    METRIC_CATALOG,
    RATIO_METRIC_ID,
    SCORING_POLICY_VERSION,
    TURN_GATE_METRIC_ORDER,
    TURN_METRIC_ORDER,
    metric_display_index,
)
from ..domain.models import (
    GoldenSession,
    GoldenShared,
    JudgeAttemptReport,
    JudgeMetricResult,
    JudgeMetricSummary,
    JudgeReport,
    JudgeSessionReport,
    JudgeTurnReport,
    JudgeTurnResult,
    PromptVersion,
    ReviewOverride,
    ReviewRevision,
    RunSummary,
    TurnObservation,
)
from .profile_rules import profile_definitions


class JudgeResultError(ValueError):
    """Raised when imported judge results do not satisfy the contract."""


def build_worklist(
    *,
    run: RunSummary,
    sessions: dict[str, GoldenSession],
    observations: list[TurnObservation],
    prompt: PromptVersion,
    shared: GoldenShared | None = None,
    part_index: int = 1,
    part_count: int = 1,
    execution_evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Export one item per executed Turn: golden, compiled rubric, actual, evidence refs.

    `part_index`/`part_count` slice the batch deterministically so a large run can be judged in
    several passes without losing the ability to resume.
    """

    extended_evidence = prompt.version_label in {
        "full-evaluation-v9", "full-evaluation-v11", "full-evaluation-v12",
        "full-evaluation-v13", "full-evaluation-v14"}
    definitions = profile_definitions(shared.context) if shared else []
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for observation in sorted(
        observations, key=lambda item: (item.session_id, item.seq, item.repeat_index)
    ):
        session = sessions.get(observation.session_id)
        turn = next(
            (item for item in session.turns if item.turn_id == observation.turn_id),
            None,
        ) if session else None
        key = (observation.turn_id, observation.repeat_index)
        if key in seen:
            continue
        seen.add(key)
        allowed_refs = _allowed_refs(observation)
        tool_evidence = (_turn_tool_evidence(observation, execution_evidence)
                         if prompt.version_label in {
                             "full-evaluation-v12", "full-evaluation-v13", "full-evaluation-v14"}
                         else None)
        items.append(
            {
                "session_id": observation.session_id,
                "turn_id": observation.turn_id,
                "seq": observation.seq,
                "repeat_index": observation.repeat_index,
                "observation_id": observation.observation_id,
                "metric_ids": list(TURN_METRIC_ORDER),
                "turn_gate_metric_ids": list(TURN_GATE_METRIC_ORDER),
                "input": {
                    "context_policy": "teacher_forcing",
                    "golden": {
                        "messages": [
                            message.model_dump(mode="json")
                            for message in (turn.expected.messages if turn else [])
                        ],
                        "profile_update": {
                            key: value.value
                            for key, value in sorted(
                                (turn.expected.profile_update if turn else {}).items()
                            )
                        },
                        "profile_snapshot": (
                            turn.expected.profile_snapshot if turn else {}
                        ),
                        "tool_names": [
                            call.name
                            for call in (turn.expected.tool_calls if turn else [])
                        ],
                    },
                    "compiled_rubric": compile_rubric(turn),
                    "actual": {
                        "reply": [
                            message.model_dump(mode="json") if extended_evidence
                            else {"role": message.role, "content": message.content}
                            for message in observation.response_messages
                        ],
                        "profile_writes": ({} if observation.profile_readback.get("settled")
                                           is False else observation.profile_writes),
                        "profile_after": (None if observation.profile_readback.get("settled")
                                          is False else observation.profile_after),
                        "profile_readback": observation.profile_readback,
                        **({"profile_observation_ref": f"{observation.turn_id}#execution",
                            "profile_update_refs": {key: f"{observation.turn_id}#profile:{key}"
                                                    for key in observation.profile_writes}}
                           if prompt.version_label in {
                               "full-evaluation-v11", "full-evaluation-v12",
                               "full-evaluation-v13", "full-evaluation-v14"}
                           else {}),
                        "tool_names": (
                            observation.tool_diagnostics.actual_tool_names
                            if observation.tool_diagnostics
                            else []
                        ),
                        # 仅 v12 使用已冻结且确认属于本轮新调用的返回正文。
                        **({"tool_results": (tool_evidence["tool_results"] or None
                                             if tool_evidence is not None else None)}
                           if extended_evidence else {}),
                    },
                    "request_messages": [
                        message.model_dump(mode="json") if extended_evidence
                        else {"role": message.role, "content": message.content}
                        for message in observation.request_messages
                    ],
                    "execution": {
                        "status": observation.status.value,
                        "stage_reached": observation.stage_reached.value,
                        "routing_verified": observation.routing_verified,
                        "error_code": observation.error_code,
                        **({"judge_evidence": {
                            key: value for key, value in tool_evidence.items()
                            if key != "tool_results"}}
                           if tool_evidence is not None else {}),
                        **({"profile_completion_verified": (
                            observation.profile_readback.get("completion_verified") is True
                        )} if prompt.version_label in {
                               "full-evaluation-v11", "full-evaluation-v12",
                               "full-evaluation-v13", "full-evaluation-v14"}
                        else {}),
                        # EVAL 联系人无法重放真实客户的外部 MCP 返回，只比较可验证的决策与处理。
                        **({"tool_return_policy": (
                            "comparison_unverified"
                            if prompt.version_label in {
                                "full-evaluation-v13", "full-evaluation-v14"} else
                            "external_mcp_non_equivalent"
                            if observation.execution_target == "platform"
                            else "equivalent_fixture"
                        )} if extended_evidence else {}),
                    },
                    # 工单是 JSON 文档（要发给外部模型），所以这里必须是可序列化且稳定的列表。
                    "allowed_evidence_refs": sorted(allowed_refs),
                },
            }
        )
    all_items = items
    if part_count > 1:
        if part_index < 1 or part_index > part_count:
            raise JudgeResultError(
                f"part_index must be between 1 and part_count ({part_count})"
            )
        all_items = [
            item
            for index, item in enumerate(items)
            if index % part_count == part_index - 1
        ]
    if prompt.version_label in {
        "full-evaluation-v11", "full-evaluation-v12", "full-evaluation-v13", "full-evaluation-v14"}:
        for item in all_items:
            evidence = item["input"]
            golden = evidence["golden"]
            # 分类字段直接依据选项定义判断，避免原参考自由文本成为错误的唯一目标。
            enum_fields = {field["key"] for field in definitions if field.get("allowed_values")}
            golden["profile_required_fields"] = sorted(golden["profile_update"])
            golden["profile_update"] = {
                key: value for key, value in golden["profile_update"].items()
                if key not in enum_fields}
            golden["profile_snapshot"] = {key: value
                                          for key, value in golden["profile_snapshot"].items()
                                          if key not in enum_fields}
            for index, message in enumerate(evidence["request_messages"], start=1):
                message["evidence_ref"] = f"{item['turn_id']}#customer:{index}"
            for index, message in enumerate(evidence["actual"]["reply"], start=1):
                message["evidence_ref"] = f"{item['turn_id']}#actual:{index}"
    if (prompt.version_label == "full-evaluation-v14"
            and (shared is None or shared.context.reference_status != "confirmed")):
        for item in all_items:
            # 未确认参考不进入 Judge；业务依据来自独立生成并冻结的本次评分标准。
            item["input"]["golden"] = {"messages": [], "profile_update": {},
                "profile_snapshot": {}, "profile_required_fields": [], "tool_names": []}
    schema = _output_schema()
    if prompt.version_label in {
        "full-evaluation-v10", "full-evaluation-v11", "full-evaluation-v12",
        "full-evaluation-v13", "full-evaluation-v14"}:
        metric = schema["properties"]["items"]["items"]["properties"]["metrics"]["items"]
        for result_schema in (metric, metric["properties"]["checks"]["items"]):
            properties = result_schema["properties"]
            # 先写证据与最终理由，再落状态，避免模型先选状态后在理由中推翻自己。
            result_schema["properties"] = {
                **{key: value for key, value in properties.items()
                   if key not in {"reason", "status"}},
                "reason": properties["reason"], "status": properties["status"],
            }
            if prompt.version_label in {
                "full-evaluation-v11", "full-evaluation-v12",
                "full-evaluation-v13", "full-evaluation-v14"}:
                properties["evidence_refs"]["items"] = {"type": "string", "enum": sorted({
                    ref for item in all_items for ref in item["input"]["allowed_evidence_refs"]
                })}
    return {
        "run_id": run.run_id,
        "dataset_id": run.request.dataset_id or "",
        "context_policy": "teacher_forcing",
        "shared_context": {
            # 这个空间的画像字段字典：判指标 2/3 的「合理更新 / 同义表达 / 合法字段」时必须知道
            # 有哪些字段、各自是什么意思——只给 key（如 `demo_field_c`）模型只能猜。
            "profile_schema": definitions,
            **({"reference_status": shared.context.reference_status if shared else "unknown"}
               if prompt.version_label in {"full-evaluation-v13", "full-evaluation-v14"} else {}),
            # 空间指导、字段和技能共同确定业务约束；参考与 Rubric 不覆盖明确规则。
            "guidance": (shared.context.guidance or None) if shared else None,
            # 评估备注说明观测限制；不能覆盖空间禁令或把未发生的动作视为已完成。
            "evaluation_notes": (shared.context.evaluation_notes or None) if shared else None,
            **({"skill_rules": shared.context.skill_rules}
               if shared and shared.context.skill_rules else {}),
        },
        "repeats": run.request.repeats,
        "part_index": part_index,
        "part_count": part_count,
        "total_items": len(items),
        "prompt_kind": prompt.prompt_kind.value,
        "prompt_version": prompt.version_label,
        "prompt_sha256": prompt.content_sha256,
        "system_prompt": prompt.content,
        "output_schema": schema,
        "items": all_items,
    }


def _turn_tool_evidence(
    observation: TurnObservation, evidence_by_observation: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """消费采集层冻结的本轮证据；引用复用已有执行观测入口。"""

    evidence = (evidence_by_observation or {}).get(observation.observation_id)
    if not evidence:
        return None
    metadata = {key: evidence[key] for key in (
        "version", "status", "diagnostics", "diagnostic_counts", "trace", "truncated")
        if key in evidence}
    metadata["image_pixels_available"] = False
    trace = evidence.get("trace") or {}
    same_turn = (evidence.get("version") == "turn-evidence-v1"
                 and evidence.get("status") in {"available", "partial"}
                 and observation.replay_request_id
                 and trace.get("request_id") == observation.replay_request_id)
    tools = []
    if same_turn:
        for result in evidence.get("tool_results", []):
            if (result.get("association") == "same_turn_response_call_id"
                    and all(result.get(key) for key in (
                        "call_id", "name", "content", "span_id", "call_span_id"))):
                tools.append({**result, "evidence_ref": f"{observation.turn_id}#execution"})
    metadata["tool_results_count"] = len(tools)
    if not same_turn or len(tools) != len(evidence.get("tool_results", [])):
        metadata["status"] = "partial" if tools else "unavailable"
        metadata["diagnostics"] = [*metadata.get("diagnostics", []),
                                   "judge_worklist_unverified_tool_results_omitted"]
    return {**metadata, "tool_results": tools}


def validate_results(
    *,
    observations: list[TurnObservation],
    results: list[JudgeTurnResult],
    allow_partial: bool = False,
) -> list[JudgeTurnResult]:
    """Validate shape, coverage, metric set, evidence refs and ratio arithmetic.

    `allow_partial` accepts a slice of the batch (for judging in parts); the caller then merges it
    with what is already stored and re-checks coverage.
    """

    allowed: dict[tuple[str, int], set[str]] = {}
    for observation in observations:
        allowed[(observation.turn_id, observation.repeat_index)] = _allowed_refs(observation)

    seen: set[tuple[str, int]] = set()
    for item in results:
        key = (item.turn_id, item.repeat_index)
        if key in seen:
            raise JudgeResultError(
                f"duplicate judge result for {item.turn_id}#r{item.repeat_index}"
            )
        seen.add(key)
        if key not in allowed:
            raise JudgeResultError(
                f"judge result for unknown turn {item.turn_id}#r{item.repeat_index}"
            )
        metric_ids = [metric.metric_id for metric in item.metrics]
        if len(metric_ids) != len(set(metric_ids)):
            raise JudgeResultError(f"{item.turn_id}#r{item.repeat_index} repeats a metric id")
        if set(metric_ids) != set(TURN_METRIC_ORDER):
            missing = sorted(set(TURN_METRIC_ORDER) - set(metric_ids))
            extra = sorted(set(metric_ids) - set(TURN_METRIC_ORDER))
            raise JudgeResultError(
                f"{item.turn_id}#r{item.repeat_index} metric set mismatch; "
                f"missing={missing}, extra={extra}"
            )
        for metric in item.metrics:
            _validate_metric(metric, allowed[key], item.turn_id, item.repeat_index)

    missing = sorted(set(allowed) - seen)
    if missing and not allow_partial:
        preview = ", ".join(f"{turn}#r{repeat}" for turn, repeat in missing[:5])
        raise JudgeResultError(f"missing judge results for {len(missing)} turns: {preview}")
    return [_normalise_ratio_value(item) for item in results]


def _normalise_ratio_value(item: JudgeTurnResult) -> JudgeTurnResult:
    """ratio.value 由代码重算：模型只给 numerator/denominator，value 是冗余推导值。"""

    metrics = []
    for metric in item.metrics:
        if metric.metric_id == RATIO_METRIC_ID and metric.ratio:
            value = (
                metric.ratio.numerator / metric.ratio.denominator
                if metric.ratio.denominator
                else None
            )
            ratio = metric.ratio.model_copy(
                update={"value": value}
            )
            metric = metric.model_copy(update={"ratio": ratio})
        metrics.append(metric)
    return item.model_copy(update={"metrics": metrics})


def merge_results(
    stored: list[JudgeTurnResult], incoming: list[JudgeTurnResult]
) -> list[JudgeTurnResult]:
    """Merge a judged batch into what is already stored; later verdicts win."""

    merged: dict[tuple[str, int], JudgeTurnResult] = {
        (item.turn_id, item.repeat_index): item for item in stored
    }
    for item in incoming:
        merged[(item.turn_id, item.repeat_index)] = item
    return sorted(merged.values(), key=lambda item: (item.turn_id, item.repeat_index))


def apply_overrides(
    results: list[JudgeTurnResult], revisions: list[ReviewRevision]
) -> list[JudgeTurnResult]:
    """Fold human review revisions into the verdicts before aggregation (later revisions win)."""

    if not revisions:
        return results
    latest: dict[tuple[str, int, str], ReviewOverride] = {}
    for revision in sorted(revisions, key=lambda item: item.revision_index):
        for override in revision.overrides:
            latest[(override.turn_id, override.repeat_index, override.metric_id)] = override
    if not latest:
        return results

    output: list[JudgeTurnResult] = []
    for item in results:
        metrics = []
        for metric in item.metrics:
            override = latest.get((item.turn_id, item.repeat_index, metric.metric_id))
            if override is None:
                metrics.append(metric)
                continue
            metrics.append(
                metric.model_copy(
                    update={
                        "status": override.status,
                        "reason": f"[人工复核] {override.reason}",
                        "evidence_refs": override.evidence_refs or metric.evidence_refs,
                        "diagnostic_code": "review_override",
                        "overridden": True,
                    }
                )
            )
        output.append(item.model_copy(update={"metrics": metrics}))
    return output


def coverage(
    observations: list[TurnObservation], results: list[JudgeTurnResult]
) -> dict[str, Any]:
    """How much of the batch has verdicts — drives the resume UI."""

    expected = {(item.turn_id, item.repeat_index) for item in observations}
    judged = {(item.turn_id, item.repeat_index) for item in results}
    missing = sorted(expected - judged)
    return {
        "expected": len(expected),
        "judged": len(expected & judged),
        "missing": len(missing),
        "missing_keys": [f"{turn}#r{repeat}" for turn, repeat in missing[:20]],
        "complete": not missing,
    }


def with_display_indexes(report: JudgeReport) -> JudgeReport:
    """给报告补上维度顺序编号。

    报告是可重建投影：早先存下来的报告还没有 `display_index`，读取时补齐即可，不触碰任何判定、
    计数或聚合口径。
    """

    summaries = [
        summary.model_copy(update={"display_index": metric_display_index(summary.metric_id)})
        for summary in report.metric_summaries
    ]
    unchanged = all(
        old.display_index == new.display_index
        for old, new in zip(report.metric_summaries, summaries, strict=True)
    )
    if unchanged:
        return report
    return report.model_copy(update={"metric_summaries": summaries})


def build_report(
    *,
    run: RunSummary,
    sessions: dict[str, GoldenSession],
    observations: list[TurnObservation],
    results: list[JudgeTurnResult],
    prompt: PromptVersion,
    revisions: list[ReviewRevision] | None = None,
) -> JudgeReport:
    """Aggregate validated results into the report the UI renders."""

    revisions = revisions or []
    results = apply_overrides(results, revisions)
    by_key = {(item.turn_id, item.repeat_index): item for item in results}
    session_ids = sorted({item.session_id for item in observations})
    repeats = run.request.repeats
    metric_counts: dict[str, dict[str, int]] = {
        metric_id: {"pass": 0, "fail": 0, "unavailable": 0}
        for metric_id in TURN_METRIC_ORDER
    }
    ratios: dict[str, list[float]] = {RATIO_METRIC_ID: []}
    missing_turns: list[tuple[str, int, str]] = []
    first_failures: dict[str, int] = {}
    session_reports: list[JudgeSessionReport] = []
    warnings: list[str] = []
    judged_count = 0

    for session_id in session_ids:
        attempts: list[JudgeAttemptReport] = []
        passed_attempts = 0
        has_unknown_attempt = False
        for repeat_index in range(1, repeats + 1):
            turns: list[JudgeTurnReport] = []
            session_observations = [
                item
                for item in observations
                if item.session_id == session_id and item.repeat_index == repeat_index
            ]
            for observation in sorted(session_observations, key=lambda item: item.seq):
                result = by_key.get((observation.turn_id, repeat_index))
                if result is None:
                    missing_turns.append(
                        (session_id, repeat_index, observation.turn_id)
                    )
                    turns.append(
                        JudgeTurnReport(
                            turn_id=observation.turn_id,
                            seq=observation.seq,
                            repeat_index=repeat_index,
                            status="missing_judgement",
                            metrics=[],
                            turn_status="unavailable",
                            turn_pass=None,
                            tool_diagnostics=observation.tool_diagnostics,
                        )
                    )
                    continue
                judged_count += 1
                metrics = _ordered_metrics(result.metrics)
                for metric in metrics:
                    metric_counts[metric.metric_id][metric.status] += 1
                    if metric.metric_id == RATIO_METRIC_ID and metric.ratio:
                        if metric.ratio.value is not None:
                            ratios[RATIO_METRIC_ID].append(metric.ratio.value)
                gate = {metric.metric_id: metric.status for metric in metrics}
                gate_statuses = [gate[metric_id] for metric_id in TURN_GATE_METRIC_ORDER]
                if "fail" in gate_statuses:
                    turn_status = "fail"
                    turn_pass: bool | None = False
                elif "unavailable" in gate_statuses:
                    turn_status = "unavailable"
                    turn_pass = None
                else:
                    turn_status = "pass"
                    turn_pass = True
                turns.append(
                    JudgeTurnReport(
                        turn_id=observation.turn_id,
                        seq=observation.seq,
                        repeat_index=repeat_index,
                        status="judged",
                        metrics=metrics,
                        turn_status=turn_status,
                        turn_pass=turn_pass,
                        failed_metric_ids=[
                            metric.metric_id
                            for metric in metrics
                            if metric.status == "fail"
                        ],
                        tool_diagnostics=observation.tool_diagnostics,
                    )
                )
            statuses = [turn.turn_status for turn in turns]
            if not turns:
                attempt_status: Literal["pass", "fail", "unavailable"] = "unavailable"
                session_pass: bool | None = None
            elif "fail" in statuses:
                attempt_status = "fail"
                session_pass = False
            elif "unavailable" in statuses:
                attempt_status = "unavailable"
                session_pass = None
            else:
                attempt_status = "pass"
                session_pass = True
            if session_pass is None:
                has_unknown_attempt = True
            if session_pass:
                passed_attempts += 1
            first_failure = next(
                (turn.seq for turn in turns if turn.turn_status == "fail"), None
            )
            if first_failure is not None:
                first_failures[str(first_failure)] = (
                    first_failures.get(str(first_failure), 0) + 1
                )
            attempts.append(
                JudgeAttemptReport(
                    repeat_index=repeat_index,
                    turns=turns,
                    session_status=attempt_status,
                    session_pass=session_pass,
                    first_failure_turn=first_failure,
                )
            )
        pass_at_k = _pass_at_k(passed_attempts, repeats, has_unknown_attempt)
        if has_unknown_attempt:
            warnings.append(
                f"{session_id} 有 repeat 未取得有效判定，pass^k 记为 null；需补跑或补齐打分"
            )
        session_reports.append(
            JudgeSessionReport(
                session_id=session_id,
                attempts=attempts,
                passed_attempt_count=passed_attempts,
                pass_at_k=pass_at_k,
            )
        )

    if missing_turns:
        warnings.append(f"{len(missing_turns)} 个 Turn 没有打分结果，按 unavailable 处理")
    batch_pass_at_k = _batch_pass_at_k(session_reports, repeats)
    return JudgeReport(
        run_id=run.run_id,
        dataset_id=run.request.dataset_id or "",
        judge_contract_version=JUDGE_CONTRACT_VERSION,
        judge_prompt_version=prompt.version_label,
        judge_prompt_sha256=prompt.content_sha256,
        scoring_policy=SCORING_POLICY_VERSION,
        repeats=repeats,
        judged_turn_count=judged_count,
        missing_turn_count=len(missing_turns),
        metric_summaries=_metric_summaries(metric_counts, ratios),
        sessions=session_reports,
        first_failure_turn_distribution=first_failures,
        batch_pass_at_k=batch_pass_at_k,
        quality_warnings=warnings,
        review_revision_count=len(revisions),
        reviewed_metric_count=sum(len(revision.overrides) for revision in revisions),
        updated_at=datetime.now(UTC),
    )


def compile_rubric(turn) -> dict[str, Any]:
    """Compile the v0.4 fixed-heading rubric into the judge input shape."""

    if turn is None:
        return {"must_cover": [], "forbidden": [], "guidance": {"should_lead": ""}}
    sections: dict[str, str] = {}
    current: str | None = None
    for line in turn.rubric.splitlines():
        if line.startswith("【") and "】" in line:
            current, _, rest = line[1:].partition("】")
            sections[current] = rest.strip()
        elif current:
            sections[current] = f"{sections[current]}\n{line}".strip()
    return {
        "must_cover": _split_section(sections.get("必须覆盖", "")),
        "forbidden": _split_section(sections.get("禁止", "")),
        "guidance": {"should_lead": sections.get("主导动作", "")},
        "tools": sections.get("工具", ""),
        "knowledge": sections.get("知识召回", ""),
        "profile": sections.get("画像", ""),
        "note": sections.get("标注说明", ""),
    }


def _split_section(value: str) -> list[str]:
    text = value.strip().rstrip("。")
    if not text:
        return []
    parts = [item.strip(" 。") for item in text.split("；")]
    return [item for item in parts if item]


def _ordered_metrics(metrics: list[JudgeMetricResult]) -> list[JudgeMetricResult]:
    order = {metric_id: index for index, metric_id in enumerate(TURN_METRIC_ORDER)}
    return sorted(metrics, key=lambda metric: order.get(metric.metric_id, len(order)))


def validate_check_contract(
    result: JudgeTurnResult, must_cover: list[str], allowed_refs: set[str],
) -> None:
    """v12 校验评分工单覆盖与证据合同，不替模型判断业务是否达标。"""

    for metric in result.metrics:
        for check in metric.checks:
            refs = [ref.strip() for ref in check.evidence_refs if ref.strip()]
            if check.status in {"pass", "fail"} and not refs:
                raise JudgeResultError("check_evidence_missing: pass/fail checks need direct refs")
            if set(refs) - allowed_refs:
                raise JudgeResultError("check_evidence_unknown: use allowed_evidence_refs")
        if metric.metric_id != RATIO_METRIC_ID:
            continue
        actual = Counter(check.check_id for check in metric.checks)
        expected_ids = Counter(must_cover)
        if actual != expected_ids:
            missing = list((expected_ids - actual).elements())
            unexpected = list((actual - expected_ids).elements())
            raise JudgeResultError(
                f"check_coverage_mismatch: expected {len(must_cover)} exact IDs; "
                f"missing={missing[:1]!r}; unexpected={unexpected[:1]!r}. "
                "Copy check_id verbatim from compiled_rubric.must_cover; do not paraphrase.")
        statuses = {check.status for check in metric.checks}
        expected = ("unavailable" if not must_cover else
                    "fail" if "fail" in statuses else
                    "unavailable" if "unavailable" in statuses else "pass")
        if metric.status != expected:
            raise JudgeResultError(
                f"check_status_conflict: metric 5 must be {expected} from its checks")


def _validate_metric(
    metric: JudgeMetricResult,
    allowed_refs: set[str],
    turn_id: str,
    repeat_index: int,
) -> None:
    label = f"{turn_id}#r{repeat_index} {metric.metric_id}"
    refs = [ref.strip() for ref in metric.evidence_refs if ref.strip()]
    if metric.status in {"pass", "fail"} and not refs:
        raise JudgeResultError(f"{label} needs at least one evidence ref")
    unknown = [ref for ref in refs if ref not in allowed_refs]
    if unknown:
        raise JudgeResultError(f"{label} cites unknown evidence refs: {unknown[:3]}")
    if metric.metric_id == RATIO_METRIC_ID:
        if metric.ratio is None:
            raise JudgeResultError(f"{label} must report a coverage ratio")
        ratio = metric.ratio
        if ratio.denominator < ratio.numerator:
            raise JudgeResultError(f"{label} ratio numerator exceeds denominator")
        if metric.checks:
            covered = sum(1 for check in metric.checks if check.status == "pass")
            if ratio.numerator != covered or ratio.denominator != len(metric.checks):
                raise JudgeResultError(
                    f"{label} ratio must equal the checks it reports "
                    f"({covered}/{len(metric.checks)})"
                )
        # value 完全由代码重算（见 _normalise_ratio_value），模型给不给、给多少都不影响校验。
    elif metric.ratio is not None:
        raise JudgeResultError(f"{label} must not report a ratio")


def _allowed_refs(observation: TurnObservation) -> set[str]:
    refs = {
        f"{observation.turn_id}#customer:{index}"
        for index, _ in enumerate(observation.request_messages, start=1)
    }
    refs |= {
        f"{observation.turn_id}#actual:{index}"
        for index, _ in enumerate(observation.response_messages, start=1)
    }
    refs |= {f"{observation.turn_id}#execution" for _ in [observation]}
    refs |= {
        f"{observation.turn_id}#profile:{key}" for key in observation.profile_writes
    }
    refs |= {f"{observation.turn_id}#routing" for _ in [observation]}
    return refs


def _pass_at_k(passed: int, repeats: int, unknown: bool) -> dict[str, float | None]:
    if unknown:
        return {str(index): None for index in range(1, repeats + 1)}
    output: dict[str, float | None] = {}
    for index in range(1, repeats + 1):
        if passed < index:
            output[str(index)] = 0.0
            continue
        output[str(index)] = math.comb(passed, index) / math.comb(repeats, index)
    return output


def _batch_pass_at_k(
    sessions: list[JudgeSessionReport], repeats: int
) -> dict[str, float | None]:
    output: dict[str, float | None] = {}
    for index in range(1, repeats + 1):
        values = [
            session.pass_at_k[str(index)]
            for session in sessions
            if session.pass_at_k.get(str(index)) is not None
        ]
        output[str(index)] = sum(values) / len(values) if values else None
    return output


def _metric_summaries(
    counts: dict[str, dict[str, int]], ratios: dict[str, list[float]]
) -> list[JudgeMetricSummary]:
    summaries: list[JudgeMetricSummary] = []
    catalog = {metric.metric_id: metric for metric in METRIC_CATALOG}
    for metric_id in TURN_METRIC_ORDER:
        bucket = counts[metric_id]
        denominator = bucket["pass"] + bucket["fail"]
        ratio_values = ratios.get(metric_id, [])
        definition = catalog[metric_id]
        summaries.append(
            JudgeMetricSummary(
                metric_id=metric_id,
                name=definition.name,
                display_index=metric_display_index(metric_id),
                mode=definition.mode.value,
                pass_count=bucket["pass"],
                fail_count=bucket["fail"],
                unavailable_count=bucket["unavailable"],
                denominator=denominator,
                pass_rate=(bucket["pass"] / denominator) if denominator else None,
                mean_ratio=(
                    sum(ratio_values) / len(ratio_values) if ratio_values else None
                ),
            )
        )
    return summaries


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["turn_id", "repeat_index", "metrics"],
                    "properties": {
                        "turn_id": {"type": "string"},
                        "repeat_index": {"type": "integer"},
                        "metrics": {
                            "type": "array",
                            "required_metric_ids": list(TURN_METRIC_ORDER),
                            "items": {
                                "type": "object",
                                "required": ["metric_id", "status", "reason", "evidence_refs"],
                                "properties": {
                                    "metric_id": {"enum": list(TURN_METRIC_ORDER)},
                                    "status": {"enum": ["pass", "fail", "unavailable"]},
                                    "reason": {"type": "string"},
                                    "evidence_refs": {"type": "array"},
                                    "checks": {
                                        "type": "array",
                                        "description": (
                                            "逐检查点结果；每个元素形如 "
                                            '{"check_id": "<must_cover 原文>", "status": '
                                            '"pass|fail|unavailable", "reason": "…"}'
                                        ),
                                        "items": {
                                            "type": "object",
                                            "required": ["check_id", "status", "reason"],
                                            "properties": {
                                                "check_id": {"type": "string"},
                                                "status": {
                                                    "enum": ["pass", "fail", "unavailable"]
                                                },
                                                "reason": {"type": "string"},
                                                "evidence_refs": {"type": "array"},
                                            },
                                        },
                                    },
                                    "ratio": {"type": ["object", "null"]},
                                    "diagnostic_code": {"type": ["string", "null"]},
                                },
                            },
                        },
                    },
                },
            }
        },
        "notes": {
            "aggregation": "门控、session 状态、指标汇总与 pass^k 全部由代码重算，模型只给状态。",
            "ratio_metric": RATIO_METRIC_ID,
        },
    }
