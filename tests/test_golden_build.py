from __future__ import annotations

from datetime import UTC, datetime

from golden_eval.application.golden_build import build_session
from golden_eval.application.turn_projection import build_turn_tasks
from golden_eval.domain.corpus import (
    SenderRole,
    SessionOutcome,
    SourceMessage,
)


def test_leading_human_turn_stays_in_history_for_teacher_forcing() -> None:
    def message(seq: int, role: SenderRole, content: str, minute: int) -> SourceMessage:
        return SourceMessage(
            seq=seq,
            role=role,
            content=content,
            text=content,
            content_type="text",
            sent_at=datetime(2026, 9, 17, 4, 40 + minute, tzinfo=UTC),
        )

    outcome = SessionOutcome(
        workspace_id="ws-1",
        session_id="S-1",
        included=True,
        messages=[
            message(1, SenderRole.VISITOR, "我是Komorebi", 0),
            message(2, SenderRole.HUMAN, "人工助教先接了一句", 1),
            message(3, SenderRole.VISITOR, "抱歉啊", 2),
            message(4, SenderRole.AI, "AI 接手后的回复", 3),
        ],
    )

    session, anomalies = build_session(
        outcome, snapshot_id="snap-x", profile_keys=[], annotated_at="2026-09-21"
    )

    assert session is not None
    assert [item.code for item in anomalies] == ["human_reply_turn_excluded"]
    # 人工处理过的开头不进 turns，但必须留在 history 里。
    assert [turn.input.customer_messages[0].content for turn in session.turns] == ["抱歉啊"]
    assert [(message.role, message.content) for message in session.history or []] == [
        ("user", "我是Komorebi"),
        ("assistant", "人工助教先接了一句"),
        ("user", "抱歉啊"),
        ("assistant", "AI 接手后的回复"),
    ]
    assert session.turns[0].history_length == 2

    tasks = build_turn_tasks(
        run_id="run-1",
        session=session,
        repeat_index=1,
        account_prefix="gde-run-1",
        bot_user_id="bot",
        im_bot_id="im",
    )
    assert [(message.role, message.content) for message in tasks[0].messages] == [
        ("user", "我是Komorebi"),
        ("assistant", "人工助教先接了一句"),
        ("user", "抱歉啊"),
    ]




def test_pulled_profile_writes_become_this_turn_profile_update() -> None:
    """平台日志直接写过的字段，构建金标时就是本轮增量，不需要模型再猜。"""

    def message(
        seq: int,
        role: SenderRole,
        content: str,
        minute: int,
        *,
        profile_writes: dict | None = None,
    ) -> SourceMessage:
        return SourceMessage(
            seq=seq,
            role=role,
            content=content,
            text=content,
            content_type="text",
            sent_at=datetime(2026, 9, 17, 4, 40 + minute, tzinfo=UTC),
            profile_writes=profile_writes or {},
        )

    outcome = SessionOutcome(
        workspace_id="ws-1",
        session_id="S-1",
        included=True,
        messages=[
            message(1, SenderRole.VISITOR, "我要修桌椅", 0),
            message(
                2,
                SenderRole.AI,
                "好的，我来登记",
                1,
                profile_writes={"WztPq_c": "桌椅维修", "not_in_schema": "x"},
            ),
            message(3, SenderRole.VISITOR, "还是要修椅子", 2),
            message(4, SenderRole.AI, "已记录", 3, profile_writes={"WztPq_c": "椅子"}),
        ],
    )

    session, _ = build_session(
        outcome,
        snapshot_id="snap-x",
        profile_keys=["WztPq_c", "dzGkB_c"],
        annotated_at="2026-09-21",
    )

    assert session is not None
    first, second = session.turns
    # 第一轮：平台写了 WztPq_c，不在 schema 里的字段直接丢掉。
    assert {
        key: (update.value, update.updated_from, update.source.value)
        for key, update in first.expected.profile_update.items()
    } == {"WztPq_c": ("桌椅维修", None, "pulled")}
    assert first.expected.profile_snapshot == {"WztPq_c": "桌椅维修", "dzGkB_c": ""}
    # 第二轮：平台改写了同一个字段，旧值进 updated_from。
    assert second.expected.profile_update["WztPq_c"].value == "椅子"
    assert second.expected.profile_update["WztPq_c"].updated_from == "桌椅维修"
    assert second.expected.profile_snapshot == {"WztPq_c": "椅子", "dzGkB_c": ""}
    # 首轮之前的画像仍然是空的：拉取到的是过程中写出来的值。
    assert session.input.memory_before.profile_snapshot == {"WztPq_c": "", "dzGkB_c": ""}
    assert session.expected.memory_after.profile_snapshot == {"WztPq_c": "椅子", "dzGkB_c": ""}


def test_writes_from_excluded_turns_reach_the_next_gold_turn() -> None:
    """人工回复的 Turn 不进金标，但平台在里面写过的画像字段必须跟着带到下一轮。"""

    def message(
        seq: int,
        role: SenderRole,
        content: str,
        minute: int,
        *,
        profile_writes: dict | None = None,
    ) -> SourceMessage:
        return SourceMessage(
            seq=seq,
            role=role,
            content=content,
            text=content,
            content_type="text",
            sent_at=datetime(2026, 9, 17, 4, 40 + minute, tzinfo=UTC),
            profile_writes=profile_writes or {},
        )

    outcome = SessionOutcome(
        workspace_id="ws-1",
        session_id="S-1",
        included=True,
        messages=[
            message(1, SenderRole.VISITOR, "想咨询", 0),
            message(2, SenderRole.AI, "好的", 1, profile_writes={"city": "成都"}),
            message(3, SenderRole.VISITOR, "再问一句", 2),
            message(4, SenderRole.AI, "稍等", 3, profile_writes={"budget": "2000"}),
            message(5, SenderRole.HUMAN, "我来看", 4),
            message(6, SenderRole.VISITOR, "继续", 5),
            message(7, SenderRole.AI, "已记录", 6),
        ],
    )

    session, _ = build_session(
        outcome,
        snapshot_id="snap-x",
        profile_keys=["city", "budget"],
        annotated_at="2026-09-21",
    )

    assert session is not None
    assert len(session.turns) == 2
    assert session.turns[0].expected.profile_update["city"].value == "成都"
    # 人工 Turn 里写下的 budget 落在下一轮：金标没有更细的位置表达「轮间变化」。
    assert session.turns[1].expected.profile_update["budget"].value == "2000"
    assert session.turns[1].expected.profile_snapshot == {"city": "成都", "budget": "2000"}
