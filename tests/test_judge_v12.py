import pytest
from test_jsonl_corpus import shared_payload
from test_judge_model import _evaluate_run, _judge_prompt, _result_payload
from test_judge_report import golden_session, observation

from golden_eval.application.judge_model import judge_batch
from golden_eval.application.judge_report import compile_rubric
from golden_eval.domain.models import GoldenShared
from golden_eval.integrations.structured_model import FixtureStructuredModel


def evaluate(tmp_path, *, observed=None, mutate=None, version="full-evaluation-v13", rubric=None,
             reference_status="confirmed"):
    session = golden_session()
    session.turns[0].rubric = (rubric if rubric is not None else
                             "【必须覆盖】确认城市；说明下一步。\n【禁止】不得编造。")
    payload = _result_payload()
    checks = compile_rubric(session.turns[0])["must_cover"]
    metric = payload["metrics"][3]
    metric["checks"] = [{"check_id": check, "status": "pass", "reason": "本轮满足。",
                         "evidence_refs": [payload["metrics"][0]["evidence_refs"][0]]}
                        for check in checks]
    metric["ratio"] = {"numerator": len(checks), "denominator": len(checks)}
    if mutate:
        mutate(payload)
    raw = shared_payload()
    raw["context"]["reference_status"] = reference_status
    return judge_batch(run=_evaluate_run(), sessions={session.session_id: session},
        shared=GoldenShared.model_validate(raw),
        pending=[observed or observation()],
        prompt=_judge_prompt(tmp_path).model_copy(update={"version_label": version}),
        model=FixtureStructuredModel([{"items": [payload]}]))


@pytest.mark.parametrize("invalid", [False, True])
def test_partial_writes_do_not_hide_missing_required_field(tmp_path, invalid):
    observed = observation().model_copy(update={
        "profile_writes": {"budget": {"value": "100"}},
        "profile_after": {"city": "", "budget": "100"},
        "profile_observed_before": {"city": "", "budget": ""},
        "profile_readback": {"settled": True, "completion_verified": False},
    })

    def mutate(payload):
        if invalid:
            for metric in payload["metrics"][:2]:
                metric.update(status="fail", diagnostic_code="profile_invalid_update",
                              reason="已观察到无依据的预算写入。")

    outcome = evaluate(tmp_path, observed=observed, mutate=mutate)
    assert not outcome.errors
    assert [metric.status for metric in outcome.judged[0].metrics[:2]] == (
        ["fail", "fail"] if invalid else ["unavailable", "unavailable"])


@pytest.mark.parametrize("case", ["unknown_ref", "empty_ref", "invented", "omitted", "duplicate",
                                  "fail_as_unknown", "unknown_as_fail"])
def test_v12_rejects_inconsistent_coverage_contract(tmp_path, case):
    def mutate(payload):
        metric = payload["metrics"][3]
        if case == "unknown_ref":
            metric["checks"][0]["evidence_refs"] = ["unknown"]
        elif case == "empty_ref":
            metric["checks"][0]["evidence_refs"] = []
        elif case == "invented":
            metric["checks"][0]["check_id"] = "not a rubric check"
        elif case == "omitted":
            metric["checks"] = []
            metric["ratio"] = {"numerator": 0, "denominator": 0}
        elif case == "duplicate":
            metric["checks"].append(dict(metric["checks"][0]))
            metric["ratio"] = {"numerator": len(metric["checks"]),
                               "denominator": len(metric["checks"])}
        else:
            metric["checks"][0]["status"] = "fail" if case == "fail_as_unknown" else "unavailable"
            metric["status"] = "unavailable" if case == "fail_as_unknown" else "fail"
            metric["reason"] = "待核对该业务条件。"
            metric["ratio"]["numerator"] -= 1

    outcome = evaluate(tmp_path, mutate=mutate)
    assert not outcome.judged
    assert outcome.errors[0]["code"] == "model_item_contract_violation"


def test_v12_accepts_all_checks_with_direct_evidence(tmp_path):
    outcome = evaluate(tmp_path)
    assert not outcome.errors
    assert outcome.judged[0].metrics[3].status == "pass"


@pytest.mark.parametrize("status", ["fail", "unavailable"])
def test_v12_preserves_business_failure_and_unknown_checks(tmp_path, status):
    def mutate(payload):
        metric = payload["metrics"][3]
        metric["checks"][0].update(status=status, reason="报价资料未齐，需先核对摆放条件。")
        metric.update(status=status, reason="依据本轮适用条件逐项判断。")
        metric["ratio"]["numerator"] -= 1

    outcome = evaluate(tmp_path, mutate=mutate)
    assert not outcome.errors
    assert outcome.judged[0].metrics[3].status == status


def test_v12_accepts_staged_action_without_demanding_reference_words(tmp_path):
    def mutate(payload):
        payload["metrics"][3]["checks"][0]["reason"] = (
            "报价条件尚缺：本轮明确先问摆放以核对服务费用，不要求当轮最终总价。")

    outcome = evaluate(tmp_path, mutate=mutate)
    assert not outcome.errors
    assert outcome.judged[0].metrics[3].status == "pass"


def test_legacy_v11_keeps_its_checks_contract(tmp_path):
    outcome = evaluate(tmp_path, version="full-evaluation-v11",
        mutate=lambda payload: payload["metrics"][3].update(checks=[]))
    assert not outcome.errors


@pytest.mark.parametrize("status", ["pass", "fail", "unavailable"])
def test_empty_rubric_cannot_prove_completeness(tmp_path, status):
    outcome = evaluate(tmp_path, rubric="【必须覆盖】\n【禁止】不得编造。",
        mutate=lambda payload: payload["metrics"][3].update(status=status))
    if status == "unavailable":
        assert not outcome.errors
        metric = outcome.judged[0].metrics[3]
        assert metric.ratio.numerator == metric.ratio.denominator == 0
        assert metric.ratio.value is None
    else:
        assert not outcome.judged
        assert outcome.errors[0]["code"] == "model_item_contract_violation"


def current_turn_evidence():
    return {"version": "turn-evidence-v1", "status": "partial", "diagnostics": [],
            "trace": {"request_id": "request-current", "trace_id": "trace-current",
                      "content_sha256": "body-sha", "call_count_status": "unknown"},
            "tool_results": [{"evidence_ref": "session#T1#trace:span2:tool:sha",
                "call_id": "fresh", "name": "knowledge", "content": "胶圈含税价格199",
                "span_id": "span2", "call_span_id": "span1", "truncated": False,
                "association": "same_turn_response_call_id"}],
            "image_pixels_available": False, "truncated": False}


@pytest.mark.parametrize("version", [
    "full-evaluation-v11", "full-evaluation-v12", "full-evaluation-v13"])
@pytest.mark.parametrize("identity", [
    "current", "other_observation", "other_request", "historical"])
def test_only_v12_receives_verified_current_turn_tool_results(tmp_path, version, identity):
    from copy import deepcopy

    from golden_eval.application.judge_report import build_worklist

    observed = observation().model_copy(update={"replay_request_id": "request-current"})
    frozen = current_turn_evidence()
    if identity == "other_request":
        frozen["trace"]["request_id"] = "request-other"
    if identity == "historical":
        frozen["tool_results"][0]["association"] = "historical_context"
    original = deepcopy(frozen)
    mapping = {"other" if identity == "other_observation" else observed.observation_id: frozen}
    session = golden_session()
    worklist = build_worklist(run=_evaluate_run(), sessions={session.session_id: session},
        observations=[observed], prompt=_judge_prompt(tmp_path).model_copy(
            update={"version_label": version}), execution_evidence=mapping)
    item = worklist["items"][0]["input"]
    if version in {"full-evaluation-v12", "full-evaluation-v13"} and identity == "current":
        tools = item["actual"]["tool_results"]
        assert tools[0]["content"] == "胶圈含税价格199"
        assert tools[0]["evidence_ref"] == f"{observed.turn_id}#execution"
        assert tools[0]["evidence_ref"] in item["allowed_evidence_refs"]
        assert tools[0]["span_id"] == "span2"
        metadata = item["execution"]["judge_evidence"]
        assert "tool_results" not in metadata
        assert metadata["trace"]["call_count_status"] == "unknown"
        assert metadata["image_pixels_available"] is False
    else:
        assert not item["actual"]["tool_results"]
    assert frozen == original


def test_judge_batch_threads_evidence_and_accepts_existing_execution_ref(tmp_path):
    observed = observation().model_copy(update={"replay_request_id": "request-current"})
    payload = _result_payload(ref=f"{observed.turn_id}#execution")
    payload["metrics"][3].update(status="unavailable", checks=[],
                               ratio={"numerator": 0, "denominator": 0})
    model = FixtureStructuredModel([{"items": [payload]}])
    session = golden_session()
    outcome = judge_batch(run=_evaluate_run(), sessions={session.session_id: session},
        pending=[observed], prompt=_judge_prompt(tmp_path).model_copy(
            update={"version_label": "full-evaluation-v12"}), model=model,
        execution_evidence={observed.observation_id: current_turn_evidence()})
    assert not outcome.errors
    assert outcome.judged[0].metrics[3].status == "unavailable"


@pytest.mark.parametrize("reference_status", ["generated", "unknown", "confirmed"])
def test_unconfirmed_reference_cannot_create_profile_obligation(tmp_path, reference_status):
    # 模型已依据字段/技能判断本轮无需提取；缺少未确认参考字段不能在代码层制造义务。
    observed = observation().model_copy(update={
        "profile_writes": {}, "profile_after": {"city": ""},
        "profile_readback": {"settled": True, "completion_verified": False},
    })
    outcome = evaluate(tmp_path, observed=observed, reference_status=reference_status)
    assert not outcome.errors
    expected = "unavailable" if reference_status == "confirmed" else "pass"
    assert [m.status for m in outcome.judged[0].metrics[:2]] == [expected, expected]


def test_coverage_feedback_identifies_the_exact_missing_and_edited_id(tmp_path):
    def edit_id(payload):
        payload["metrics"][3]["checks"][1]["check_id"] = "说明下一步和详情"
    outcome = evaluate(tmp_path, mutate=edit_id)
    detail = outcome.errors[0]["detail"]
    assert "说明下一步" in detail and "说明下一步和详情" in detail
