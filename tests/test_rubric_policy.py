"""独立起草评分标准，不能从待评分回答或未确认参考反推要求。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from test_jsonl_corpus import shared_payload
from test_judge_report import golden_session
from test_rubric_build import turn_sections

from agent_eval import rubric_policy
from golden_eval.application.prompt_registry import load_builtin_prompts
from golden_eval.domain.models import PromptKind
from golden_eval.integrations.structured_model import StructuredModelCall


def inputs():
    session = golden_session()
    session.turns[0].expected.messages[0].content = "UNCONFIRMED_REFERENCE_SENTINEL"
    session.turns[0].rubric = "OLD_RUBRIC_SENTINEL"
    job = {"case_id": session.turns[0].turn_id, "experiment_id": "run", "input": {
        "shared": shared_payload(), "item": session.model_dump(mode="json"),
        "observation": {"profile_observed_before": {"city": "BEFORE_SENTINEL"},
            "profile_after": {"city": "AFTER_SENTINEL"},
            "response_messages": [{"content": "CANDIDATE_SENTINEL"}]},
        "turn": {"messages": [{"role": "assistant", "content": "PRIOR_HISTORY_SENTINEL"},
                              {"role": "user", "content": "I am in Chengdu."}],
                 "profile_before": {"city": "FALLBACK_SENTINEL"}}}}
    prompt = load_builtin_prompts(Path(__file__).resolve().parents[1] / "prompts")[
        PromptKind.RUBRIC_DRAFT].version(is_active=True)
    manifest = {"workspace_policy": {"config_sha256": "rules-hash", "context": {
        "guidance": "CURRENT_RULE_SENTINEL", "skill_rules": [], "profile_schema": []}},
        "rubric_prompt": prompt.model_dump(mode="json"),
        "judge": {"model": "fixture", "timeout_seconds": 10, "format_attempts": 1}}
    return job, manifest


class Store:
    def freeze_turn_rubric(self, job, basis):
        return basis


def test_independent_rubric_excludes_answers_and_reuses_frozen_criteria(monkeypatch):
    job, manifest = inputs()
    original = deepcopy(job)
    seen = []

    class Model:
        def generate(self, **kwargs):
            seen.append(deepcopy(kwargs))
            payload = {"turns": [turn_sections(job["case_id"]).model_dump()]}
            return StructuredModelCall(payload=payload,
                                       latency_ms=1, model="fixture")

    monkeypatch.setattr(rubric_policy, "OpenAICompatibleStructuredModel", lambda _: Model())
    monkeypatch.setattr(rubric_policy.gateway, "api_key", lambda _: "fixture")
    settings = SimpleNamespace(model_gateway_url="https://fixture.invalid")
    result = rubric_policy.prepare(job, Store(), settings, manifest)
    encoded = str(seen[0]["input_document"])
    for excluded in ["UNCONFIRMED_REFERENCE_SENTINEL", "OLD_RUBRIC_SENTINEL",
                     "CANDIDATE_SENTINEL", "AFTER_SENTINEL", "FALLBACK_SENTINEL"]:
        assert excluded not in encoded
    assert "BEFORE_SENTINEL" in encoded and "CURRENT_RULE_SENTINEL" in encoded
    assert "PRIOR_HISTORY_SENTINEL" in encoded
    assert "本轮无画像更新" not in result["rubric"]
    assert result["source"] == "workspace_rules" and result["rubric_sha256"]
    assert job == original
    manifest["judge_rubrics"] = {job["case_id"]: result}
    assert rubric_policy.prepare(job, None, None, manifest) is result
    assert len(seen) == 1


def test_confirmed_reference_preserved_without_generation():
    job, manifest = inputs()
    job["input"]["shared"]["context"]["reference_status"] = "confirmed"
    result = rubric_policy.prepare(job, Store(), None, manifest)
    assert result["source"] == "confirmed_reference"
    assert result["rubric"] == "OLD_RUBRIC_SENTINEL"
