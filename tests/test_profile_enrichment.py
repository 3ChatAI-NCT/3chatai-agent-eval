from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_jsonl_corpus import session_payload, shared_payload

from golden_eval.adapters.platform_profile import (
    GUIDANCE_CONFIG,
    OBJECT_FIELD_LIST,
    OBJECT_LIST,
    SKILLS_CONFIG,
    PlatformProfileReader,
    ProfileContractUnavailableError,
)
from golden_eval.adapters.sqlite_prompts import SqlitePromptStore
from golden_eval.application.profile_enrichment import (
    ProfileEnrichmentError,
    apply_results,
    build_worklist,
)
from golden_eval.application.prompt_registry import PromptRegistry
from golden_eval.domain.models import (
    ConversationMessage,
    GoldenProfileUpdate,
    GoldenSession,
    GoldenShared,
    ProfileUpdateSource,
    PromptKind,
    RunIdentity,
    profile_golden_fingerprint,
)

TENANT = "ws-profile-1"


def test_profile_worklist_contains_field_skill_rules_and_explicit_options():
    data = shared_payload()
    instruction = ("## 城市\n调用 tool.update.visitor.city\n可选值及定义\n"
                   "- {成都：客户在成都}\n- {重庆：客户在重庆}")
    data["context"]["skill_rules"] = [{"name": "城市归类", "instruction": instruction}]
    prompt_store = SqlitePromptStore(":memory:")
    registry = PromptRegistry(
        prompt_store, prompts_root=Path(__file__).resolve().parents[1] / "prompts")
    worklist = build_worklist(draft_id="draft", revision=1,
        shared=GoldenShared.model_validate(data),
        sessions=[GoldenSession.model_validate(session_payload())],
        prompt=registry.active(PromptKind.PROFILE_ENRICHMENT))
    target = worklist["target_workspace"]
    city = next(field for field in target["profile_schema"] if field["key"] == "city")
    assert city["allowed_values"] == ["成都", "重庆"]
    assert city["extraction_rules"][0]["skill"] == "城市归类"
    assert target["skill_rules"] == data["context"]["skill_rules"]
    prompt_store.close()


def identity() -> RunIdentity:
    return RunIdentity(tenant_id=TENANT, staff_id="9", request_id="req-p", source="platform_header")


class FakeConfigTransport:
    """Target configuration in the response shapes the demo verified against the platform."""

    def __init__(
        self, *, skills: str = "writes", with_guidance: bool = True,
        guidance_system: Any = None,
    ) -> None:
        self.calls: list[str] = []
        self.headers: dict[str, dict[str, str]] = {}
        self._skills = skills
        self._with_guidance = with_guidance
        self._guidance_system = guidance_system

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(url)
        self.headers[url] = headers
        if url.endswith(SKILLS_CONFIG):
            if self._skills == "unavailable":
                return {"code": 500, "data": None}
            instruction = (
                "普通回复，不写画像"
                if self._skills == "none"
                else "调用 tool.update.visitor.city 与 tool.update.visitor.budget"
            )
            return {
                "code": 200,
                "data": [
                    {
                        "id": "skill-1",
                        "system": json.dumps(
                            {"title": "画像更新", "instructionPrompt": instruction}
                        ),
                    }
                ],
            }
        if url.endswith(GUIDANCE_CONFIG):
            if not self._with_guidance:
                return {"code": 500, "data": None}
            system = self._guidance_system
            if system is None:
                system = [{"style": "default", "prompt": "你是客服"}]
            return {"code": 200, "data": {"system": system, "sandbox": "尚未生效的指导"}}
        return {"code": 200, "data": {}}

    def post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        self.calls.append(url)
        self.headers[url] = headers
        if url.endswith(OBJECT_LIST):
            return {
                "code": 200,
                "data": {
                    "list": [
                        {"id": "obj-1", "apiName": "butlerVisitor"},
                        {"id": "obj-2", "apiName": "butlerMessage"},
                    ]
                },
            }
        if url.endswith(OBJECT_FIELD_LIST):
            assert payload == {"body": {"businessObjectId": "obj-1"}}
            return {
                "code": 200,
                "data": {
                    "list": [
                        {
                            "apiName": "city",
                            "name": "城市",
                            "type": "TEXT",
                            "desc": "客户所在城市",
                            "createMode": "CUSTOM",
                            "status": "ON",
                        },
                        {
                            "apiName": "budget",
                            "name": "预算",
                            "type": "NUMBER",
                            "desc": "客户预算",
                            "createMode": "CUSTOM",
                            "status": "ON",
                        },
                        {
                            "apiName": "nickname",
                            "name": "昵称",
                            "type": "TEXT",
                            "desc": "客户昵称",
                            "createMode": "CUSTOM",
                            "status": "ON",
                        },
                        {"apiName": "legacy_off", "name": "停用字段", "status": "OFF"},
                        {"apiName": "order_no", "name": "订单号", "createMode": "SYSTEM"},
                    ]
                },
            }
        return {"code": 200, "data": {}}


def test_profile_reader_intersects_fields_and_skills() -> None:
    reader = PlatformProfileReader(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=FakeConfigTransport(),
    )

    contract = reader.read(TENANT)

    keys = [field.key for field in contract.fields]
    # city and budget are exposed *and* written by a published skill; nickname is exposed only.
    assert keys == ["city", "budget"]
    assert contract.guidance_available is True
    assert contract.target_config_hash
    city = next(field for field in contract.fields if field.key == "city")
    assert city.label == "城市"
    assert city.type == "TEXT"
    assert city.description == "客户所在城市"
    assert contract.excluded_fields["nickname"] == [
        "not_written_by_published_system_skills"
    ]
    assert contract.skill_rules == [{
        "name": "画像更新", "description": "",
        "instruction": "调用 tool.update.visitor.city 与 tool.update.visitor.budget",
    }]


def test_sandbox_only_profile_rule_is_not_used_for_evaluation():
    class SandboxTransport(FakeConfigTransport):
        def get(self, url, **kwargs):
            result = super().get(url, **kwargs)
            if url.endswith(SKILLS_CONFIG):
                result["data"].append({"system": "", "sandbox": json.dumps({
                    "title": "未发布", "instructionPrompt": "调用 tool.update.visitor.nickname",
                })})
            return result

    contract = PlatformProfileReader(
        base_url="https://platform.example.com", identity=identity(),
        transport=SandboxTransport(),
    ).read(TENANT)
    assert "nickname" not in {field.key for field in contract.fields}
    assert all(rule["name"] != "未发布" for rule in contract.skill_rules)


@pytest.mark.parametrize("system", ["", [{"style": "default", "prompt": " "}]])
def test_empty_system_guidance_is_unavailable_even_with_sandbox(system) -> None:
    contract = PlatformProfileReader(
        base_url="https://platform.example.com", identity=identity(), token="t",
        transport=FakeConfigTransport(guidance_system=system),
    ).read(TENANT)

    assert contract.guidance_available is False
    assert contract.guidance is None


def test_target_fingerprint_changes_with_guidance_content() -> None:
    hashes = [
        PlatformProfileReader(
            base_url="https://platform.example.com", identity=identity(), token="t",
            transport=FakeConfigTransport(guidance_system=content),
        ).read(TENANT).target_config_hash
        for content in ["衣柜售前顾问", "通用客服"]
    ]
    assert hashes[0] != hashes[1]


def test_profile_reader_requires_readable_target_sources() -> None:
    transport = FakeConfigTransport(skills="unavailable")
    reader = PlatformProfileReader(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    with pytest.raises(ProfileContractUnavailableError) as unavailable:
        reader.read(TENANT)
    assert "skills" in str(unavailable.value)

    # Guidance is optional: the contract is still usable, the gap is reported.
    reader = PlatformProfileReader(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=FakeConfigTransport(with_guidance=False),
    )
    contract = reader.read(TENANT)
    assert contract.guidance_available is False
    assert [field.key for field in contract.fields] == ["city", "budget"]
    assert any("guidance_config_unavailable" in item for item in contract.warnings)


def test_profile_reader_reports_when_nothing_is_writable() -> None:
    reader = PlatformProfileReader(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=FakeConfigTransport(skills="none"),
        golden_profile_keys=["city"],
    )

    contract = reader.read(TENANT)

    # Exposed but never written by a published skill: nothing is safe to write down.
    assert contract.fields == []
    assert contract.excluded_fields["city"] == ["not_written_by_published_system_skills"]
    assert any("画像字段" in warning for warning in contract.warnings)


def test_universal_config_calls_use_bearer_without_tenant_header() -> None:
    transport = FakeConfigTransport()
    reader = PlatformProfileReader(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    reader.read(TENANT)

    butler = transport.headers["https://platform.example.com" + SKILLS_CONFIG]
    assert butler["Tenant_id"] == TENANT
    assert butler["Authorization"] == "Bearer t"
    universal = transport.headers["https://platform.example.com" + OBJECT_LIST]
    assert "Tenant_id" not in universal
    assert universal["Authorization"] == "Bearer t"


def test_worklist_covers_every_turn_with_recursion_inputs() -> None:
    shared_payload_data = shared_payload()
    shared_payload_data["context"]["profile_schema"] = [
        {
            "key": "city",
            "type": "string",
            "description": "客户城市",
            "value_format": "城市名",
            "examples": [],
            "updatable": True,
            "ttl_days": None,
        }
    ]
    session = GoldenSession.model_validate(session_payload())
    session = session.model_copy(
        update={
            "input": session.input.model_copy(
                update={
                    "memory_before": session.input.memory_before.model_copy(
                        update={"profile_snapshot": {"city": ""}}
                    )
                }
            )
        }
    )
    prompt = SqlitePromptStore(":memory:")
    registry = PromptRegistry(
        prompt, prompts_root=Path(__file__).resolve().parents[1] / "prompts"
    )

    worklist = build_worklist(
        draft_id="draft-1",
        revision=3,
        shared=GoldenShared.model_validate(shared_payload_data),
        sessions=[session],
        prompt=registry.active(PromptKind.PROFILE_ENRICHMENT),
    )

    assert worklist["prompt_version"] == "profile-enrichment-v6"
    assert worklist["prompt_sha256"]
    assert worklist["system_prompt"].startswith("## 1. 目的")
    assert len(worklist["items"]) == 1
    item = worklist["items"][0]
    assert item["turn_id"] == "GD-FIXTURE-0001#T1"
    assert item["input"]["profile_before"] == {"city": ""}
    assert item["input"]["evidence_refs"]
    result_schema = worklist["output_schema"]["properties"]["results"]["items"]
    assert result_schema["properties"]["profile_snapshot"]["required_keys"] == ["city"]
    prompt.close()


@pytest.mark.parametrize("history_length", [None, 0, 2])
def test_worklist_preserves_frozen_history_boundary(history_length) -> None:
    session = GoldenSession.model_validate(session_payload())
    session = session.model_copy(update={
        "history": [
            ConversationMessage(role="user", content="我在成都"),
            ConversationMessage(role="assistant", content="人工客服已登记城市"),
            ConversationMessage(role="user", content="后续才说的预算"),
        ],
        "turns": [session.turns[0].model_copy(update={"history_length": history_length})],
    })
    prompt = SqlitePromptStore(":memory:")
    registry = PromptRegistry(
        prompt, prompts_root=Path(__file__).resolve().parents[1] / "prompts"
    )
    worklist = build_worklist(
        draft_id="draft-1",
        revision=1,
        shared=GoldenShared.model_validate(shared_payload()),
        sessions=[session],
        prompt=registry.active(PromptKind.PROFILE_ENRICHMENT),
    )

    expected = [
        {"role": "user", "content": "我在成都"},
        {"role": "assistant", "content": "人工客服已登记城市"},
    ] if history_length == 2 else []
    assert worklist["items"][0]["input"]["history"] == expected
    prompt.close()


def test_apply_results_validates_whitelist_and_recursion() -> None:
    shared_payload_data = shared_payload()
    shared_payload_data["context"]["profile_schema"] = [
        {
            "key": "city",
            "type": "string",
            "description": "客户城市",
            "value_format": "城市名",
            "examples": [],
            "updatable": True,
            "ttl_days": None,
        }
    ]
    shared = GoldenShared.model_validate(shared_payload_data)
    session = GoldenSession.model_validate(session_payload())
    session = session.model_copy(
        update={
            "input": session.input.model_copy(
                update={
                    "memory_before": session.input.memory_before.model_copy(
                        update={"profile_snapshot": {"city": ""}}
                    )
                }
            )
        }
    )

    updated = apply_results(
        shared=shared,
        session=session,
        results=[
            {
                "turn_id": "GD-FIXTURE-0001#T1",
                "profile_update": {"city": {"value": "成都"}},
                "profile_snapshot": {"city": "成都"},
                "evidence_refs": ["GD-FIXTURE-0001#T1#customer:1"],
            }
        ],
    )

    turn = updated.turns[0]
    assert turn.expected.profile_update["city"].value == "成都"
    assert turn.expected.profile_snapshot == {"city": "成都"}
    assert updated.expected.memory_after.profile_snapshot == {"city": "成都"}
    # The 【画像】 line is a projection of the validated profile golden, never hand-typed.
    assert "【画像】本轮应更新：city；其余画像字段保持上一轮值。" in turn.rubric

    with pytest.raises(ProfileEnrichmentError) as unknown:
        apply_results(
            shared=shared,
            session=session,
            results=[
                {
                    "turn_id": "GD-FIXTURE-0001#T1",
                    "profile_update": {"nickname": {"value": "x"}},
                    "profile_snapshot": {"city": "成都"},
                }
            ],
        )
    assert "unknown profile fields" in str(unknown.value)

    with pytest.raises(ProfileEnrichmentError) as wrong_snapshot:
        apply_results(
            shared=shared,
            session=session,
            results=[
                {
                    "turn_id": "GD-FIXTURE-0001#T1",
                    "profile_update": {"city": {"value": "成都"}},
                    "profile_snapshot": {"city": "重庆"},
                }
            ],
        )
    assert "not previous + update" in str(wrong_snapshot.value)

    with pytest.raises(ProfileEnrichmentError) as incomplete:
        apply_results(shared=shared, session=session, results=[])
    assert "missing results" in str(incomplete.value)


def _pulled_profile_payloads() -> tuple[dict, dict]:
    """两轮会话：第一轮的字段是平台日志直接拉到的（pulled），第二轮只有对话。"""

    payload = session_payload()
    payload["input"]["memory_before"]["profile_snapshot"] = {"WztPq_c": "", "dzGkB_c": ""}
    payload["turns"] = [
        {
            "turn_id": "GD-FIXTURE-0001#T1",
            "session_id": "GD-FIXTURE-0001",
            "seq": 1,
            "input": {
                "customer_messages": [{"seq": 1, "type": "text", "content": "我要修桌椅"}]
            },
            "expected": {
                "messages": [{"seq": 1, "type": "text", "content": "好的"}],
                "profile_update": {"WztPq_c": {"value": "桌椅维修", "source": "pulled"}},
                "profile_snapshot": {"WztPq_c": "桌椅维修", "dzGkB_c": ""},
            },
            "rubric": "登记维修类型。",
        },
        {
            "turn_id": "GD-FIXTURE-0001#T2",
            "session_id": "GD-FIXTURE-0001",
            "seq": 2,
            "input": {"customer_messages": [{"seq": 1, "type": "text", "content": "多少钱"}]},
            "expected": {
                "messages": [{"seq": 1, "type": "text", "content": "按上门费算"}],
                "profile_update": {},
                "profile_snapshot": {"WztPq_c": "桌椅维修", "dzGkB_c": ""},
            },
            "rubric": "报价费用口径。",
        },
    ]
    payload["expected"]["memory_after"]["profile_snapshot"] = {
        "WztPq_c": "桌椅维修",
        "dzGkB_c": "",
    }

    shared_payload_data = shared_payload()
    shared_payload_data["context"]["profile_schema"] = [
        {
            "key": key,
            "type": "string",
            "description": key,
            "value_format": "文本",
            "examples": [],
            "updatable": True,
            "ttl_days": None,
        }
        for key in ("WztPq_c", "dzGkB_c")
    ]
    return shared_payload_data, payload


def _pulled_profile_fixture() -> tuple[GoldenShared, GoldenSession]:
    shared_payload_data, payload = _pulled_profile_payloads()
    return (
        GoldenShared.model_validate(shared_payload_data),
        GoldenSession.model_validate(payload),
    )




def test_pulled_profile_values_reach_the_worklist_and_survive_enrichment() -> None:
    """直接拉取到的字段进工单（供模型避让），回写时以平台值为准，不被模型覆盖。"""

    shared, session = _pulled_profile_fixture()
    prompts = SqlitePromptStore(":memory:")
    registry = PromptRegistry(
        prompts, prompts_root=Path(__file__).resolve().parents[1] / "prompts"
    )

    worklist = build_worklist(
        draft_id="draft-1",
        revision=1,
        shared=shared,
        sessions=[session],
        prompt=registry.active(PromptKind.PROFILE_ENRICHMENT),
    )

    first, second = worklist["items"]
    assert first["input"]["pulled_profile_update"] == {"WztPq_c": "桌椅维修"}
    # 后一轮的起点已经包含平台写进去的值。
    assert second["input"]["profile_before"] == {"WztPq_c": "桌椅维修", "dzGkB_c": ""}
    assert second["input"]["pulled_profile_update"] == {}

    updated = apply_results(
        shared=shared,
        session=session,
        results=[
            {
                "turn_id": "GD-FIXTURE-0001#T1",
                # 模型把已拉取字段写成别的值：忽略，不报错，也不改金标。
                "profile_update": {
                    "WztPq_c": {"value": "模型猜错的"},
                    "dzGkB_c": {"value": "是"},
                },
                "profile_snapshot": {"WztPq_c": "模型猜错的", "dzGkB_c": "是"},
            },
            {
                "turn_id": "GD-FIXTURE-0001#T2",
                "profile_update": {},
                "profile_snapshot": {"WztPq_c": "桌椅维修", "dzGkB_c": "是"},
            },
        ],
    )

    first_turn = updated.turns[0]
    assert first_turn.expected.profile_update["WztPq_c"].value == "桌椅维修"
    assert first_turn.expected.profile_update["WztPq_c"].source is ProfileUpdateSource.PULLED
    assert first_turn.expected.profile_update["dzGkB_c"].value == "是"
    assert first_turn.expected.profile_update["dzGkB_c"].source is ProfileUpdateSource.AI
    assert first_turn.expected.profile_snapshot == {"WztPq_c": "桌椅维修", "dzGkB_c": "是"}
    assert updated.turns[1].expected.profile_snapshot == {"WztPq_c": "桌椅维修", "dzGkB_c": "是"}
    assert updated.expected.memory_after.profile_snapshot == {
        "WztPq_c": "桌椅维修",
        "dzGkB_c": "是",
    }
    assert "【画像】本轮应更新：WztPq_c、dzGkB_c；其余画像字段保持上一轮值。" in first_turn.rubric


def test_enrichment_still_cannot_silently_change_an_unpulled_value() -> None:
    """没有被拉取过的字段仍按原合同严格校验：模型改坏了就是整份拒绝。"""

    shared, session = _pulled_profile_fixture()

    with pytest.raises(ProfileEnrichmentError) as wrong:
        apply_results(
            shared=shared,
            session=session,
            results=[
                {
                    "turn_id": "GD-FIXTURE-0001#T1",
                    "profile_update": {},
                    "profile_snapshot": {"WztPq_c": "桌椅维修", "dzGkB_c": "是"},
                },
                {
                    "turn_id": "GD-FIXTURE-0001#T2",
                    "profile_update": {},
                    "profile_snapshot": {"WztPq_c": "桌椅维修", "dzGkB_c": ""},
                },
            ],
        )
    assert "is not previous + update" in str(wrong.value)


def test_profile_fingerprint_ignores_where_a_value_came_from() -> None:
    """平台拉取来的值与模型补来的值等价：换来源不该让补全记账失效。"""

    _shared, session = _pulled_profile_fixture()
    pulled = profile_golden_fingerprint(session)
    ai_sourced = session.model_copy(
        update={
            "turns": [
                session.turns[0].model_copy(
                    update={
                        "expected": session.turns[0].expected.model_copy(
                            update={
                                "profile_update": {
                                    "WztPq_c": GoldenProfileUpdate(
                                        value="桌椅维修", source=ProfileUpdateSource.AI
                                    )
                                }
                            }
                        )
                    }
                ),
                session.turns[1],
            ]
        }
    )

    assert profile_golden_fingerprint(ai_sourced) == pulled








def create_reviewed_draft(client: TestClient) -> str:
    created = client.post(
        "/api/v1/golden-drafts",
        json={"shared": shared_payload(), "sessions": [session_payload()]},
    )
    assert created.status_code == 201
    return created.json()["draft_id"]
