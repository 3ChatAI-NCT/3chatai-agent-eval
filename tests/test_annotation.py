import json
from types import SimpleNamespace

from test_jsonl_corpus import session_payload, shared_payload, write_dataset

from agent_eval import annotation
from agent_eval.models import digest
from golden_eval.adapters.jsonl_corpus import JsonlCorpusStore
from golden_eval.integrations.structured_model import OpenAICompatibleStructuredModel


def test_annotation_accepts_captured_session_routing(monkeypatch, tmp_path):
    shared = shared_payload()
    session = session_payload()
    routing = {session["session_id"]: {"bot_user_id": "bot-1", "im_bot_id": "im-1"}}
    shared["dataset"]["source_config"]["session_routing"] = routing
    draft = {"status": "draft", "revision": 1,
             "content": {"shared": shared, "items": [session]}}
    profile_prompt, rubric_prompt = annotation.frozen_prompts()
    job = {"draft_id": "draft-1", "session_id": session["session_id"],
           "case_sha256": digest(session), "shared_sha256": digest(shared),
           "model": "qwen3.7-plus", "profile_prompt": profile_prompt,
           "rubric_prompt": rubric_prompt}
    turn_id = session["turns"][0]["turn_id"]
    replies = iter([
        {"results": [{"turn_id": turn_id,
                      "profile_update": {"city": {"value": "Chengdu"}},
                      "profile_snapshot": {"city": "Chengdu"},
                      "evidence_refs": [f"{turn_id}#customer:1"]}]},
        {"session_id": session["session_id"], "turns": [{
            "turn_id": turn_id, "must_cover": ["确认客户所在城市"],
            "forbidden": ["不得编造信息"], "lead_action": "无",
            "tools": "调用 lookup_store 查询门店"}]},
    ])
    requests = []

    def post(_model, body):
        requests.append(body)
        return {"choices": [{"message": {"content": json.dumps(next(replies))}}]}

    monkeypatch.setattr(annotation.gateway, "api_key", lambda _settings: "test-key")
    monkeypatch.setattr(OpenAICompatibleStructuredModel, "_post", post)
    result = annotation.generate(
        job, SimpleNamespace(draft=lambda _draft_id: draft),
        SimpleNamespace(model_gateway_url="https://gateway.example/v1"),
    )
    assert len(requests) == 2
    assert all(request["enable_thinking"] is False for request in requests)
    assert result["turns"][0]["expected"]["profile_snapshot"] == {"city": "Chengdu"}
    assert "确认客户所在城市" in result["turns"][0]["rubric"]
    write_dataset(tmp_path, [result], shared)
    corpus = JsonlCorpusStore.from_directory("captured", tmp_path)
    assert corpus.get_shared("captured").dataset.source_config["session_routing"] == routing


def test_annotation_worker_preserves_model_error_code():
    from unittest.mock import patch

    from agent_eval.worker import run_annotation
    from golden_eval.integrations.structured_model import StructuredModelError

    finished = []
    store = SimpleNamespace(claim_annotation=lambda *_args: {"id": "annotation-test"},
                            finish_annotation=lambda job, **kwargs: finished.append(kwargs))
    with patch("agent_eval.worker.annotation.generate",
               side_effect=StructuredModelError("model_timeout", "sensitive details")):
        assert run_annotation(store, SimpleNamespace(lease_seconds=120), "test")
    assert finished == [{"error_code": "model_timeout"}]


def test_annotation_failure_detail_preserves_phase_without_profile_values():
    from golden_eval.application.profile_enrichment import ProfileEnrichmentError

    error = annotation.AnnotationError(
        "profile", ProfileEnrichmentError("模型返回 1 个 Turn 的结果，工单是 6 个，无法一一对应"))
    assert error.code == "profile_validation_failed"
    assert error.detail["stage"] == "profile"
    assert "6 个" in error.detail["message"]
    unexpected = annotation.AnnotationError("rubric", RuntimeError("secret payload"))
    assert "secret payload" not in str(unexpected.detail)


def test_annotation_worker_persists_safe_validation_detail():
    from unittest.mock import patch

    from agent_eval.worker import run_annotation
    from golden_eval.application.profile_enrichment import ProfileEnrichmentError

    finished = []
    store = SimpleNamespace(claim_annotation=lambda *_args: {"id": "annotation-test"},
                            finish_annotation=lambda job, **kwargs: finished.append(kwargs))
    error = annotation.AnnotationError("profile", ProfileEnrichmentError("missing results for: T2"))
    with patch("agent_eval.worker.annotation.generate", side_effect=error):
        assert run_annotation(store, SimpleNamespace(lease_seconds=120), "test")
    assert finished == [{"error_code": "profile_validation_failed", "error_detail": error.detail}]


def test_profile_batch_contract_matches_json_object_mode():
    from golden_eval.application.profile_enrichment import build_worklist
    from golden_eval.domain.models import GoldenSession, GoldenShared, PromptVersion

    session = GoldenSession.model_validate(session_payload())
    session = session.model_copy(update={"turns": [
        session.turns[0].model_copy(update={"turn_id": f"{session.session_id}#T{i}", "seq": i})
        for i in range(1, 7)]})
    profile_prompt, _ = annotation.frozen_prompts()
    work = build_worklist(draft_id="draft", revision=1,
        shared=GoldenShared.model_validate(shared_payload()),
        sessions=[session],
        prompt=PromptVersion.model_validate(profile_prompt))
    assert "target_workspace" in work
    assert all("target_workspace" not in item["input"] for item in work["items"])
    schema = work["output_schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["results"]["minItems"] == 6
    assert schema["properties"]["results"]["maxItems"] == 6
    assert "results" in work["system_prompt"]


def test_reproduced_single_result_cannot_cover_six_turns():
    import pytest

    from golden_eval.application.profile_enrichment import ProfileEnrichmentError
    from golden_eval.application.profile_model import _normalise_results

    items = [{"turn_id": f"S#T{i}"} for i in range(1, 7)]
    bare = {"profile_update": {}, "profile_snapshot": {}, "evidence_refs": []}
    with pytest.raises(ProfileEnrichmentError, match="返回 1 个.*工单是 6 个"):
        _normalise_results(bare, items)
    complete = {"results": [{**bare, "turn_id": item["turn_id"]} for item in items]}
    assert len(_normalise_results(complete, items)) == 6
