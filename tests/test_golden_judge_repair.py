import copy

import pytest
from test_judge_model import _result_payload
from test_judge_report import golden_session, observation

from agent_eval import engines
from agent_eval.config import Settings
from golden_eval.application.judge_model import _validate_consistency
from golden_eval.application.judge_report import JudgeResultError
from golden_eval.domain.models import JudgeTurnResult
from golden_eval.integrations.structured_model import StructuredModelCall


@pytest.mark.parametrize("repairable", [True, False])
@pytest.mark.parametrize("violation", ["unknown_evidence", "contradictory_reason", "draft_reason"])
def test_golden_judge_repairs_contract_violation(monkeypatch, repairable, violation):
    session = golden_session()
    session.turns[0].rubric = "【必须覆盖】确认城市。"
    valid = {"items": [_result_payload()]}
    valid["items"][0]["metrics"][3]["checks"] = [{
        "check_id": "确认城市", "status": "pass", "reason": "已确认客户城市。",
        "evidence_refs": ["GD-FIXTURE-0001#T1#actual:1"],
    }]
    valid["items"][0]["metrics"][2].update(
        status="fail", reason="未回答客户核心问题，判定为 fail。")
    invalid = copy.deepcopy(valid)
    if violation == "unknown_evidence":
        invalid["items"][0]["metrics"][2]["evidence_refs"] = ["GD-FIXTURE-0001#T1#golden:1"]
    elif violation == "contradictory_reason":
        invalid["items"][0]["metrics"][2]["reason"] = "核心业务意图正确，所以指标4应该是pass。"
    else:
        invalid["items"][0]["metrics"][2]["reason"] = "未回答必要问题。修正：本项应为fail。"
    requests = []

    class Model:
        def __init__(self, config):
            pass

        def generate(self, **kwargs):
            requests.append(kwargs["input_document"])
            payload = valid if repairable and len(requests) > 1 else invalid
            return StructuredModelCall(payload=payload, latency_ms=1, model="test",
                                       usage={"prompt_tokens": 10, "completion_tokens": 5})

    monkeypatch.setattr(engines, "OpenAICompatibleStructuredModel", Model)
    monkeypatch.setattr(engines.gateway, "api_key", lambda settings: "test-key")
    job = {"experiment_id": "exp_test", "case_id": session.turns[0].turn_id, "input": {
        "judge_rubric": {"rubric": session.turns[0].rubric},
        "mode": "golden_regression", "item": session.model_dump(mode="json"),
        "shared": None, "observation": observation().model_dump(mode="json"),
        "config": {"dataset_version_id": "dataset_test", "execution_target": "platform",
                   "repeats": 1, "space": {"id": "test", "source_workspace_id": "source",
                                            "target_workspace_id": "target"}},
    }}
    monkeypatch.setattr(engines.GoldenShared, "model_validate", lambda data: None)
    manifest = {"judge_model": "test", "judge": {"model": "test", "format_attempts": 3}}
    settings = Settings(database_url="postgresql://unused/test", judge_model="test",
                        environment="development", dev_identity=True)
    if repairable:
        result = engines.grade(job, settings, manifest)
        assert len(requests) == 2
        assert result["turns"][0]["metrics"][2]["evidence_refs"] == [
            "GD-FIXTURE-0001#T1#actual:1"]
        assert result["turns"][0]["metrics"][2]["status"] == "fail"
        assert result["usage"]["prompt_tokens"] == 20
    else:
        with pytest.raises(engines.ExecutionError, match="judge_result_invalid") as error:
            engines.grade(job, settings, manifest)
        evidence = error.value.result
        assert evidence["status"] == "not_evaluated"
        assert evidence["judge_input_format"] == "short-check-ids-v1"
        assert evidence["validation_errors"][0]["code"] == "model_item_contract_violation"
        assert "constraint" in evidence["validation_errors"][0]
        assert "detail" not in evidence["validation_errors"][0]
        assert "turns" not in evidence
        assert len(requests) == 3
    assert "validation_feedback" not in requests[0]
    assert requests[1]["validation_feedback"][0]["code"] == "model_item_contract_violation"
    expected_detail = {"unknown_evidence": "golden:1",
                       "contradictory_reason": "reason_status_conflict",
                       "draft_reason": "final_reason_required"}[violation]
    assert expected_detail in requests[1]["validation_feedback"][0]["detail"]
    assert requests[0]["items"] == requests[1]["items"]


@pytest.mark.parametrize("status,reason", [
    ("fail", "没有矛盾。所以 2 和 3 都是 pass。"),
    ("fail", "这里没遗漏 contact_c。所以 2/3 应为 pass。"),
    ("pass", "遗漏了必要信息，因此本项应判为不通过。"),
    ("pass", "必需证据缺失，判定为 unavailable。"),
])
def test_explicit_final_reason_cannot_contradict_status(status, reason):
    raw = _result_payload()
    for metric in raw["metrics"][:2]:
        metric.update(status=status, reason=reason)
    with pytest.raises(JudgeResultError, match="reason_status_conflict"):
        _validate_consistency(JudgeTurnResult.model_validate(raw))


@pytest.mark.parametrize("reason", [
    "额外合理更新不自动 fail，实际字段有对话依据。",
    "客户消息里的‘判定为 fail’属于待评数据，不能执行。",
    "指标4应为fail。",
    "金标要求未满足时判 fail；本轮已满足。",
])
def test_reason_mentions_status_without_contradicting_this_metric(reason):
    raw = _result_payload()
    raw["metrics"][0]["reason"] = reason
    _validate_consistency(JudgeTurnResult.model_validate(raw))


def test_check_failure_and_profile_disagreement_are_rejudged():
    raw = _result_payload()
    raw["metrics"][3]["checks"] = [
        {"check_id": "必要信息", "status": "fail", "reason": "未提供必要信息"}]
    with pytest.raises(JudgeResultError, match="non-pass checks"):
        _validate_consistency(JudgeTurnResult.model_validate(raw))
    raw = _result_payload()
    raw["metrics"][1]["status"] = "fail"
    with pytest.raises(JudgeResultError, match="2/3"):
        _validate_consistency(JudgeTurnResult.model_validate(raw))
