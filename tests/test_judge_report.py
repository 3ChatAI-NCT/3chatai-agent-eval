from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_jsonl_corpus import session_payload

from golden_eval.adapters.sqlite_prompts import SqlitePromptStore
from golden_eval.application.judge_report import (
    JudgeResultError,
    build_report,
    compile_rubric,
    validate_results,
)
from golden_eval.application.prompt_registry import PromptRegistry
from golden_eval.domain.models import (
    ConversationMessage,
    CreateRunRequest,
    GoldenSession,
    JudgeMetricResult,
    JudgeRatio,
    JudgeTurnResult,
    PromptKind,
    TurnObservation,
)

PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"


def prompt_registry(tmp_path) -> PromptRegistry:
    return PromptRegistry(SqlitePromptStore(tmp_path / "prompts.sqlite"), prompts_root=PROMPTS_ROOT)


def golden_session() -> GoldenSession:
    return GoldenSession.model_validate(session_payload())


def observation(
    *, run_id: str = "run-1", repeat_index: int = 1
) -> TurnObservation:
    return TurnObservation(
        observation_id=f"obs_{repeat_index}",
        run_id=run_id,
        session_id="GD-FIXTURE-0001",
        turn_id="GD-FIXTURE-0001#T1",
        seq=1,
        repeat_index=repeat_index,
        status="completed",
        stage_reached="profile_settled",
        account_id="acct-1",
        execution_target="fixture",
        request_messages=[ConversationMessage(role="user", content="I am in Chengdu.")],
        response_messages=[ConversationMessage(role="assistant", content="Understood.")],
        profile_writes={"city": {"value": "Chengdu"}},
        routing_verified=True,
        tool_diagnostics=None,
    )


def metric(metric_id: str, status: str = "pass", ref: str = "GD-FIXTURE-0001#T1#actual:1"):
    kwargs: dict = {
        "metric_id": metric_id,
        "status": status,
        "reason": f"{metric_id} {status}",
        "evidence_refs": [ref] if status in {"pass", "fail"} else [],
    }
    if metric_id == "5":
        passed = status == "pass"
        kwargs["ratio"] = JudgeRatio(
            numerator=1 if passed else 0,
            denominator=1,
            value=1.0 if passed else 0.0,
        )
    return JudgeMetricResult(**kwargs)


def turn_result(repeat_index: int = 1, failures: set[str] | None = None) -> JudgeTurnResult:
    failures = failures or set()
    return JudgeTurnResult(
        turn_id="GD-FIXTURE-0001#T1",
        repeat_index=repeat_index,
        metrics=[
            metric(metric_id, "fail" if metric_id in failures else "pass")
            for metric_id in ("2", "3", "4", "5", "6", "8", "9")
        ],
    )


def run_summary(repeats: int = 2):
    return CreateRunRequest(
        kind="evaluate",
        dataset_id="dv_test",
        target_connection_id="target-main",
        repeats=repeats,
    )






def test_compile_rubric_reads_fixed_headings() -> None:
    session = golden_session()
    turn = session.turns[0].model_copy(
        update={
            "rubric": (
                "【必须覆盖】确认城市；给出下一步。\n"
                "【禁止】不得编造门店。\n"
                "【主导动作】请客户补充门店名称\n"
                "【工具】本轮无可观察执行动作；多余调用记错。\n"
                "【画像】本轮应更新：city；其余画像字段保持上一轮值。"
            )
        }
    )

    compiled = compile_rubric(turn)

    assert compiled["must_cover"] == ["确认城市", "给出下一步"]
    assert compiled["forbidden"] == ["不得编造门店"]
    assert compiled["guidance"]["should_lead"] == "请客户补充门店名称"
    assert compiled["profile"].startswith("本轮应更新：city")


def test_results_validation_rejects_shape_and_evidence_errors(tmp_path) -> None:
    observations = [observation()]

    ok = validate_results(observations=observations, results=[turn_result()])
    assert len(ok) == 1

    with pytest.raises(JudgeResultError, match="metric set mismatch"):
        validate_results(
            observations=observations,
            results=[
                JudgeTurnResult(
                    turn_id="GD-FIXTURE-0001#T1",
                    repeat_index=1,
                    metrics=[metric("4")],
                )
            ],
        )

    with pytest.raises(JudgeResultError, match="needs at least one evidence ref"):
        validate_results(
            observations=observations,
            results=[
                JudgeTurnResult(
                    turn_id="GD-FIXTURE-0001#T1",
                    repeat_index=1,
                    metrics=[
                        metric(metric_id, status="pass", ref="")
                        if metric_id != "5"
                        else JudgeMetricResult(
                            metric_id="5",
                            status="pass",
                            reason="ok",
                            evidence_refs=[],
                            ratio=JudgeRatio(numerator=1, denominator=1, value=1.0),
                        )
                        for metric_id in ("2", "3", "4", "5", "6", "8", "9")
                    ],
                )
            ],
        )

    with pytest.raises(JudgeResultError, match="unknown evidence refs"):
        validate_results(
            observations=observations,
            results=[
                JudgeTurnResult(
                    turn_id="GD-FIXTURE-0001#T1",
                    repeat_index=1,
                    metrics=[
                        metric(metric_id, ref="nope#T1#actual:9")
                        for metric_id in ("2", "3", "4", "5", "6", "8", "9")
                    ],
                )
            ],
        )


def test_metric_checks_accept_the_model_key_name() -> None:
    """Judge output may name the check key `check` or `check_id`, and may carry evidence."""

    item = JudgeTurnResult.model_validate(
        {
            "turn_id": "GD-FIXTURE-0001#T1",
            "repeat_index": 1,
            "metrics": [
                {
                    "metric_id": "5",
                    "status": "fail",
                    "reason": "must_cover 未覆盖",
                    "evidence_refs": ["GD-FIXTURE-0001#T1#actual:1"],
                    "checks": [
                        {
                            "check": "确认客户所在城市",
                            "status": "fail",
                            "reason": "实际回复未确认城市",
                            "evidence_refs": ["GD-FIXTURE-0001#T1#actual:1"],
                        }
                    ],
                    "ratio": {"numerator": 0, "denominator": 1, "value": 0.0},
                }
            ],
        }
    )

    check = item.metrics[0].checks[0]
    assert check.check_id == "确认客户所在城市"
    assert check.evidence_refs == ["GD-FIXTURE-0001#T1#actual:1"]










def two_turn_payload() -> dict:
    payload = session_payload()
    second = json.loads(json.dumps(payload["turns"][0]))
    second["turn_id"] = "GD-FIXTURE-0001#T2"
    second["seq"] = 2
    second["input"]["customer_messages"] = [
        {"seq": 1, "type": "text", "content": "Thanks."}
    ]
    second["expected"]["messages"] = [
        {"seq": 1, "type": "text", "content": "Happy to help."}
    ]
    second["expected"]["profile_update"] = {}
    second["expected"]["profile_snapshot"] = {"city": "Chengdu"}
    payload["turns"] = [payload["turns"][0], second]
    return payload




def golden_run(repeats: int):
    """和生产 Worker 一样，用 engines.golden_run 从任务输入构造运行摘要。"""

    from agent_eval.engines import golden_run as build

    return build({"experiment_id": "exp_test", "input": {"config": {
        "dataset_version_id": "dv_test", "execution_target": "platform", "repeats": repeats,
        "space": {"id": "rec_test", "source_workspace_id": "source-main",
                  "target_workspace_id": "target-main"}}}})


def test_report_aggregates_gate_session_and_pass_at_k(tmp_path) -> None:
    registry = prompt_registry(tmp_path)
    session = golden_session()
    observations = [observation(repeat_index=1), observation(repeat_index=2)]
    results = validate_results(
        observations=observations,
        results=[turn_result(1), turn_result(2, failures={"5"})],
    )

    report = build_report(
        run=golden_run(repeats=2),
        sessions={session.session_id: session},
        observations=observations,
        results=results,
        prompt=registry.active(PromptKind.JUDGE),
    )

    metrics = {item.metric_id: item for item in report.metric_summaries}
    assert metrics["5"].fail_count == 1
    assert metrics["5"].pass_count == 1
    assert metrics["5"].pass_rate == 0.5
    assert metrics["5"].mean_ratio == 0.5
    assert metrics["8"].mode == "scored"

    attempt_one, attempt_two = report.sessions[0].attempts
    assert attempt_one.session_pass is True
    assert attempt_two.session_status == "fail"
    assert attempt_two.first_failure_turn == 1
    assert report.sessions[0].passed_attempt_count == 1
    # 两次里通过一次：pass^1 = 1/2，pass^2 = 0
    assert report.sessions[0].pass_at_k["1"] == pytest.approx(0.5)
    assert report.sessions[0].pass_at_k["2"] == 0.0
    assert report.batch_pass_at_k["1"] == pytest.approx(0.5)
    assert report.first_failure_turn_distribution == {"1": 1}


def test_report_marks_missing_judgements_as_unavailable(tmp_path) -> None:
    registry = prompt_registry(tmp_path)
    session = golden_session()
    observations = [observation(), observation()]
    observations[1] = observations[1].model_copy(
        update={"observation_id": "obs_other", "turn_id": "GD-FIXTURE-0001#T2", "seq": 2}
    )

    report = build_report(
        run=golden_run(repeats=1),
        sessions={session.session_id: session},
        observations=observations,
        results=[turn_result(1)],
        prompt=registry.active(PromptKind.JUDGE),
    )

    assert report.missing_turn_count == 1
    assert report.sessions[0].attempts[0].session_pass is None
    assert report.sessions[0].pass_at_k["1"] is None
    assert report.batch_pass_at_k["1"] is None
    assert any("没有打分结果" in warning for warning in report.quality_warnings)
