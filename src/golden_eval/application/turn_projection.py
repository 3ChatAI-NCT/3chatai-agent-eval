from __future__ import annotations

from ..domain.models import ConversationMessage, GoldenSession, TurnTask
from .scoring_policy import REQUIRED_METRIC_IDS


def build_turn_tasks(
    *,
    run_id: str,
    session: GoldenSession,
    repeat_index: int,
    account_prefix: str,
    bot_user_id: str,
    im_bot_id: str,
) -> list[TurnTask]:
    """Project complete teacher-forcing history while keeping scoring fields out of the request."""

    tasks: list[TurnTask] = []
    prior_messages: list[ConversationMessage] = []
    history = session.history
    profile_before = dict(session.input.memory_before.profile_snapshot)
    for turn in sorted(session.turns, key=lambda item: item.seq):
        current = [
            ConversationMessage(
                role="user",
                content=message.content,
                attachments=message.attachments or [],
            )
            for message in turn.input.customer_messages
        ]
        if history is not None:
            base = list(history[: turn.history_length or 0])
        else:
            base = list(prior_messages)
        account_id = f"{account_prefix}-{session.session_id}-{turn.seq}-{repeat_index}"
        tasks.append(
            TurnTask(
                run_id=run_id,
                session_id=session.session_id,
                turn_id=turn.turn_id,
                seq=turn.seq,
                repeat_index=repeat_index,
                account_id=account_id,
                profile_before=dict(profile_before),
                messages=[*base, *current],
                expected=turn.expected,
                rubric=turn.rubric,
                bot_user_id=bot_user_id,
                im_bot_id=im_bot_id,
            )
        )
        prior_messages.extend(current)
        prior_messages.extend(
            ConversationMessage(
                role="assistant",
                content=message.content,
                attachments=message.attachments or [],
            )
            for message in turn.expected.messages
        )
        for field, update in turn.expected.profile_update.items():
            profile_before[field] = update.value
    return tasks


def assert_current_metric_policy() -> None:
    assert REQUIRED_METRIC_IDS == ("1", "2", "3", "4", "5", "6", "8", "9")
