"""一键起草 rubric：批量生成、跳过已生成、失败只影响那一个 Session。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from test_jsonl_corpus import session_payload

from golden_eval.application.rubric_model import plan_sessions
from golden_eval.domain.models import (
    GoldenSession,
    RubricDraftRecord,
)


def _sections(session: GoldenSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "turns": [
            {
                "turn_id": turn.turn_id,
                "must_cover": ["覆盖线上金标的核心结论"],
                "forbidden": ["不得编造链接"],
                "lead_action": "无；不得凭空追加业务动作。",
                "tools": "本轮无可观察执行动作；多余调用记错。",
            }
            for turn in session.turns
        ],
    }




def test_plan_sessions_skips_already_drafted_unless_forced() -> None:
    session = GoldenSession.model_validate(session_payload())
    record = RubricDraftRecord(
        session_id=session.session_id,
        prompt_kind="rubric_draft",
        prompt_version="rubric-draft-v7",
        prompt_sha256="a" * 64,
        turn_count=len(session.turns),
        applied_at=datetime.now(UTC),
    )

    targets, skipped, deferred = plan_sessions(
        sessions=[session],
        records=[record],
        requested=None,
        force=False,
        prompt_version="rubric-draft-v7",
    )
    assert targets == [] and skipped == [session.session_id] and deferred == []

    forced, _, _ = plan_sessions(
        sessions=[session],
        records=[record],
        requested=None,
        force=True,
        prompt_version="rubric-draft-v7",
    )
    assert [item.session_id for item in forced] == [session.session_id]





def test_string_valued_sections_are_accepted_as_single_item_lists() -> None:
    """模型把"只有一条"的小节写成字符串时不该判失败（实测 DeepSeek 会这样返回）。"""

    from golden_eval.domain.models import RubricTurnSections

    turn = RubricTurnSections.model_validate(
        {
            "turn_id": "T1",
            "must_cover": "覆盖核心结论",
            "forbidden": ["不得编造链接"],
            "lead_action": "无",
            "tools": "本轮无可观察执行动作",
        }
    )
    assert turn.must_cover == ["覆盖核心结论"]
