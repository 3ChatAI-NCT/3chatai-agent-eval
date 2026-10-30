from __future__ import annotations

from pathlib import Path

import pytest
from test_jsonl_corpus import session_payload, shared_payload

from golden_eval.adapters.sqlite_prompts import SqlitePromptStore
from golden_eval.application.prompt_registry import PromptRegistry
from golden_eval.application.rubric_build import (
    RubricBuildError,
    apply_results,
    build_worklist,
    compile_turn_rubric,
)
from golden_eval.domain.models import (
    ConversationMessage,
    GoldenSession,
    GoldenShared,
    PromptKind,
    RubricTurnSections,
)

PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"


def registry(tmp_path) -> tuple[PromptRegistry, SqlitePromptStore]:
    store = SqlitePromptStore(tmp_path / "prompts.sqlite")
    return PromptRegistry(store, prompts_root=PROMPTS_ROOT), store


def turn_sections(turn_id: str, *, must_cover: list[str] | None = None):
    return RubricTurnSections(
        turn_id=turn_id,
        must_cover=must_cover or ["确认客户所在城市"],
        forbidden=["不得编造门店规则"],
        lead_action="请客户补充具体门店名称",
        tools="本轮无可观察执行动作；多余调用记错。",
        note="考门店信息是否问全",
    )


def test_worklist_carries_prompt_and_per_turn_evidence(tmp_path) -> None:
    prompt_registry, store = registry(tmp_path)
    session = GoldenSession.model_validate(session_payload())

    worklist = build_worklist(
        draft_id="draft-1",
        revision=2,
        shared=GoldenShared.model_validate(shared_payload()),
        sessions=[session],
        prompt=prompt_registry.active(PromptKind.RUBRIC_DRAFT),
    )

    assert worklist["prompt_kind"] == "rubric_draft"
    assert worklist["prompt_version"] == "rubric-draft-v11"
    assert worklist["prompt_sha256"]
    assert worklist["system_prompt"].startswith("## 1. 目的")
    assert worklist["output_schema"]["notes"]["profile_fields"] == ["city"]
    items = worklist["items"]
    assert "current_rubric_draft" not in items[0]["input"]
    assert [item["turn_id"] for item in items] == ["GD-FIXTURE-0001#T1"]
    assert items[0]["input"]["current_turn"]["golden_reply"] == []
    assert items[0]["input"]["evidence_refs"]
    # 字段字典：给 key + 中文名 + 类型 + 说明，模型才知道 `demo_field_c` 这类字段是什么
    schema = worklist["profile_schema"]
    assert schema and schema[0]["key"] == "city"
    assert {"key", "label", "type", "description", "updatable"} <= set(schema[0])
    assert items[0]["sections_required"] == [
        "必须覆盖",
        "禁止",
        "主导动作",
        "工具",
        "画像",
        "标注说明",
    ]
    # 空间级背景经 shared 下发；fixture 既没配 guidance 也没有空间特殊处理，两个键存在但为 null。
    assert worklist["shared_context"] == {
        "guidance": None, "evaluation_notes": None, "reference_status": "unknown"}
    store.close()


def test_worklist_carries_the_workspace_guidance(tmp_path) -> None:
    """空间级全局规则（含禁止事项）必须随工单下发，模型才有依据写 forbidden。"""

    prompt_registry, store = registry(tmp_path)
    payload = shared_payload()
    payload["context"]["guidance"] = "【全局规则】严禁发送用户提供的图片链接"
    payload["context"]["skill_rules"] = [{
        "name": "画像收集", "description": "客户提到城市", "instruction": "按字段说明归一化城市",
    }]
    shared = GoldenShared.model_validate(payload)
    session = GoldenSession.model_validate(session_payload())

    worklist = build_worklist(
        draft_id="draft-1",
        revision=1,
        shared=shared,
        sessions=[session],
        prompt=prompt_registry.active(PromptKind.RUBRIC_DRAFT),
    )

    assert worklist["shared_context"]["guidance"] == "【全局规则】严禁发送用户提供的图片链接"
    assert worklist["shared_context"]["skill_rules"] == payload["context"]["skill_rules"]
    store.close()


def test_worklist_history_is_the_context_the_agent_actually_gets(tmp_path) -> None:
    """history 用 Session 冻结的完整前序对话（含线上人工轮），不是只累加金标轮。"""

    prompt_registry, store = registry(tmp_path)
    shared = GoldenShared.model_validate(shared_payload())
    session = GoldenSession.model_validate(session_payload())
    turn = session.turns[0]
    session = session.model_copy(
        update={
            "history": [
                ConversationMessage(role="user", content="我是Komorebi"),
                ConversationMessage(role="assistant", content="人工助教先接了一句"),
                ConversationMessage(role="user", content="I am in Chengdu."),
                ConversationMessage(role="assistant", content="Understood."),
            ],
            "turns": [turn.model_copy(update={"history_length": 2})],
        }
    )

    worklist = build_worklist(
        draft_id="draft-1",
        revision=1,
        shared=shared,
        sessions=[session],
        prompt=prompt_registry.active(PromptKind.RUBRIC_DRAFT),
    )

    history = worklist["items"][0]["input"]["history"]
    assert [(item["role"], item["content"]) for item in history] == [
        ("user", "我是Komorebi"),
        ("assistant", "人工助教先接了一句"),
    ]
    store.close()


def test_worklist_without_history_length_falls_back_to_gold_turns(tmp_path) -> None:
    """只有 history 没有 history_length 时不猜边界：回落到旧的只累加金标轮口径。"""

    prompt_registry, store = registry(tmp_path)
    shared = GoldenShared.model_validate(shared_payload())
    session = GoldenSession.model_validate(session_payload())
    session = session.model_copy(
        update={
            "history": [ConversationMessage(role="user", content="我是Komorebi")],
            "turns": [session.turns[0].model_copy(update={"history_length": None})],
        }
    )

    worklist = build_worklist(
        draft_id="draft-1",
        revision=1,
        shared=shared,
        sessions=[session],
        prompt=prompt_registry.active(PromptKind.RUBRIC_DRAFT),
    )

    assert worklist["items"][0]["input"]["history"] == []
    store.close()


def test_applying_results_compiles_headings_and_aligns_the_profile_line() -> None:
    session = GoldenSession.model_validate(session_payload())

    updated = apply_results(
        session=session,
        turns=[turn_sections(session.turns[0].turn_id)],
    )

    rubric = updated.turns[0].rubric
    assert rubric.startswith("【必须覆盖】确认客户所在城市。")
    assert "【禁止】不得编造门店规则。" in rubric
    assert "【主导动作】请客户补充具体门店名称" in rubric
    # The model never writes 【画像】: it mirrors the validated profile golden.
    assert "【画像】本轮应更新：city；其余画像字段保持上一轮值。" in rubric
    assert "【标注说明】考门店信息是否问全" in rubric


def test_results_must_cover_every_turn_exactly_once() -> None:
    session = GoldenSession.model_validate(session_payload())

    with pytest.raises(RubricBuildError, match="missing rubric results"):
        apply_results(session=session, turns=[])

    with pytest.raises(RubricBuildError, match="unknown turns"):
        apply_results(
            session=session,
            turns=[turn_sections("GD-FIXTURE-0001#T1"), turn_sections("other#T1")],
        )

    duplicate = turn_sections("GD-FIXTURE-0001#T1")
    with pytest.raises(RubricBuildError, match="duplicate turn_id"):
        apply_results(
            session=session,
            turns=[duplicate, duplicate],
        )


def test_compile_helpers_reject_empty_sections() -> None:
    with pytest.raises(RubricBuildError, match="must not be empty"):
        compile_turn_rubric(
            RubricTurnSections(
                turn_id="t1",
                must_cover=["   "],
                forbidden=["x"],
                lead_action="y",
                tools="z",
            )
        )
