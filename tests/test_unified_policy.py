"""同一评分器隔离各空间规则，并显式传递参考确认状态。"""
from copy import deepcopy
from pathlib import Path

import pytest
from test_jsonl_corpus import shared_payload
from test_judge_model import _evaluate_run
from test_judge_report import golden_session, observation

from golden_eval.application.judge_report import build_worklist
from golden_eval.application.prompt_registry import load_builtin_prompts
from golden_eval.application.rubric_build import build_worklist as rubric_worklist
from golden_eval.domain.models import GoldenShared, PromptKind


@pytest.mark.parametrize("status", ["unknown", "generated", "confirmed"])
def test_workspace_rules_and_reference_status_are_isolated(status):
    builtins = load_builtin_prompts(Path(__file__).resolve().parents[1] / "prompts")
    session = golden_session()
    original = session.model_dump(mode="json")
    first = shared_payload()
    first["context"].update(reference_status=status, guidance="允许发送图片链接。",
        skill_rules=[{"name": "分类", "instruction": "仅按当前字段说明分类。"}])
    second = deepcopy(first)
    second["context"].update(guidance="禁止发送图片链接。", reference_status="unknown")
    for raw in [first, second, first]:
        shared = GoldenShared.model_validate(raw)
        judge = build_worklist(run=_evaluate_run(), sessions={session.session_id: session},
            observations=[observation()], prompt=builtins[PromptKind.JUDGE].version(is_active=True),
            shared=shared)
        rubric = rubric_worklist(draft_id="draft", revision=1, shared=shared,
            sessions=[session], prompt=builtins[PromptKind.RUBRIC_DRAFT].version(is_active=True))
        for worklist in [judge, rubric]:
            context = worklist["shared_context"]
            assert context["guidance"] == raw["context"]["guidance"]
            assert context["skill_rules"] == raw["context"]["skill_rules"]
            assert context["reference_status"] == raw["context"]["reference_status"]
        assert "current_rubric_draft" not in rubric["items"][0]["input"]
        policy = judge["items"][0]["input"]["execution"]["tool_return_policy"]
        assert policy == "comparison_unverified"
    assert session.model_dump(mode="json") == original


def test_legacy_reference_status_is_unknown_and_invalid_status_rejected():
    assert GoldenShared.model_validate(shared_payload()).context.reference_status == "unknown"
    raw = shared_payload()
    raw["context"]["reference_status"] = "automatically_approved"
    with pytest.raises(ValueError):
        GoldenShared.model_validate(raw)


def test_runtime_configuration_does_not_rewrite_reference():
    from agent_eval.workspace_policy import judging_shared

    raw = shared_payload()
    original = deepcopy(raw)
    policy = {"context": {"guidance": "运行时规则", "skill_rules": [],
                           "profile_schema": []}}
    judged = judging_shared(raw, policy)
    assert judged.context.guidance == "运行时规则"
    assert judged.context.profile_schema == []
    assert judged.context.reference_status == "unknown"
    assert raw == original
    assert judging_shared(raw, None).context.profile_schema


def test_existing_workspace_policy_is_reused_without_network():
    from agent_eval.workspace_policy import prepare

    frozen = {"workspace_id": "target", "context": {"guidance": "first"}}
    assert prepare({}, None, None, {"workspace_policy": frozen}) is frozen


def test_policy_reads_target_configuration_with_target_credentials(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from agent_eval import workspace_policy
    from golden_eval.domain.corpus import ProfileContract, ProfileFieldDefinition

    seen = []
    contract = ProfileContract(workspace_id="target", target_config_hash="config",
        fields=[ProfileFieldDefinition(key="city", description="Current field")],
        guidance_available=True, guidance="Current guidance",
        skill_rules=[{"instruction": "tool.update.visitor.city"}], captured_at=datetime.now(UTC))

    class Reader:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def read(self, workspace_id):
            assert workspace_id == "target"
            return contract

    class Store:
        def space(self, space_id):
            raise AssertionError("must use frozen workspace pair")

        def freeze_workspace_policy(self, job, policy):
            return policy

    monkeypatch.setattr(workspace_policy.registry, "credentials", lambda *_: ("source", "target"))
    monkeypatch.setattr(workspace_policy, "PlatformProfileReader", Reader)
    job = {"input": {"config": {"space": {"id": "registered",
        "source_workspace_id": "source", "target_workspace_id": "target"}}}}
    settings = SimpleNamespace(platform_base_url="https://fixture.invalid")
    policy = workspace_policy.prepare(job, Store(), settings, {"execution_reused": True})
    assert seen[0]["token"] == "target"
    assert seen[0]["identity"].tenant_id == "target"
    assert policy["context"]["skill_rules"] == contract.skill_rules
    assert policy["context"]["profile_schema"][0]["description"] == "Current field"
    assert policy["execution_reused"] is True
    # 明确未配置指导不等于读配置失败，仍可依据技能、字段与客户事实评测。
    contract.guidance_available = False
    contract.guidance = None
    assert workspace_policy.prepare(job, Store(), settings, {})["context"]["guidance"] is None


def test_unreadable_configuration_is_not_an_agent_failure(monkeypatch):
    from agent_eval import workspace_policy
    from agent_eval.engines import ExecutionError

    class Store:
        def space(self, _):
            return {"target_workspace_id": "target"}

    def unavailable(*_):
        raise ValueError("registry unavailable")

    monkeypatch.setattr(workspace_policy.registry, "credentials", unavailable)
    with pytest.raises(ExecutionError) as found:
        workspace_policy.prepare({"input": {"config": {"space": {
            "id": "s", "source_workspace_id": "source", "target_workspace_id": "target"}}}},
                                 Store(), None, {})
    assert found.value.code == "workspace_rules_unavailable"
    assert found.value.uncertain is False


def test_unconfirmed_reference_is_excluded_from_rubric_generation():
    builtins = load_builtin_prompts(Path(__file__).resolve().parents[1] / "prompts")
    raw = shared_payload()
    session = golden_session()
    session.turns[0].expected.messages[0].content = "REFERENCE_ANCHOR_DO_NOT_EXPOSE"
    session.turns[0].rubric = "OLD_RUBRIC_DO_NOT_EXPOSE"
    worklist = rubric_worklist(draft_id="d", revision=1,
        shared=GoldenShared.model_validate(raw), sessions=[session],
        prompt=builtins[PromptKind.RUBRIC_DRAFT].version(is_active=True))
    encoded = str(worklist)
    assert "REFERENCE_ANCHOR_DO_NOT_EXPOSE" not in encoded
    assert "OLD_RUBRIC_DO_NOT_EXPOSE" not in encoded
    current = worklist["items"][0]["input"]["current_turn"]
    assert current["golden_reply"] == []
    assert current["profile_update"] == {}
    assert current["tool_calls"] == []


def test_judge_does_not_receive_unconfirmed_reference_answers():
    builtins = load_builtin_prompts(Path(__file__).resolve().parents[1] / "prompts")
    session = golden_session()
    session.turns[0].expected.messages[0].content = "CURRENT_REFERENCE_SENTINEL"
    original = session.model_dump(mode="json")
    for status in ["unknown", "generated", "confirmed"]:
        raw = shared_payload()
        raw["context"]["reference_status"] = status
        work = build_worklist(run=_evaluate_run(), sessions={session.session_id: session},
            observations=[observation()], prompt=builtins[PromptKind.JUDGE].version(is_active=True),
            shared=GoldenShared.model_validate(raw))
        assert ("CURRENT_REFERENCE_SENTINEL" in str(work)) == (status == "confirmed")
    assert session.model_dump(mode="json") == original
