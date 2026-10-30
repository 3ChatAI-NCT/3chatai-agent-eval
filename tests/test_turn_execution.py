from __future__ import annotations

import time
from typing import Any

import pytest

from golden_eval.adapters.target_contacts import (
    CHAT_MESSAGES,
    MESSAGE_HISTORY,
    VISITOR_CREATE,
    VISITOR_UPDATE,
    FixtureTargetContact,
    PlatformTargetContact,
    _reply_messages,
)
from golden_eval.application.turn_execution import ProfileReadbackPolicy, execute_turn
from golden_eval.domain.models import (
    ContactLifecycleStage,
    ConversationMessage,
    ObservationStatus,
    RunIdentity,
    TurnTask,
)
from golden_eval.integrations.platform_http import PlatformHTTPError

TENANT = "ws-target-1"

# Real executions observe a full profile window; unit tests use a short window.
FAST_READBACK = ProfileReadbackPolicy(
    timeout_seconds=0.05, poll_seconds=0.01, min_wait_seconds=0
)


class FakePlatform:
    """Canned platform target: records calls, can be told to break routing."""

    def __init__(
        self,
        *,
        break_routing: bool = False,
        agent_logs: list[dict[str, Any]] | None = None,
        history_agent_logs: list[dict[str, Any]] | None = None,
        echo: str | None = "same",
        chat_network_error: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._echo = echo
        self._chat_network_error = chat_network_error
        self._break_routing = break_routing
        self._agent_logs = agent_logs or []
        self._history_agent_logs = history_agent_logs or []

    def post(
        self, url: str, *, headers: dict[str, str], payload: dict[str, Any],
        response_headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append(("POST", url, headers | {"__payload": payload}))
        if url.endswith(VISITOR_CREATE):
            return {"code": 200, "data": {"id": "visitor-1", "accountId": payload["accountId"]}}
        if url.endswith(VISITOR_UPDATE):
            self._last_routing = payload
            return {"code": 200, "data": {}}
        if url.endswith(CHAT_MESSAGES):
            if self._chat_network_error:
                raise PlatformHTTPError(url, None, "timeout after 45s")
            if response_headers is not None and self._echo:
                response_headers["request_id"] = (
                    headers["request_id"] if self._echo == "same" else self._echo
                )
            return {
                "code": 200,
                "data": {
                    "dialogId": "dialog-1",
                    "messages": [{"role": "ai", "content": "你好，这里是门店信息。"}],
                    "agentLogs": self._agent_logs,
                },
            }
        if url.endswith(MESSAGE_HISTORY):
            return {
                "code": 200,
                "data": {
                    "messages": [
                        {
                            "id": "m-1",
                            "senderType": 3,
                            "createdTime": int(time.time() * 1000),
                            "content": "你好，这里是门店信息。",
                            "agentLogs": self._history_agent_logs,
                        }
                    ]
                },
            }
        return {"code": 200, "data": {}}

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        self.calls.append(("GET", url, headers | {"__params": params or {}}))
        routing = getattr(self, "_last_routing", {}) or {}
        return {
            "code": 200,
            "data": {
                "id": "visitor-1",
                "accountId": (params or {}).get("accountId"),
                "botUserId": "other-bot" if self._break_routing else routing.get("botUserId"),
                "imBotId": routing.get("imBotId"),
                "extendMap": {"city": "成都"},
            },
        }


def identity() -> RunIdentity:
    return RunIdentity(tenant_id=TENANT, staff_id="7", request_id="req-1", source="platform_header")


@pytest.mark.parametrize("code", [403, "403", 500, "500"])
def test_chat_business_error_is_not_a_successful_empty_reply(code):
    class RejectedChat(FakePlatform):
        def post(self, url, **kwargs):
            if url.endswith(CHAT_MESSAGES):
                self.calls.append(("POST", url, kwargs))
                return {"code": code, "message": "platform rejected chat", "data": None}
            return super().post(url, **kwargs)

    transport = RejectedChat()
    target = PlatformTargetContact(
        base_url="https://platform.example.com", identity=identity(), transport=transport)
    observed = execute_turn(
        turn_task(), target=target, execution_target="platform", readback=FAST_READBACK,
        bot_user_id="bot-1", im_bot_id="im-1")
    expected = ObservationStatus.EXECUTION_ERROR if int(code) == 403 else (
        ObservationStatus.DELIVERY_UNKNOWN)
    assert observed.status == expected
    assert observed.error_code == f"platform_http_{code}"
    assert observed.error_detail == "platform rejected chat"
    assert observed.response_messages == []
    assert sum(url.endswith(CHAT_MESSAGES) for _, url, _ in transport.calls) == 1


@pytest.mark.parametrize("operation", ["create", "route", "read"])
def test_contact_business_error_stops_before_chat(operation):
    class RejectedContact(FakePlatform):
        def post(self, url, **kwargs):
            if ((operation == "create" and url.endswith(VISITOR_CREATE))
                    or (operation == "route" and url.endswith(VISITOR_UPDATE))):
                return {"code": "403", "message": "contact forbidden", "data": None}
            return super().post(url, **kwargs)

        def get(self, url, **kwargs):
            return {"code": "403", "message": "contact forbidden", "data": None}

    transport = RejectedContact()
    target = PlatformTargetContact(
        base_url="https://platform.example.com", identity=identity(), transport=transport)
    observed = execute_turn(
        turn_task(), target=target, execution_target="platform", readback=FAST_READBACK,
        bot_user_id="bot-1", im_bot_id="im-1")
    assert observed.status == ObservationStatus.EXECUTION_ERROR
    assert observed.error_code == "platform_http_403"
    assert not any(url.endswith(CHAT_MESSAGES) for _, url, _ in transport.calls)


def test_successful_empty_reply_remains_a_valid_observation():
    class SilentChat(FakePlatform):
        def post(self, url, **kwargs):
            if url.endswith(CHAT_MESSAGES):
                return {"code": 200, "data": {"messages": [], "agentLogs": []}}
            return super().post(url, **kwargs)

    target = PlatformTargetContact(
        base_url="https://platform.example.com", identity=identity(), transport=SilentChat())
    observed = execute_turn(
        turn_task(), target=target, execution_target="platform", readback=FAST_READBACK,
        bot_user_id="bot-1", im_bot_id="im-1")
    assert observed.status == ObservationStatus.COMPLETED
    assert observed.response_messages == []


def test_reply_image_without_mime_keeps_previewable_type():
    reply = _reply_messages({"data": {"messages": [{"content": "guide.jpg", "attachments": [
        {"url": "https://assets.example.com/guide.jpg", "type": "image"},
        {"url": "https://assets.example.com/manual.pdf"},
    ]}]}})[0]
    assert reply.attachments[0].content_type == "image/jpeg"
    assert reply.attachments[1].content_type == "application/octet-stream"


def turn_task(**overrides) -> TurnTask:
    payload = {
        "run_id": "run-1",
        "session_id": "GD-TARGET-0001",
        "turn_id": "GD-TARGET-0001#T1",
        "seq": 1,
        "repeat_index": 1,
        "account_id": "gde-run-1-1-1",
        "profile_before": {"city": "成都"},
        "messages": [{"role": "user", "content": "你们门店在哪"}],
        "expected": {
            "messages": [{"seq": 1, "type": "text", "content": "在成都"}],
            "profile_update": {},
            "profile_snapshot": {"city": "成都"},
            "state": None,
            "tool_calls": [],
        },
        "rubric": "回答门店位置",
        "bot_user_id": "unbound",
        "im_bot_id": "unbound",
    }
    payload.update(overrides)
    return TurnTask.model_validate(payload)


def test_platform_target_sends_bearer_and_tenant_headers() -> None:
    transport = FakePlatform()
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="secret-token",
        transport=transport,
    )

    target.create_contact(account_id="gde-1", profile={"city": "成都"})

    _, url, headers = transport.calls[0]
    assert url.endswith(VISITOR_CREATE)
    assert headers["Tenant_id"] == TENANT
    assert headers["staff_id"] == "7"
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["__payload"]["accountId"] == "gde-1"
    assert headers["__payload"]["extendMap"] == {"city": "成都"}


def test_contact_seed_matches_the_reference_shape() -> None:
    """平台内置字段走顶层、空值不写入、不凭空给联系人起名字。"""

    transport = FakePlatform()
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    target.create_contact(
        account_id="gde-1",
        profile={"name": "", "phoneNumber": "13800000000", "city": "成都", "empty": ""},
    )

    payload = transport.calls[0][2]["__payload"]
    assert payload == {
        "accountId": "gde-1",
        "phoneNumber": "13800000000",
        "extendMap": {"city": "成都"},
    }


def test_conversation_payload_carries_attachments() -> None:
    """图片必须以附件形式随消息发出；只发“图片附件：xxx”标签等于让被测 Agent 看不到图。"""

    transport = FakePlatform()
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )
    messages = [
        ConversationMessage(role="user", content="历史文本"),
        ConversationMessage(
            role="user",
            content="图片附件：image-1",
            attachments=[
                {
                    "name": "image-1",
                    "content_type": "image/*",
                    "url": "https://cdn.example.com/a.jpg",
                    "size": 2375,
                    "stored_url": "/api/v1/objects/aa/a.jpg",
                    "sha256": "aa",
                    "stored": True,
                }
            ],
        ),
    ]

    target.send_messages(account_id="gde-1", messages=messages)

    payload = _chat_payload(transport)
    assert "attachments" not in payload["messages"][0]
    assert payload["messages"][1]["attachments"] == [
        {
            "name": "image-1",
            "type": "image/*",
            "url": "https://cdn.example.com/a.jpg",
            "size": 2375,
        }
    ]


def test_execute_turn_completes_full_lifecycle() -> None:
    transport = FakePlatform()
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    observation = execute_turn(
        turn_task(),
        target=target,
        execution_target="platform",
        profile_keys=["city"],
        bot_user_id="bot-1",
        im_bot_id="im-1",
        readback=FAST_READBACK,
    )

    assert observation.status is ObservationStatus.COMPLETED
    assert observation.stage_reached is ContactLifecycleStage.PROFILE_SETTLED
    assert observation.routing_verified is True
    assert observation.bot_user_id == "bot-1"
    assert observation.im_bot_id == "im-1"
    assert observation.response_messages[0].content == "你好，这里是门店信息。"
    assert observation.profile_observed_before == {"city": "成都"}
    assert observation.error_code is None
    # The immediate snapshot is always kept next to the settled one, and the readback says whether
    # the platform stopped changing.
    assert observation.profile_immediate_after == {"city": "成都"}
    assert observation.profile_readback["strategy"] == "bounded_poll"
    assert observation.profile_readback["attempts"] >= 1
    assert observation.profile_after == {"city": "成都"}
    methods = [call[0] for call in transport.calls]
    assert methods[:4] == ["POST", "POST", "GET", "POST"]  # create, route, verify, converse
    assert set(methods[4:]) == {"GET", "POST"}  # one history log fallback + bounded polling
    assert [call[1] for call in transport.calls if call[1].endswith(MESSAGE_HISTORY)]
    assert observation.chat_transport == "sync_without_logs"


def test_routing_mismatch_stops_before_sending_the_conversation() -> None:
    transport = FakePlatform(break_routing=True)
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    observation = execute_turn(
        turn_task(),
        target=target,
        execution_target="platform",
        profile_keys=["city"],
        bot_user_id="bot-1",
        im_bot_id="im-1",
        readback=FAST_READBACK,
    )

    assert observation.status is ObservationStatus.EXECUTION_ERROR
    assert observation.stage_reached is ContactLifecycleStage.CREATED
    assert observation.error_code == "contact_lifecycle_error"
    assert "routing verification failed" in (observation.error_detail or "")
    assert not [call for call in transport.calls if call[1].endswith(CHAT_MESSAGES)]


def test_default_readback_confirms_a_delayed_profile_write(monkeypatch):
    from types import SimpleNamespace

    from golden_eval.application.turn_execution import _settle_profile

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds

    target = SimpleNamespace(read_contact=lambda **_: {"extendMap": {"city": "成都"}})
    after, meta = _settle_profile(
        target, account_id="test", keys=["city"], baseline={"city": ""},
        policy=ProfileReadbackPolicy(), sleep=sleep)
    assert after == {"city": "成都"}
    assert meta["settled"] is True
    assert meta["attempts"] >= 3


def test_delayed_routing_readback_waits_without_recreating_or_resending(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class DelayedRouting(FakePlatform):
        reads = 0

        def get(self, *args, **kwargs):
            result = super().get(*args, **kwargs)
            self.reads += 1
            if self.reads <= 2:
                result["data"].pop("botUserId")
                result["data"].pop("imBotId")
            return result

    transport = DelayedRouting()
    target = PlatformTargetContact(base_url="https://platform.example.com", identity=identity(),
                                   transport=transport)
    observed = execute_turn(turn_task(), target=target, execution_target="platform",
        bot_user_id="bot-1", im_bot_id="im-1", readback=ProfileReadbackPolicy(timeout_seconds=0),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    assert observed.status is ObservationStatus.COMPLETED
    assert observed.routing_verified is True
    assert clock[0] > 0
    for path in [VISITOR_CREATE, VISITOR_UPDATE, CHAT_MESSAGES]:
        assert len([call for call in transport.calls if call[1].endswith(path)]) == 1


def test_missing_routing_remains_bounded_and_never_sends_chat(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    transport = FakePlatform()

    class MissingRouting(PlatformTargetContact):
        def read_contact(self, *, account_id, timeout_seconds=None):
            return {"accountId": account_id}

    target = MissingRouting(base_url="https://platform.example.com", identity=identity(),
                            transport=transport)
    observed = execute_turn(turn_task(), target=target, execution_target="platform",
        bot_user_id="bot-1", im_bot_id="im-1", readback=FAST_READBACK,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    assert observed.error_code == "contact_lifecycle_error"
    assert 0 < clock[0] <= 5
    assert not [call for call in transport.calls if call[1].endswith(CHAT_MESSAGES)]


@pytest.mark.parametrize("http_status", [502, 504, None])
def test_chat_gateway_failure_retains_delivery_uncertainty(http_status):
    class GatewayFailure(FakePlatform):
        def post(self, url, **kwargs):
            if url.endswith(CHAT_MESSAGES):
                self.calls.append(("POST", url, {}))
                raise PlatformHTTPError(url, http_status, "gateway request failed")
            return super().post(url, **kwargs)

    transport = GatewayFailure()
    target = PlatformTargetContact(base_url="https://platform.example.com", identity=identity(),
                                   transport=transport)
    observed = execute_turn(turn_task(), target=target, execution_target="platform",
        bot_user_id="bot-1", im_bot_id="im-1", readback=FAST_READBACK)
    assert observed.status is ObservationStatus.DELIVERY_UNKNOWN
    assert observed.chat_transport == "sync_delivery_unknown"
    assert len([call for call in transport.calls if call[1].endswith(CHAT_MESSAGES)]) == 1


def test_changing_profile_does_not_claim_the_settled_stage():
    class ChangingPlatform(FakePlatform):
        reads = 0

        def get(self, *args, **kwargs):
            result = super().get(*args, **kwargs)
            self.reads += 1
            if self.reads > 1:
                result["data"]["extendMap"]["city"] = str(self.reads)
            return result

    observed = _replayed(ChangingPlatform())
    assert observed.status is ObservationStatus.COMPLETED
    assert observed.profile_readback["settled"] is False
    assert observed.stage_reached is ContactLifecycleStage.RESPONSE_OBSERVED


def test_chat_payload_has_monotonic_timestamps_and_stable_ids() -> None:
    transport = FakePlatform()
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )
    messages = [
        ConversationMessage(role="user", content="历史一"),
        ConversationMessage(role="assistant", content="历史二"),
        ConversationMessage(role="user", content="当前问题"),
    ]

    target.send_messages(account_id="gde-1", messages=messages)
    first = _chat_payload(transport)
    transport.calls.clear()
    target.send_messages(account_id="gde-1", messages=messages)
    second = _chat_payload(transport)

    stamps = [item["createdAt"] for item in first["messages"]]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert [item["id"] for item in first["messages"]] == [
        item["id"] for item in second["messages"]
    ]
    assert first["async"] is False


def _chat_payload(transport: FakePlatform) -> dict[str, Any]:
    _, url, headers = next(call for call in transport.calls if call[1].endswith(CHAT_MESSAGES))
    assert url.endswith(CHAT_MESSAGES)
    return headers["__payload"]


def test_execute_turn_skips_routing_when_not_configured() -> None:
    observation = execute_turn(
        turn_task(),
        target=FixtureTargetContact(),
        execution_target="fixture",
        profile_keys=["city"],
        readback=FAST_READBACK,
    )

    assert observation.status is ObservationStatus.COMPLETED
    assert observation.execution_target == "fixture"
    assert observation.routing_verified is False
    assert len(observation.response_messages) == 1


def test_platform_turn_without_any_routing_fails_before_creating_a_contact() -> None:
    transport = FakePlatform()
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    observation = execute_turn(
        turn_task(),
        target=target,
        execution_target="platform",
        profile_keys=["city"],
        readback=FAST_READBACK,
    )

    assert observation.status is ObservationStatus.EXECUTION_ERROR
    assert observation.stage_reached is ContactLifecycleStage.PLANNED
    assert observation.error_code == "contact_routing_missing"
    assert transport.calls == []


@pytest.mark.parametrize("target_channel,unexpected_bot,expected", [
    ("openApi", None, ObservationStatus.COMPLETED),
    ("chatly", None, ObservationStatus.EXECUTION_ERROR),
    (None, None, ObservationStatus.EXECUTION_ERROR),
    ("openApi", "other-bot", ObservationStatus.EXECUTION_ERROR),
])
def test_verified_openapi_source_requires_matching_target_route(
    target_channel, unexpected_bot, expected, monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    class OpenAPIPlatform(FakePlatform):
        def get(self, *args, **kwargs):
            response = super().get(*args, **kwargs)
            response["data"].update({"channelId.type": target_channel,
                                     "botUserId": unexpected_bot})
            return response

    transport = OpenAPIPlatform()
    target = PlatformTargetContact(base_url="https://platform.example.com",
        identity=identity(), token="t", transport=transport)
    task = turn_task()
    evidence = {"session_id": task.session_id, "channel_type": "openApi",
                "visitor_found": True, "bot_user_id": None, "im_bot_id": None}
    observed = execute_turn(task, target=target, execution_target="platform",
        profile_keys=["city"], readback=FAST_READBACK, routing_evidence=evidence,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert observed.status == expected
    assert observed.routing_evidence == evidence
    assert observed.routing_verified is (expected is ObservationStatus.COMPLETED)
    assert not any(url.endswith(VISITOR_UPDATE) for _, url, _ in transport.calls)
    assert sum(url.endswith(CHAT_MESSAGES) for _, url, _ in transport.calls) == (
        1 if expected is ObservationStatus.COMPLETED else 0)


@pytest.mark.parametrize("final_route,expected,wait_seconds", [
    ({"channelId.type": "openApi"}, ObservationStatus.COMPLETED, 1.0),
    ({}, ObservationStatus.EXECUTION_ERROR, 5.0),
    ({"channelId.type": "chatly"}, ObservationStatus.EXECUTION_ERROR, 1.0),
    ({"botUserId": "unexpected-bot"}, ObservationStatus.EXECUTION_ERROR, 1.0),
])
def test_openapi_missing_channel_readback_is_bounded_and_rejects_explicit_mismatch(
    final_route, expected, wait_seconds, monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class DelayedOpenAPIPlatform(FakePlatform):
        def get(self, *args, **kwargs):
            result = super().get(*args, **kwargs)
            result["data"].update({"botUserId": None, "imBotId": None})
            if clock[0] >= 1.0:
                result["data"].update(final_route)
            return result

    transport = DelayedOpenAPIPlatform()
    task = turn_task()
    observed = execute_turn(task, target=PlatformTargetContact(
        base_url="https://platform.example.com", identity=identity(), transport=transport),
        execution_target="platform", readback=ProfileReadbackPolicy(timeout_seconds=0),
        routing_evidence={"session_id": task.session_id, "channel_type": "openApi",
                          "visitor_found": True, "bot_user_id": None, "im_bot_id": None},
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    assert observed.status == expected
    assert clock[0] == wait_seconds
    assert sum(url.endswith(VISITOR_CREATE) for _, url, _ in transport.calls) == 1
    assert not any(url.endswith(VISITOR_UPDATE) for _, url, _ in transport.calls)
    assert sum(url.endswith(CHAT_MESSAGES) for _, url, _ in transport.calls) == (
        1 if expected is ObservationStatus.COMPLETED else 0)


@pytest.mark.parametrize("changes", [
    {"session_id": "another-session"}, {"channel_type": "chatly"},
    {"visitor_found": False}, {"bot_user_id": "source-bot"},
])
def test_missing_bot_route_is_not_allowed_by_unverified_source_metadata(changes):
    task = turn_task()
    evidence = {"session_id": task.session_id, "channel_type": "openApi",
                "visitor_found": True, "bot_user_id": None, "im_bot_id": None} | changes
    transport = FakePlatform()
    observed = execute_turn(task, target=PlatformTargetContact(
        base_url="https://platform.example.com", identity=identity(), transport=transport),
        execution_target="platform", readback=FAST_READBACK, routing_evidence=evidence)
    assert observed.error_code == "contact_routing_missing"
    assert transport.calls == []












OBSERVED_TOOLS = [
    {"type": "task", "name": "门店信息检索", "costTime": 1200},
    {"type": "language", "message": "简体中文", "status": "certain"},
    {"type": "agent", "name": "mainagent"},
    {"type": "knowledge", "name": None},
]


def _platform_observation(**kwargs: Any):
    transport = FakePlatform(**kwargs)
    observation = execute_turn(
        turn_task(
            expected={
                "messages": [{"seq": 1, "type": "text", "content": "在成都"}],
                "profile_update": {},
                "profile_snapshot": {"city": "成都"},
                "state": None,
                "tool_calls": [{"name": "门店信息检索"}, {"name": "平台知识库检索"}],
            }
        ),
        target=PlatformTargetContact(
            base_url="https://platform.example.com",
            identity=identity(),
            token="t",
            transport=transport,
        ),
        execution_target="platform",
        profile_keys=["city"],
        bot_user_id="bot-1",
        im_bot_id="im-1",
        readback=FAST_READBACK,
    )
    return observation, transport


def test_agent_logs_on_the_reply_become_the_62_diagnostic() -> None:
    observation, _ = _platform_observation(agent_logs=OBSERVED_TOOLS)

    assert observation.chat_transport == "sync"
    diagnostic = observation.tool_diagnostics
    assert diagnostic is not None
    assert diagnostic.expected_tool_names == ["平台知识库检索", "门店信息检索"]
    assert diagnostic.actual_tool_names == ["平台知识库检索", "门店信息检索"]
    assert diagnostic.name_set_match is True
    assert [log.type for log in observation.agent_logs] == [
        "task",
        "language",
        "agent",
        "knowledge",
    ]
    assert observation.raw_agent_logs[0]["name"] == "门店信息检索"


def test_missing_execution_log_falls_back_to_the_session_history() -> None:
    observation, transport = _platform_observation(history_agent_logs=OBSERVED_TOOLS)

    assert observation.chat_transport == "history_readback"
    assert observation.tool_diagnostics is not None
    assert observation.tool_diagnostics.actual_tool_names == ["平台知识库检索", "门店信息检索"]
    assert [call[1] for call in transport.calls if call[1].endswith(MESSAGE_HISTORY)]


def test_reply_without_agent_logs_still_records_the_missing_evidence() -> None:
    observation, _ = _platform_observation()

    assert observation.status is ObservationStatus.COMPLETED
    assert observation.chat_transport == "sync_without_logs"
    assert observation.tool_diagnostics is not None
    # 没有日志就是不匹配，但绝不因此判失败：6.2 是诊断，不进 Gate。
    assert observation.tool_diagnostics.actual_tool_names == []
    assert observation.tool_diagnostics.missing_tool_names == [
        "平台知识库检索",
        "门店信息检索",
    ]


def _replayed(transport: FakePlatform):
    target = PlatformTargetContact(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )
    return execute_turn(
        turn_task(),
        target=target,
        execution_target="platform",
        profile_keys=["city"],
        bot_user_id="bot-1",
        im_bot_id="im-1",
        readback=FAST_READBACK,
        replay_request_id="a" * 32,
    )


def test_replay_request_id_goes_only_on_the_chat_call_and_is_recorded() -> None:
    """模型调用链路按它到网关日志关联：只能出现在对话请求上，其余请求不能混进同一个 ID。"""

    transport = FakePlatform()
    observation = _replayed(transport)

    chat = [headers for _, url, headers in transport.calls if url.endswith(CHAT_MESSAGES)]
    others = [headers for _, url, headers in transport.calls if not url.endswith(CHAT_MESSAGES)]
    assert [headers["request_id"] for headers in chat] == ["a" * 32]
    assert others and all(headers["request_id"] != "a" * 32 for headers in others)
    assert observation.replay_request_id == "a" * 32
    assert observation.replay_request_id_echoed == "a" * 32


def test_gateway_echo_is_kept_as_returned() -> None:
    assert _replayed(FakePlatform(echo="b" * 32)).replay_request_id_echoed == "b" * 32
    assert _replayed(FakePlatform(echo=None)).replay_request_id_echoed is None


def test_delivery_unknown_turn_still_records_the_replay_request_id() -> None:
    observation = _replayed(FakePlatform(chat_network_error=True))

    assert observation.status is ObservationStatus.DELIVERY_UNKNOWN
    assert observation.replay_request_id == "a" * 32
    assert observation.replay_request_id_echoed is None


def test_fixture_target_echoes_the_replay_request_id() -> None:
    observation = execute_turn(
        turn_task(),
        target=FixtureTargetContact(),
        execution_target="fixture",
        readback=ProfileReadbackPolicy(0, 0, 0),
        replay_request_id="c" * 32,
    )

    assert observation.replay_request_id_echoed == "c" * 32


@pytest.mark.parametrize("restored", ["", "重庆"])
def test_profile_restore_mismatch_does_not_send_chat(monkeypatch, restored):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class MissingSeed(FixtureTargetContact):
        sent = False

        def read_contact(self, **kwargs):
            contact = super().read_contact(**kwargs)
            return {**contact, "city": restored}

        def send_messages(self, **kwargs):
            self.sent = True
            return super().send_messages(**kwargs)

    target = MissingSeed()
    observed = execute_turn(turn_task(), target=target, execution_target="platform",
        bot_user_id="bot-1", readback=ProfileReadbackPolicy(0, 0, 0),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert observed.error_code == "profile_restore_mismatch"
    assert observed.status == ObservationStatus.EXECUTION_ERROR
    assert observed.profile_observed_before == {"city": restored}
    assert target.sent is False
    assert clock[0] == (5 if not restored else 0)


def test_profile_restore_retries_missing_and_keeps_full_readback_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class DelayedSeed(FixtureTargetContact):
        sent_at = None
        reads = 0

        def read_contact(self, **kwargs):
            self.reads += 1
            contact = super().read_contact(**kwargs)
            return {**contact, "city": "" if self.reads == 1 else " 成都 "}

        def send_messages(self, **kwargs):
            self.sent_at = clock[0]
            return super().send_messages(**kwargs)

    target = DelayedSeed()
    observed = execute_turn(turn_task(), target=target, execution_target="platform",
        bot_user_id="bot-1", sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert observed.error_code is None
    assert target.sent_at == 0.5
    assert clock[0] - target.sent_at == 90
    assert observed.profile_readback["elapsed_ms"] == 90000


@pytest.mark.parametrize("expected,actual", [(199, "199"), (False, "false"), (" 成都 ", "成都")])
def test_profile_restore_normalises_platform_scalars(expected, actual):
    class NormalisedSeed(FixtureTargetContact):
        def read_contact(self, **kwargs):
            return {**super().read_contact(**kwargs), "city": actual}

    observed = execute_turn(turn_task(profile_before={"city": expected}),
        target=NormalisedSeed(), execution_target="platform", bot_user_id="bot-1",
        readback=ProfileReadbackPolicy(0, 0, 0))
    assert observed.error_code is None
    assert observed.response_messages


@pytest.mark.parametrize("first_change", [False, True])
def test_readback_does_not_finish_before_late_async_profile_write(monkeypatch, first_change):
    from types import SimpleNamespace

    from golden_eval.application.turn_execution import _settle_profile

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds

    def read_contact(**_):
        return {"extendMap": {
            "city": "成都" if first_change and clock[0] >= 12 else "",
            "special_need_c": "局部改造" if clock[0] >= 75 else "",
        }}

    after, meta = _settle_profile(
        SimpleNamespace(read_contact=read_contact), account_id="test",
        keys=["city", "special_need_c"], baseline={"city": "", "special_need_c": ""},
        policy=ProfileReadbackPolicy(), sleep=sleep)
    assert after == {"city": "成都" if first_change else "", "special_need_c": "局部改造"}
    assert meta["settled"] is True
    assert meta["timeout_seconds"] == 90
    assert meta["completion_verified"] is False
    assert clock[0] == meta["timeout_seconds"]


def test_readback_late_read_failure_does_not_reuse_earlier_stability(monkeypatch):
    from types import SimpleNamespace

    from golden_eval.application.turn_execution import _settle_profile

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    policy = ProfileReadbackPolicy()

    def sleep(seconds):
        clock[0] += seconds

    def read_contact(**_):
        if clock[0] >= policy.timeout_seconds - policy.poll_seconds:
            raise PlatformHTTPError("profile-read", 503, "temporarily unavailable")
        return {"extendMap": {"city": "成都"}}

    after, meta = _settle_profile(
        SimpleNamespace(read_contact=read_contact), account_id="test", keys=["city"],
        baseline={"city": "成都"}, policy=policy, sleep=sleep)
    assert after == {"city": "成都"}
    assert meta["settled"] is False
    assert meta["errors"]


def test_readback_network_wait_uses_remaining_budget(monkeypatch):
    from types import SimpleNamespace

    from golden_eval.application.turn_execution import _settle_profile

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds

    def read_contact(*, timeout_seconds=None, **_):
        assert timeout_seconds is not None
        clock[0] += timeout_seconds
        raise PlatformHTTPError("read", None, "timeout")

    _, meta = _settle_profile(
        SimpleNamespace(read_contact=read_contact), account_id="test", keys=["city"],
        baseline={"city": ""}, policy=ProfileReadbackPolicy(), sleep=sleep,
    )
    assert clock[0] == 90
    assert meta["settled"] is False


def test_initial_profile_read_failure_preserves_reply_and_retries():
    from golden_eval.adapters.target_contacts import FixtureTargetContact

    class ProfileReadFailure(FixtureTargetContact):
        sent = False
        failed_once = False

        def send_messages(self, **kwargs):
            self.sent = True
            return super().send_messages(**kwargs)

        def read_contact(self, **kwargs):
            if self.sent and not self.failed_once:
                self.failed_once = True
                raise PlatformHTTPError("read", 503, "temporary")
            return super().read_contact(**kwargs)

    observed = execute_turn(turn_task(), target=ProfileReadFailure(),
        execution_target="fixture", readback=FAST_READBACK)
    assert observed.status == "completed"
    assert observed.response_messages
    assert observed.profile_immediate_after is None
    assert observed.profile_after is not None
    assert observed.profile_readback["errors"]


@pytest.mark.parametrize("change_at,fail_at,expected_end", [
    (None, None, 30), (24, None, 36), (None, 27, 42),
])
def test_stable_readback_waits_minimum_and_resets_on_changes_or_errors(
        monkeypatch, change_at, fail_at, expected_end):
    from types import SimpleNamespace

    from golden_eval.application.turn_execution import _settle_profile

    clock = [0.0]
    reads = []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def read_contact(*, timeout_seconds, **_):
        reads.append(clock[0])
        assert timeout_seconds <= 3
        if clock[0] == fail_at:
            raise PlatformHTTPError("read", 503, "temporary")
        return {"extendMap": {"city": "成都" if change_at and clock[0] >= change_at else ""}}

    after, meta = _settle_profile(
        SimpleNamespace(read_contact=read_contact), account_id="test", keys=["city"],
        baseline={"city": ""}, policy=ProfileReadbackPolicy(90, 3, 30, 12),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert reads[0] == 3  # 最小等待期间也持续读取，才能验证稳定窗口。
    assert clock[0] == expected_end
    assert after == {"city": "成都" if change_at else ""}
    assert meta["elapsed_ms"] == expected_end * 1000
    assert meta["exit_reason"] == "stable_window"
    assert meta["settled"] is True and meta["completion_verified"] is False
    assert bool(meta["errors"]) == (fail_at is not None)


def test_unstable_readback_reaches_deadline_without_claiming_stability(monkeypatch):
    from types import SimpleNamespace

    from golden_eval.application.turn_execution import _settle_profile

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    after, meta = _settle_profile(
        SimpleNamespace(read_contact=lambda **_: {"extendMap": {"city": str(clock[0])}}),
        account_id="test", keys=["city"], baseline={"city": ""},
        policy=ProfileReadbackPolicy(90, 3, 30, 12),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert clock[0] == 90
    assert after == {"city": "87.0"}
    assert meta["exit_reason"] == "deadline"
    assert meta["settled"] is False and meta["completion_verified"] is False
