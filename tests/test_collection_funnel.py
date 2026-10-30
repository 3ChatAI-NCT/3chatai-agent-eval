from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from golden_eval.application.collection import (
    CorpusCollector,
    count_human_reply_turns,
    count_publishable_turns,
    first_customer_message_at,
    session_stage_reasons,
)
from golden_eval.domain.corpus import (
    CollectionPolicy,
    ExclusionReason,
    SenderRole,
    SessionCandidate,
    SourceMessage,
)

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def message(seq: int, role: SenderRole, minutes: int, content: str = "text") -> SourceMessage:
    return SourceMessage(
        seq=seq, role=role, content=content, sent_at=BASE + timedelta(minutes=minutes)
    )


def test_consecutive_customer_messages_form_one_turn() -> None:
    messages = [
        message(1, SenderRole.VISITOR, 0),
        message(2, SenderRole.VISITOR, 1),
        message(3, SenderRole.AI, 2),
    ]

    assert count_publishable_turns(messages) == 1
    assert count_human_reply_turns(messages) == 0
    assert first_customer_message_at(messages) == BASE


def test_human_reply_turn_is_not_publishable() -> None:
    messages = [
        message(1, SenderRole.VISITOR, 0),
        message(2, SenderRole.AI, 1),
        message(3, SenderRole.VISITOR, 2),
        message(4, SenderRole.HUMAN, 3),
        message(5, SenderRole.AI, 4),
        message(6, SenderRole.VISITOR, 5),
        message(7, SenderRole.AI, 6),
    ]

    assert count_publishable_turns(messages) == 2
    assert count_human_reply_turns(messages) == 1


def test_unanswered_customer_turn_is_not_publishable() -> None:
    messages = [
        message(1, SenderRole.VISITOR, 0),
        message(2, SenderRole.AI, 1),
        message(3, SenderRole.VISITOR, 2),
    ]

    # 只有客户消息、Agent 没回的 Turn 没有可对照的金标回复，不算可发布。
    assert count_publishable_turns(messages) == 1


def test_system_message_breaks_the_turn() -> None:
    messages = [
        message(1, SenderRole.VISITOR, 0),
        message(2, SenderRole.SYSTEM, 1),
        message(3, SenderRole.AI, 2),
    ]

    # SYSTEM 打断后，客户那句没有回复、后面的 AI 变成 unsolicited → 没有可发布 Turn。
    assert count_publishable_turns(messages) == 0
    assert count_human_reply_turns(messages) == 0


def candidate(**overrides) -> SessionCandidate:
    payload = {
        "workspace_id": "ws-1",
        "session_id": "GD-TEST-0001",
        "updated_at": BASE,
        "session_type": "single",
        "subject_type": "VISITOR",
        "chat_type": "single",
        "channel_type": "web",
        "channel_source_type": "web",
        "visitor_found": True,
        "bot_user_id": "bot-1",
        "im_bot_id": "im-1",
    }
    payload.update(overrides)
    return SessionCandidate.model_validate(payload)


def test_session_stage_reasons_are_reported_together() -> None:
    policy = CollectionPolicy(bot_user_id="bot-expected")

    assert session_stage_reasons(candidate(), policy) == [
        ExclusionReason.BOT_USER_MISMATCH
    ]
    assert session_stage_reasons(
        candidate(session_type="group", channel_type="sandbox"), policy
    ) == [
        ExclusionReason.BOT_USER_MISMATCH,
        ExclusionReason.GROUP_CHAT,
        ExclusionReason.SANDBOX,
    ]
    assert session_stage_reasons(
        candidate(visitor_found=False, bot_user_id=None), policy
    ) == [ExclusionReason.VISITOR_MISSING]
    assert session_stage_reasons(candidate(bot_user_id="bot-expected"), policy) == []




def test_scanning_continues_past_excluded_sessions_until_quota_is_filled() -> None:
    """A fixed-size fetch would return short datasets whenever the newest sessions are filtered."""

    from golden_eval.domain.corpus import SessionCandidate, SessionHistory

    class FakeSource:
        mode = "fixture"

        def __init__(self) -> None:
            self.consumed = 0

        def list_workspace_ids(self) -> list[str]:
            return ["ws-1"]

        def iter_candidates(self, workspace_id: str, policy):
            rows = [
                ("excluded-1", "group", None),
                ("excluded-2", "single", "sandbox"),
                ("usable-1", "single", None),
                ("usable-2", "single", None),
            ]
            for session_id, session_type, channel_type in rows:
                self.consumed += 1
                yield SessionCandidate(
                    workspace_id=workspace_id,
                    session_id=session_id,
                    updated_at=datetime(2026, 9, 1, tzinfo=UTC),
                    session_type=session_type,
                    channel_type=channel_type or "web",
                    channel_source_type=channel_type or "web",
                    visitor_found=True,
                    bot_user_id="bot-1",
                    im_bot_id="im-1",
                )

        def hydrate(self, candidate: SessionCandidate) -> SessionHistory:
            messages = [
                message(index * 2 + 1, SenderRole.VISITOR, index * 2, "customer")
                for index in range(3)
            ]
            messages += [
                message(index * 2 + 2, SenderRole.AI, index * 2 + 1, "agent")
                for index in range(3)
            ]
            return SessionHistory(
                session_id=candidate.session_id,
                complete=True,
                expected_message_total=len(messages),
                messages=sorted(messages, key=lambda item: item.seq),
            )

        def current_configuration(self, workspace_id: str):
            from golden_eval.domain.corpus import WorkspaceConfiguration

            return WorkspaceConfiguration(
                workspace_id=workspace_id,
                config_hash="fake",
                captured_at=datetime(2026, 9, 1, tzinfo=UTC),
            )

    source = FakeSource()
    snapshot = CorpusCollector(source).collect(
        "snap-scan", CollectionPolicy(quota_per_workspace=2, min_turns=1)
    )

    workspace = snapshot.workspaces[0]
    assert [item.session_id for item in workspace.sessions if item.included] == [
        "usable-1",
        "usable-2",
    ]
    # Three sessions had to be inspected (two excluded, one usable) before the quota was in reach.
    assert source.consumed == 4
    assert workspace.counts.scanned == 4




def test_generic_contact_filters_are_evaluated_on_known_candidate_fields() -> None:
    """本地能判定的联系人字段按同一套逻辑符复核；本地拿不到的字段交给平台条件查询。"""

    policy = CollectionPolicy(
        contact_filters=[
            {"field": "botUserId", "operator": "LIKE", "value": "bot"},
            {"field": "imBotId", "operator": "NE", "value": "im-other"},
            {"field": "phoneNumber", "operator": "LIKE", "value": "138"},
        ]
    )

    assert session_stage_reasons(candidate(), policy) == []
    assert session_stage_reasons(candidate(bot_user_id="other-1"), policy) == [
        ExclusionReason.BOT_USER_MISMATCH
    ]
    assert session_stage_reasons(candidate(im_bot_id="im-other"), policy) == [
        ExclusionReason.CONTACT_FILTER_MISMATCH
    ]


def test_legacy_bot_user_id_payload_still_builds_a_filter() -> None:
    policy = CollectionPolicy.model_validate({"bot_user_id": "bot-legacy", "min_turns": 2})

    assert [item.model_dump(mode="json") for item in policy.contact_filters] == [
        {"scope": "contact", "field": "botUserId", "operator": "EQ", "value": "bot-legacy"}
    ]
    assert session_stage_reasons(candidate(), policy) == [ExclusionReason.BOT_USER_MISMATCH]


def test_filters_on_fields_the_index_does_not_return_are_left_to_the_platform() -> None:
    """索引行不回传的字段（如 subjectType）本地取不到值，不能据此把会话判为不符合。"""

    policy = CollectionPolicy(
        contact_filters=[
            {"scope": "session", "field": "subjectType", "operator": "EQ", "value": "VISITOR"},
            {"scope": "session", "field": "channelId.type", "operator": "NE", "value": "sandbox"},
        ]
    )
    invisible = candidate(subject_type=None, channel_type=None)

    assert session_stage_reasons(invisible, policy) == []


@pytest.mark.parametrize("scan_limit, expected_count, expected_scanned", [
    (None, 50, 1029),
    (1000, 21, 1000),
])
def test_low_eligible_rate_does_not_stop_default_collection_at_1000(
    scan_limit, expected_count, expected_scanned,
) -> None:
    from golden_eval.domain.corpus import SessionHistory, WorkspaceConfiguration

    class Source:
        def iter_candidates(self, workspace_id, policy):
            for index in range(1050):
                yield candidate(session_id=f"S-{index}")

        def hydrate(self, current):
            index = int(current.session_id.removeprefix("S-"))
            turn_count = 3 if index < 21 or index >= 1000 else 1
            messages = [
                message(turn * 2 + offset + 1, role, turn * 2 + offset)
                for turn in range(turn_count)
                for offset, role in enumerate((SenderRole.VISITOR, SenderRole.AI))
            ]
            return SessionHistory(session_id=current.session_id, complete=True,
                                  expected_message_total=len(messages), messages=messages)

        def current_configuration(self, workspace_id):
            return WorkspaceConfiguration(workspace_id=workspace_id, config_hash="fixture",
                                          captured_at=BASE)

    settings = {"workspace_ids": ["ws-1"], "start": BASE,
                "end": BASE + timedelta(days=62), "quota_per_workspace": 50}
    if scan_limit is not None:
        settings["max_scanned_sessions"] = scan_limit
    snapshot = CorpusCollector(Source()).collect("low-eligible-rate", CollectionPolicy(**settings))
    assert snapshot.session_count == expected_count
    assert snapshot.counts.scanned == expected_scanned
    assert snapshot.counts.quota_shortfall == 50 - expected_count
