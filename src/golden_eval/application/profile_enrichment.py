"""Profile enrichment: export a worklist, apply reviewed results.

The golden profile cannot be skipped or guessed from reply text: it is produced per Turn from the
Profile Enrichment Prompt, then validated here (field whitelist, recursion, evidence shape) before
it is written into the draft. This module owns that contract; callers only move JSON.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import (
    GoldenProfileUpdate,
    GoldenSession,
    GoldenShared,
    GoldenTurn,
    ProfileUpdateSource,
    PromptVersion,
)
from .profile_rules import profile_definitions
from .rubric_draft import align_turn_profile_line


class ProfileEnrichmentError(ValueError):
    """Raised when imported enrichment results do not satisfy the contract."""


def build_worklist(
    *,
    draft_id: str,
    revision: int,
    shared: GoldenShared,
    sessions: list[GoldenSession],
    prompt: PromptVersion,
    session_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Produce the per-Turn enrichment inputs described by the active profile prompt."""

    wanted = set(session_ids) if session_ids else None
    schema = profile_definitions(shared.context)
    items: list[dict[str, Any]] = []
    for session in sessions:
        if wanted is not None and session.session_id not in wanted:
            continue
        source_history = (
            [{"role": message.role, "content": message.content} for message in session.history]
            if session.history is not None
            else None
        )
        history: list[dict[str, Any]] = []
        previous = dict(session.input.memory_before.profile_snapshot)
        for turn in sorted(session.turns, key=lambda item: item.seq):
            pulled = pulled_profile_values(turn)
            items.append(
                {
                    "session_id": session.session_id,
                    "turn_id": turn.turn_id,
                    "seq": turn.seq,
                    "input": {
                        # 与回放和 Rubric 起草保持同一历史边界，含金标轮前的人工消息。
                        "history": (
                            source_history[: turn.history_length]
                            if source_history is not None and turn.history_length is not None
                            else list(history)
                        ),
                        "current_turn": {
                            "customer_messages": [
                                message.model_dump(mode="json")
                                for message in turn.input.customer_messages
                            ]
                        },
                        "profile_before": dict(previous),
                        # 平台直接拉取到的写入：本轮固定不变，模型不要改写，也不要在
                        # profile_update 里重复输出它们。
                        "pulled_profile_update": pulled,
                        "evidence_refs": _evidence_refs(turn),
                    },
                }
            )
            history.extend(
                {"role": "user", "content": message.content}
                for message in turn.input.customer_messages
            )
            history.extend(
                {"role": "assistant", "content": message.content}
                for message in turn.expected.messages
            )
            previous = dict(turn.expected.profile_snapshot)
    return {
        "draft_id": draft_id,
        "revision": revision,
        "prompt_kind": prompt.prompt_kind.value,
        "prompt_version": prompt.version_label,
        "prompt_sha256": prompt.content_sha256,
        "system_prompt": prompt.content,
        "output_schema": ({"type": "object", "required": ["results"],
                           "properties": {"results": {"type": "array",
                               "minItems": len(items), "maxItems": len(items),
                               "items": _output_schema(shared)}},
                           "additionalProperties": False}
                          if prompt.version_label == "profile-enrichment-v6"
                          else _output_schema(shared)),
        "target_workspace": {"profile_schema": schema,
                             "skill_rules": shared.context.skill_rules},
        "items": items,
    }


def apply_results(
    *,
    shared: GoldenShared,
    session: GoldenSession,
    results: list[dict[str, Any]],
) -> GoldenSession:
    """Validate one session's enrichment results and return the updated session.

    平台直接拉取到的字段不参与校验也不被覆盖：它们已经是平台事实，模型只补没有拉到值的字段。
    因此模型把某个已拉取字段写成别的值不会让整份结果失败，也不会改掉金标。
    """

    allowed = [field.key for field in shared.context.profile_schema]
    allowed_set = set(allowed)
    by_turn = {}
    for item in results:
        turn_id = str(item.get("turn_id") or "")
        if not turn_id:
            raise ProfileEnrichmentError("every result needs a turn_id")
        if turn_id in by_turn:
            raise ProfileEnrichmentError(f"duplicate result for {turn_id}")
        by_turn[turn_id] = item

    missing = [
        turn.turn_id
        for turn in session.turns
        if turn.turn_id not in by_turn
    ]
    if missing:
        raise ProfileEnrichmentError(f"missing results for: {', '.join(sorted(missing))}")

    previous = dict(session.input.memory_before.profile_snapshot)
    turns: list[GoldenTurn] = []
    for turn in sorted(session.turns, key=lambda item: item.seq):
        item = by_turn[turn.turn_id]
        pulled = pulled_profile_values(turn)
        updates = {
            key: value
            for key, value in _validate_updates(
                item.get("profile_update") or {}, allowed_set, turn.turn_id
            ).items()
            if key not in pulled
        }
        updates.update(_pulled_updates(pulled, previous))
        snapshot = _validate_snapshot(
            item.get("profile_snapshot") or {},
            allowed,
            previous,
            updates,
            turn.turn_id,
            pulled=set(pulled),
        )
        turns.append(
            turn.model_copy(
                update={
                    "expected": turn.expected.model_copy(
                        update={"profile_update": updates, "profile_snapshot": snapshot}
                    ),
                    # The rubric's 【画像】 line is a projection of the validated profile golden, so
                    # it can never drift from what will actually be judged.
                    "rubric": align_turn_profile_line(turn.rubric, sorted(updates)),
                }
            )
        )
        previous = snapshot

    final = dict(turns[-1].expected.profile_snapshot) if turns else previous
    return session.model_copy(
        update={
            "turns": turns,
            "expected": session.expected.model_copy(
                update={
                    "memory_after": session.expected.memory_after.model_copy(
                        update={"profile_snapshot": final}
                    )
                }
            ),
        }
    )


def _validate_updates(
    raw: dict[str, Any], allowed: set[str], turn_id: str
) -> dict[str, GoldenProfileUpdate]:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ProfileEnrichmentError(
            f"{turn_id} updates unknown profile fields: {unknown}"
        )
    updates: dict[str, GoldenProfileUpdate] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and "value" in value:
            updates[key] = GoldenProfileUpdate.model_validate(value)
        else:
            updates[key] = GoldenProfileUpdate(value=value)
    return updates


def pulled_profile_values(turn: GoldenTurn) -> dict[str, Any]:
    """本轮平台直接拉取到的字段值（`source == pulled`），是补全模型不能改的部分。"""

    return {
        key: update.value
        for key, update in turn.expected.profile_update.items()
        if update.source is ProfileUpdateSource.PULLED
    }


def _pulled_updates(
    pulled: dict[str, Any], previous: dict[str, Any]
) -> dict[str, GoldenProfileUpdate]:
    """拉取值按当前上一轮快照重算 `updated_from`，不沿用构建时的旧值（人工可能改过前几轮）。"""

    updates: dict[str, GoldenProfileUpdate] = {}
    for key, value in pulled.items():
        previous_value = previous.get(key)
        updates[key] = GoldenProfileUpdate(
            value=value,
            updated_from=None if _is_empty(previous_value) else previous_value,
            source=ProfileUpdateSource.PULLED,
        )
    return updates


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _validate_snapshot(
    raw: dict[str, Any],
    allowed: list[str],
    previous: dict[str, Any],
    updates: dict[str, GoldenProfileUpdate],
    turn_id: str,
    *,
    pulled: set[str] | None = None,
) -> dict[str, Any]:
    if set(raw) != set(allowed):
        missing = sorted(set(allowed) - set(raw))
        extra = sorted(set(raw) - set(allowed))
        raise ProfileEnrichmentError(
            f"{turn_id} profile_snapshot keys must be exactly the schema keys; "
            f"missing={missing}, extra={extra}"
        )
    expected = dict(previous)
    for key, update in updates.items():
        previous_value = previous.get(key)
        changed = update.value != previous_value
        if key in previous and previous_value not in ("", [], None) and changed:
            if update.updated_from != previous[key]:
                raise ProfileEnrichmentError(
                    f"{turn_id} {key} changes a non-empty value but updated_from "
                    "does not match previous value"
                )
        expected[key] = update.value
    # 直接拉取到的字段以平台写入为准：模型在这几个键上写什么都不作数，也不算它写错。
    ignored = pulled or set()
    mismatch = {
        key: raw[key]
        for key in allowed
        if key not in ignored and raw[key] != expected[key]
    }
    if mismatch:
        raise ProfileEnrichmentError(
            f"{turn_id} profile_snapshot is not previous + update; "
            f"mismatched_fields={sorted(mismatch)}"
        )
    return {key: expected[key] for key in allowed}


def _evidence_refs(turn: GoldenTurn) -> list[str]:
    refs = [f"{turn.turn_id}#customer:{message.seq}" for message in turn.input.customer_messages]
    refs.extend(
        f"{turn.turn_id}#assistant:{message.seq}" for message in turn.expected.messages
    )
    return refs


def _output_schema(shared: GoldenShared) -> dict[str, Any]:
    keys = [field.key for field in shared.context.profile_schema]
    return {
        "type": "object",
        "required": ["profile_update", "profile_snapshot", "evidence_refs"],
        "properties": {
            "profile_update": {
                "type": "object",
                "description": (
                    "本 Turn 由你做语义判断的增量，键必须来自 profile_schema；"
                    "输入里 pulled_profile_update 已给的字段不要重复输出"
                ),
                "allowed_keys": keys,
                "excluded_keys": (
                    "输入 pulled_profile_update 中的字段（平台已直接拉取，以它为准）"
                ),
            },
            "profile_snapshot": {
                "type": "object",
                "description": (
                    "上一快照应用本轮 pulled_profile_update 与你的 profile_update 后的完整状态"
                ),
                "required_keys": keys,
            },
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
    }
