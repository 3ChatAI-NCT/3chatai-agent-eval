"""Rubric worklist: export per-Turn evidence, apply the structured rubric that comes back.

The rubric is the judge's criterion, so it is drafted by an editable internal prompt rather than by
hard-coded rules alone: the worklist carries the prompt body, the prompt version/hash and the
evidence for one Session, and the results come back as structured sections that this module
compiles into the v0.4 fixed-heading string after validating coverage and shape.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import (
    GoldenSession,
    GoldenShared,
    PromptVersion,
    RubricTurnSections,
)
from .profile_rules import profile_definitions
from .rubric_draft import TURN_SECTIONS, align_turn_profile_line


class RubricBuildError(ValueError):
    """Raised when imported rubric results do not satisfy the contract."""


def build_worklist(
    *,
    draft_id: str,
    revision: int,
    shared: GoldenShared,
    sessions: list[GoldenSession],
    prompt: PromptVersion,
    session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Export the rubric-drafting inputs for the requested Sessions.

    `history` 用 Session 里冻结的完整前序对话（含线上人工接手、不进金标的轮次），边界是该 Turn 的
    `history_length`——这样 rubric 起草看到的上下文和被测 Agent 实收的 `messages[]` 是同一份；
    老草案没有 `history` 时回落到「只累加金标轮」的旧口径。
    """

    wanted = set(session_ids) if session_ids else None
    # 工单里给的是**完整字段定义**（key + 中文名 + 类型 + 说明），不是光秃秃的 key：
    # `demo_field_c` 这类空间自定义字段只看名字判断不了含义，rubric 的【画像】相关规则会写歪。
    profile_fields = profile_definitions(shared.context)
    profile_keys = [field.key for field in shared.context.profile_schema]
    items: list[dict[str, Any]] = []
    for session in sessions:
        if wanted is not None and session.session_id not in wanted:
            continue
        source_history = (
            [{"role": message.role, "content": message.content} for message in session.history]
            if session.history is not None
            else None
        )
        gold_only: list[dict[str, Any]] = []
        profile_before = session.input.memory_before.profile_snapshot
        for turn in sorted(session.turns, key=lambda item: item.seq):
            # 两个字段要同时在：只有 history 没有 history_length 时不能猜边界，
            # 宁可回落到旧的「只累加金标轮」口径，也不要静默发一份空历史。
            use_source_history = source_history is not None and turn.history_length is not None
            history = (
                source_history[: turn.history_length]
                if use_source_history
                else list(gold_only)
            )
            items.append(
                {
                    "session_id": session.session_id,
                    "turn_id": turn.turn_id,
                    "seq": turn.seq,
                    "sections_required": list(TURN_SECTIONS),
                    "input": {
                        "history": history,
                        "current_turn": {
                            "customer_messages": [
                                message.model_dump(mode="json")
                                for message in turn.input.customer_messages
                            ],
                            "golden_reply": [
                                message.model_dump(mode="json")
                                for message in turn.expected.messages
                            ],
                            "tool_calls": [
                                call.name for call in turn.expected.tool_calls
                            ],
                            "profile_update": {
                                key: value.value
                                for key, value in sorted(
                                    turn.expected.profile_update.items()
                                )
                            },
                        },
                        **({"profile_before": dict(profile_before)}
                           if prompt.version_label == "rubric-draft-v11" else {}),
                        **({"current_rubric_draft": turn.rubric}
                           if prompt.version_label not in {
                               "rubric-draft-v8", "rubric-draft-v9",
                               "rubric-draft-v10", "rubric-draft-v11"}
                           else {}),
                        "evidence_refs": _evidence_refs(turn),
                    },
                }
            )
            profile_before = turn.expected.profile_snapshot
            gold_only.extend(
                {"role": "user", "content": message.content}
                for message in turn.input.customer_messages
            )
            gold_only.extend(
                {"role": "assistant", "content": message.content}
                for message in turn.expected.messages
            )
    if (prompt.version_label == "rubric-draft-v11"
            and shared.context.reference_status != "confirmed"):
        for item in items:
            current = item["input"]["current_turn"]
            current.update(golden_reply=[], profile_update={}, tool_calls=[])
            item["input"]["evidence_refs"] = [ref for ref in item["input"]["evidence_refs"]
                                               if "#customer:" in ref]
    return {
        "draft_id": draft_id,
        "revision": revision,
        "prompt_kind": prompt.prompt_kind.value,
        "prompt_version": prompt.version_label,
        "prompt_sha256": prompt.content_sha256,
        "system_prompt": prompt.content,
        "output_schema": _output_schema(profile_keys),
        # 空间级背景：guidance 是全局规则（角色定义、沟通风格、禁止事项）；evaluation_notes
        # 说明回放观测限制，不覆盖空间业务规则。逐 Turn 的工单不重复下发。
        "profile_schema": profile_fields,
        "shared_context": {
            "guidance": shared.context.guidance or None,
            **({"reference_status": shared.context.reference_status}
               if prompt.version_label in {"rubric-draft-v10", "rubric-draft-v11"} else {}),
            "evaluation_notes": shared.context.evaluation_notes or None,
            **({"skill_rules": shared.context.skill_rules} if shared.context.skill_rules else {}),
        },
        "items": items,
    }


def apply_results(
    *,
    session: GoldenSession,
    turns: list[RubricTurnSections],
) -> GoldenSession:
    """Validate one Session's structured rubric and compile it into the v0.4 heading string."""

    by_turn = {item.turn_id: item for item in turns}
    if len(by_turn) != len(turns):
        raise RubricBuildError("duplicate turn_id in rubric results")
    known = {turn.turn_id for turn in session.turns}
    unknown = sorted(set(by_turn) - known)
    if unknown:
        raise RubricBuildError(f"rubric results for unknown turns: {', '.join(unknown)}")
    missing = sorted(known - set(by_turn))
    if missing:
        raise RubricBuildError(f"missing rubric results for: {', '.join(missing)}")

    compiled_turns = []
    for turn in sorted(session.turns, key=lambda item: item.seq):
        item = by_turn[turn.turn_id]
        rubric = compile_turn_rubric(item)
        # 【画像】 always mirrors the validated profile golden, never the model's wording.
        rubric = align_turn_profile_line(
            rubric, sorted(turn.expected.profile_update)
        )
        compiled_turns.append(turn.model_copy(update={"rubric": rubric}))

    return session.model_copy(
        update={
            "turns": compiled_turns,
        }
    )


def compile_turn_rubric(item: RubricTurnSections) -> str:
    lines = [
        f"【必须覆盖】{_join(item.must_cover)}。",
        f"【禁止】{_join(item.forbidden)}。",
        f"【主导动作】{item.lead_action.strip()}",
        f"【工具】{item.tools.strip()}",
    ]
    if item.knowledge and item.knowledge.strip():
        lines.append(f"【知识召回】{item.knowledge.strip()}")
    lines.append("【画像】本轮无画像更新。")
    if item.note and item.note.strip():
        lines.append(f"【标注说明】{item.note.strip()}")
    return "\n".join(lines)


def _join(values: list[str]) -> str:
    cleaned = [value.strip().rstrip("。；;") for value in values if value.strip()]
    if not cleaned:
        raise RubricBuildError("rubric section must not be empty")
    return "；".join(cleaned)


def _evidence_refs(turn) -> list[str]:
    refs = [
        f"{turn.turn_id}#customer:{message.seq}"
        for message in turn.input.customer_messages
    ]
    refs.extend(
        f"{turn.turn_id}#assistant:{message.seq}" for message in turn.expected.messages
    )
    return refs


def _output_schema(profile_fields: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["session_id", "turns"],
        "properties": {
            "session_id": {"type": "string"},
            "turns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "turn_id",
                        "must_cover",
                        "forbidden",
                        "lead_action",
                        "tools",
                    ],
                    "properties": {
                        "turn_id": {"type": "string"},
                        "must_cover": {"type": "array", "items": {"type": "string"}},
                        "forbidden": {"type": "array", "items": {"type": "string"}},
                        "lead_action": {"type": "string"},
                        "tools": {"type": "string"},
                        "knowledge": {"type": ["string", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                },
            },
        },
        "notes": {
            "profile_line": (
                "【画像】由调用代码依据画像金标回写，模型不需要输出该小节。"
            ),
            "profile_fields": profile_fields,
        },
    }
