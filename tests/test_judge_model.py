"""模型判定：适配器解析、批次切分、逐条校验、失败不落库、API 串起来。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_judge_report import PROMPTS_ROOT, golden_session, observation, turn_result

from golden_eval.adapters.sqlite_prompts import SqlitePromptStore
from golden_eval.application.judge_model import judge_batch, pending_observations
from golden_eval.application.prompt_registry import PromptRegistry
from golden_eval.domain.models import (
    CreateRunRequest,
    PromptKind,
    RunIdentity,
    RunStatus,
    RunSummary,
)
from golden_eval.integrations.structured_model import (
    FixtureStructuredModel,
    OpenAICompatibleStructuredModel,
    StructuredModelConfig,
    StructuredModelError,
)

METRIC_IDS = ("2", "3", "4", "5", "6", "8", "9")


def _config(**overrides: Any) -> StructuredModelConfig:
    base = {
        "provider": "openai_compatible",
        "model": "gpt-4.1-mini",
        "base_url": "https://gateway.example.com/v1",
        "api_key": "judge-secret",
        "timeout_seconds": 30.0,
        "max_retries": 1,
    }
    base.update(overrides)
    return StructuredModelConfig(**base)


def _result_payload(repeat_index: int = 1, *, ref: str = "GD-FIXTURE-0001#T1#actual:1") -> dict:
    return {
        "turn_id": "GD-FIXTURE-0001#T1",
        "repeat_index": repeat_index,
        "metrics": [
            {
                "metric_id": metric_id,
                "status": "pass",
                "reason": f"{metric_id} ok",
                "evidence_refs": [ref],
                **(
                    {"ratio": {"numerator": 1, "denominator": 1, "value": 1.0}}
                    if metric_id == "5"
                    else {}
                ),
            }
            for metric_id in METRIC_IDS
        ],
    }


def test_completion_url_is_built_from_base_url() -> None:
    from golden_eval.integrations.structured_model import _completion_url

    assert _completion_url("https://gw.example.com/v1") == (
        "https://gw.example.com/v1/chat/completions"
    )
    assert _completion_url("https://gw.example.com/v1/") == (
        "https://gw.example.com/v1/chat/completions"
    )
    assert _completion_url("https://gw.example.com/chat/completions") == (
        "https://gw.example.com/chat/completions"
    )


def test_model_parses_json_and_tolerates_code_fences(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001 - 替代 urllib 调用
        seen.append(json.loads(request.data.decode("utf-8")))

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "model": "gpt-4.1-mini-2026",
                        "choices": [
                            {
                                "message": {
                                    "content": '```json\n{"items": [{"turn_id": "t1"}]}\n```'
                                }
                            }
                        ],
                        "usage": {"total_tokens": 123},
                    }
                ).encode()

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    call = OpenAICompatibleStructuredModel(_config()).generate(
        prompt_version="full-evaluation-v8",
        system_prompt="你是评分引擎",
        input_document={"items": []},
        output_schema={},
    )

    assert call.payload == {"items": [{"turn_id": "t1"}]}
    assert call.model == "gpt-4.1-mini-2026"
    assert call.usage["total_tokens"] == 123
    assert seen[0]["model"] == "gpt-4.1-mini"
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[0]["messages"][0]["content"] == "你是评分引擎"


def test_model_surfaces_http_and_json_errors(monkeypatch) -> None:
    import urllib.error

    def unauthorized(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", unauthorized)
    with pytest.raises(StructuredModelError) as excinfo:
        OpenAICompatibleStructuredModel(_config()).generate(
            prompt_version="v5", system_prompt="p", input_document={}, output_schema={}
        )
    assert excinfo.value.code == "model_http_401"
    assert excinfo.value.retryable is False  # 鉴权失败不重试

    def bad_json(request, timeout=None):  # noqa: ANN001
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "我无法输出 JSON"}}]}
                ).encode()

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", bad_json)
    with pytest.raises(StructuredModelError) as excinfo:
        OpenAICompatibleStructuredModel(_config()).generate(
            prompt_version="v5", system_prompt="p", input_document={}, output_schema={}
        )
    assert excinfo.value.code == "model_invalid_json"


def test_model_is_not_constructed_without_configuration() -> None:
    with pytest.raises(StructuredModelError) as excinfo:
        OpenAICompatibleStructuredModel(_config(model=""))
    assert excinfo.value.code == "model_not_configured"

def test_ndjson_and_array_outputs_are_collected_into_items(monkeypatch) -> None:
    """逐 Turn 工单实测会被模型吐成 NDJSON（每行一个对象），也见过整个数组。"""

    for content, expected in [
        (
            '{"turn_id": "T1", "profile_update": {}}\n{"turn_id": "T2", "profile_update": {}}\n',
            [
                {"turn_id": "T1", "profile_update": {}},
                {"turn_id": "T2", "profile_update": {}},
            ],
        ),
        (
            '[{"turn_id": "T1"}, {"turn_id": "T2"}]',
            [{"turn_id": "T1"}, {"turn_id": "T2"}],
        ),
        (
            '```json\n{"turn_id": "T1"}\n{"turn_id": "T2"}\n```',
            [{"turn_id": "T1"}, {"turn_id": "T2"}],
        ),
    ]:
        calls: list[str] = []

        def fake_urlopen(request, timeout=None, content=content):  # noqa: ANN001
            calls.append(content)

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(
                        {"choices": [{"message": {"content": content}}]}
                    ).encode()

            return Response()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        call = OpenAICompatibleStructuredModel(_config()).generate(
            prompt_version="profile-enrichment-v4",
            system_prompt="p",
            input_document={"items": []},
            output_schema={},
        )
        assert call.payload == {"items": expected}, content


def test_multi_object_ndjson_still_reports_invalid_json_when_lines_are_broken(monkeypatch) -> None:
    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "{oops}\n{also broken}"}}]}
                ).encode()

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(StructuredModelError) as excinfo:
        OpenAICompatibleStructuredModel(_config()).generate(
            prompt_version="v5", system_prompt="p", input_document={}, output_schema={}
        )
    assert excinfo.value.code == "model_invalid_json"


def test_pending_observations_skip_judged_and_respect_limit() -> None:
    observations = [observation(repeat_index=index) for index in range(1, 5)]
    judged = [turn_result(repeat_index=1)]

    pending = pending_observations(observations, judged, limit=2)

    assert [item.repeat_index for item in pending] == [2, 3]


def _evaluate_run() -> RunSummary:
    moment = observation().created_at
    return RunSummary(
        run_id="run-judge-model",
        kind="evaluate",
        name="评测 · dv_fixture",
        status=RunStatus.COMPLETED,
        request=CreateRunRequest(
            kind="evaluate",
            name="评测 · dv_fixture",
            dataset_id="dv_fixture",
            target_connection_id="fixture-target",
        ),
        created_at=moment,
        updated_at=moment,
        identity=RunIdentity(tenant_id="ws", source="connection_settings"),
    )


def _judge_prompt(tmp_path):
    return PromptRegistry(
        SqlitePromptStore(tmp_path / "prompts.sqlite"), prompts_root=PROMPTS_ROOT
    ).active(PromptKind.JUDGE).model_copy(update={"version_label": "full-evaluation-v11"})


@pytest.mark.parametrize("criteria", [
    ["调用工具 {{mcp.lookup.01234567-89ab-cdef-0123-456789abcdef}}", "确认城市"],
    ["C1", "依据字段说明更新预算"],
])
def test_judge_short_ids_preserve_criteria_and_business_failures(tmp_path, criteria):
    session = golden_session()
    session.turns[0].rubric = "【必须覆盖】" + "；".join(criteria)
    original = session.model_dump(mode="json")
    observed = observation()

    class Model(FixtureStructuredModel):
        def generate(self, **kwargs):
            compiled = kwargs["input_document"]["items"][0]["input"]["compiled_rubric"]
            ids = compiled["must_cover"]
            assert len(ids) == 2 and not set(ids) & set(criteria)
            assert compiled["must_cover_details"] == dict(zip(ids, criteria, strict=True))
            payload = _result_payload()
            metric = next(m for m in payload["metrics"] if m["metric_id"] == "5")
            metric.update(status="fail", ratio={"numerator": 1, "denominator": 2}, checks=[
                {"check_id": key, "status": status, "reason": "依据本轮回答",
                 "evidence_refs": ["GD-FIXTURE-0001#T1#actual:1"]}
                for key, status in zip(ids, ["pass", "fail"], strict=True)])
            return FixtureStructuredModel([{"items": [payload]}]).generate(**kwargs)

    outcome = judge_batch(run=_evaluate_run(), sessions={session.session_id: session},
        pending=[observed], prompt=_judge_prompt(tmp_path).model_copy(
            update={"version_label": "full-evaluation-v14"}), model=Model([]))
    assert not outcome.errors
    metric = next(m for m in outcome.judged[0].metrics if m.metric_id == "5")
    assert metric.status == "fail"
    assert [c.check_id for c in metric.checks] == criteria
    assert metric.ratio.numerator == 1 and metric.ratio.denominator == 2
    assert session.model_dump(mode="json") == original


@pytest.mark.parametrize("invalid", [
    "unknown", "duplicate", "duplicate_criterion", "missing", "ratio", "evidence"])
def test_judge_short_ids_still_reject_invalid_checks(tmp_path, invalid):
    session = golden_session()
    session.turns[0].rubric = "【必须覆盖】确认城市；给出下一步"
    if invalid == "duplicate_criterion":
        session.turns[0].rubric = "【必须覆盖】确认城市；确认城市"

    class Model(FixtureStructuredModel):
        def generate(self, **kwargs):
            ids = kwargs["input_document"]["items"][0]["input"]["compiled_rubric"]["must_cover"]
            payload = _result_payload()
            metric = next(m for m in payload["metrics"] if m["metric_id"] == "5")
            metric.update(ratio={"numerator": 2, "denominator": 2}, checks=[
                {"check_id": key, "status": "pass", "reason": "本轮回答有依据",
                 "evidence_refs": ["GD-FIXTURE-0001#T1#actual:1"]} for key in ids])
            if invalid == "unknown":
                metric["checks"][0]["check_id"] = "C999"
            elif invalid in {"duplicate", "duplicate_criterion"}:
                metric["checks"][1]["check_id"] = ids[0]
            elif invalid == "missing":
                metric["checks"].pop()
                metric["ratio"] = {"numerator": 1, "denominator": 1}
            elif invalid == "ratio":
                metric["ratio"]["numerator"] = 1
            else:
                metric["checks"][0]["evidence_refs"] = ["another-turn#actual:1"]
            return FixtureStructuredModel([{"items": [payload]}]).generate(**kwargs)

    outcome = judge_batch(run=_evaluate_run(), sessions={session.session_id: session},
        pending=[observation()], prompt=_judge_prompt(tmp_path).model_copy(
            update={"version_label": "full-evaluation-v14"}), model=Model([]))
    assert not outcome.judged
    assert [e["code"] for e in outcome.errors] == ["model_item_contract_violation"]


def test_short_check_ids_are_scoped_to_each_turn(tmp_path):
    first = golden_session()
    first.turns[0].rubric = "【必须覆盖】确认城市"
    second = first.model_copy(deep=True, update={"session_id": "OTHER"})
    second.turns[0].turn_id = "OTHER#T1"
    second.turns[0].rubric = "【必须覆盖】说明下一步"
    observations = [observation(), observation().model_copy(update={
        "observation_id": "obs_other", "session_id": "OTHER", "turn_id": "OTHER#T1"})]

    class Model(FixtureStructuredModel):
        def generate(self, **kwargs):
            payloads = []
            for item in kwargs["input_document"]["items"]:
                compiled = item["input"]["compiled_rubric"]
                assert compiled["must_cover"] == ["C1"]
                ref = f"{item['turn_id']}#actual:1"
                payload = _result_payload(ref=ref)
                payload["turn_id"] = item["turn_id"]
                payload["metrics"][3]["checks"] = [{"check_id": "C1", "status": "pass",
                    "reason": "本轮已覆盖", "evidence_refs": [ref]}]
                payloads.append(payload)
            return FixtureStructuredModel([{"items": payloads}]).generate(**kwargs)

    outcome = judge_batch(run=_evaluate_run(),
        sessions={first.session_id: first, second.session_id: second}, pending=observations,
        prompt=_judge_prompt(tmp_path).model_copy(update={"version_label": "full-evaluation-v14"}),
        model=Model([]))
    assert not outcome.errors
    checks = {r.turn_id: r.metrics[3].checks[0].check_id for r in outcome.judged}
    assert checks == {first.turns[0].turn_id: "确认城市", second.turns[0].turn_id: "说明下一步"}


@pytest.mark.parametrize("target,policy", [
    ("platform", "external_mcp_non_equivalent"),
    ("fixture", "equivalent_fixture"),
])
def test_judge_worklist_declares_mcp_return_policy(tmp_path, target, policy):
    from golden_eval.application.judge_report import build_worklist

    observed = observation().model_copy(update={"execution_target": target})
    session = golden_session()
    worklist = build_worklist(
        run=_evaluate_run(), sessions={session.session_id: session},
        observations=[observed],
        prompt=_judge_prompt(tmp_path).model_copy(update={"version_label": "full-evaluation-v9"}),
    )
    item = worklist["items"][0]["input"]
    assert item["execution"]["tool_return_policy"] == policy
    assert item["golden"]["messages"] == [
        message.model_dump(mode="json") for message in session.turns[0].expected.messages
    ]
    assert item["actual"]["reply"] == [
        message.model_dump(mode="json")
        for message in observed.response_messages
    ]


@pytest.mark.parametrize("version", ["full-evaluation-v8", "full-evaluation-v9"])
def test_attachment_evidence_is_preserved_without_changing_old_contract(tmp_path, version):
    from golden_eval.application.judge_report import build_worklist
    from golden_eval.domain.models import ConversationMessage

    reply = ConversationMessage(role="assistant", content="scheme.jpg", attachments=[{
        "url": "https://assets.example.com/scheme.jpg", "content_type": "image/jpeg",
        "name": "scheme.jpg",
    }])
    observed = observation().model_copy(update={"response_messages": [reply]})
    session = golden_session()
    prompt = _judge_prompt(tmp_path).model_copy(update={"version_label": version})
    item = build_worklist(run=_evaluate_run(), sessions={session.session_id: session},
                          observations=[observed], prompt=prompt)["items"][0]["input"]
    if version == "full-evaluation-v9":
        assert item["actual"]["reply"][0]["attachments"][0]["url"] == (
            "https://assets.example.com/scheme.jpg"
        )
        assert "tool_return_policy" in item["execution"]
    else:
        assert item["actual"]["reply"] == [{"role": "assistant", "content": "scheme.jpg"}]
        assert "tool_return_policy" not in item["execution"]


def test_judge_batch_keeps_good_items_and_reports_bad_ones(tmp_path) -> None:
    run = _evaluate_run()
    session = golden_session()
    observations = [observation(repeat_index=1), observation(repeat_index=2)]
    good = _result_payload(1)
    broken = _result_payload(2, ref="not-allowed#ref")  # 非法证据引用
    model = FixtureStructuredModel([{"items": [good, broken]}])

    outcome = judge_batch(
        run=run,
        sessions={session.session_id: session},
        pending=observations,
        prompt=_judge_prompt(tmp_path),
        model=model,
        model_profile="fixture-model",
    )

    assert outcome.requested_count == 2
    assert outcome.judged_count == 1
    assert outcome.judged[0].repeat_index == 1
    assert [item["code"] for item in outcome.errors] == ["model_item_contract_violation"]
    assert model.calls[0]["items"] == [("GD-FIXTURE-0001#T1", 1), ("GD-FIXTURE-0001#T1", 2)]


def test_judge_batch_reports_model_failure_without_inventing_verdicts(tmp_path) -> None:
    class Broken:
        def generate(self, **_: Any):
            raise StructuredModelError("model_http_500", "boom", retryable=True)

    run = _evaluate_run()
    session = golden_session()
    outcome = judge_batch(
        run=run,
        sessions={session.session_id: session},
        pending=[observation()],
        prompt=_judge_prompt(tmp_path),
        model=Broken(),
    )

    assert outcome.judged == []
    assert outcome.errors == [
        {"scope": "batch", "code": "model_http_500", "detail": "boom"}
    ]


@pytest.mark.parametrize("settled", [False, True, None])
def test_unsettled_profile_is_unavailable_without_changing_reply_scores(tmp_path, settled):
    from golden_eval.application.judge_report import build_worklist

    observed = observation().model_copy(update={
        "profile_after": {"city": "成都"}, "profile_writes": {"city": {"value": "成都"}},
        "profile_readback": {} if settled is None else {"settled": settled},
    })
    run, session, prompt = _evaluate_run(), golden_session(), _judge_prompt(tmp_path)
    worklist = build_worklist(run=run, sessions={session.session_id: session},
                             observations=[observed], prompt=prompt)
    actual = worklist["items"][0]["input"]["actual"]
    assert actual["profile_readback"] == observed.profile_readback
    if settled is False:
        assert actual["profile_after"] is None
        assert actual["profile_writes"] == {}
    outcome = judge_batch(run=run, sessions={session.session_id: session}, pending=[observed],
                          prompt=prompt,
                          model=FixtureStructuredModel([{"items": [_result_payload()]}]))
    assert not outcome.errors
    metrics = {m.metric_id: m for m in outcome.judged[0].metrics}
    for key in ("2", "3"):
        assert metrics[key].status == ("unavailable" if settled is False else "pass")
    assert all(metrics[key].status == "pass" for key in ("4", "5", "6", "8", "9"))
    assert observed.profile_after == {"city": "成都"}


@pytest.mark.parametrize(("diagnostic", "completed", "expected"), [
    ("profile_missing_update", False, "unavailable"),
    ("profile_missing_update", True, "fail"),
    ("profile_invalid_update", False, "fail"),
])
def test_bounded_async_readback_never_proves_missing_update(
    tmp_path, diagnostic, completed, expected,
):
    observed = observation().model_copy(update={
        "profile_readback": {"strategy": "bounded_poll", "settled": True,
                             "timeout_seconds": 90, "completion_verified": completed},
    })
    payload = _result_payload()
    for metric in payload["metrics"][:2]:
        metric.update(status="fail", diagnostic_code=diagnostic, reason="画像证据不符合要求。")
    session = golden_session()
    outcome = judge_batch(
        run=_evaluate_run(), sessions={session.session_id: session}, pending=[observed],
        prompt=_judge_prompt(tmp_path).model_copy(update={"version_label": "full-evaluation-v11"}),
        model=FixtureStructuredModel([{"items": [payload]}]),
    )
    assert not outcome.errors
    metrics = {m.metric_id: m for m in outcome.judged[0].metrics}
    assert metrics["2"].status == metrics["3"].status == expected
    assert all(metrics[key].status == "pass" for key in ("4", "5", "6", "8", "9"))


def test_unobserved_required_profile_is_unknown_even_when_model_says_pass(tmp_path):
    observed = observation().model_copy(update={
        "profile_writes": {}, "profile_after": {"city": ""},
        "profile_observed_before": {"city": ""},
        "profile_readback": {"settled": True, "completion_verified": False},
    })
    session = golden_session()
    outcome = judge_batch(run=_evaluate_run(), sessions={session.session_id: session},
        pending=[observed], prompt=_judge_prompt(tmp_path),
        model=FixtureStructuredModel([{"items": [_result_payload()]}]))
    assert not outcome.errors
    assert all(metric.status == "unavailable" for metric in outcome.judged[0].metrics[:2])
