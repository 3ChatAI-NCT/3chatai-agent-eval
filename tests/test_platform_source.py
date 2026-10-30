from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from golden_eval.adapters.platform_source import (
    MCP_TOOLS_CONFIG,
    MESSAGE_HISTORY,
    SESSION_GET,
    SESSION_QUERY,
    VISITOR_BY_IDS,
    PlatformCorpusSource,
)
from golden_eval.application.collection import CorpusCollector
from golden_eval.domain.corpus import CollectionPolicy, SenderRole
from golden_eval.domain.models import (
    ConnectionUpdateRequest,
    RunIdentity,
)
from golden_eval.integrations.platform_http import PlatformHTTPError

TENANT = "ws-tenant-1"
STAFF = "42"


@pytest.mark.parametrize("session_exists,visitor_exists", [(True, True), (True, False),
                                                           (False, True)])
def test_routing_evidence_checks_exact_session_and_excludes_private_fields(
    session_exists, visitor_exists,
):
    class RoutingTransport:
        def post(self, url, *, headers, payload):
            assert headers["Tenant_id"] == TENANT
            if url.endswith(SESSION_GET):
                assert payload == {"id": "S-1"}
                return {"data": {"id": "S-1" if session_exists else "other-session",
                    "type": "p2p", "visitorId": "V-1",
                    "channel": {"type": "openApi", "token": "must-not-be-copied"}}}
            assert url.endswith(VISITOR_BY_IDS) and payload == {"ids": ["V-1"]}
            return {"data": [{"id": "V-1", "name": "private-name"}]
                    if visitor_exists else []}

    source = PlatformCorpusSource(base_url="https://platform.example.com", identity=RunIdentity(
        tenant_id=TENANT, source="connection_settings"), transport=RoutingTransport())
    if not session_exists:
        with pytest.raises(ValueError, match="source_session_unavailable"):
            source.routing_evidence("S-1")
        return
    evidence = source.routing_evidence("S-1")
    assert evidence["session_id"] == "S-1" and evidence["source_workspace_id"] == TENANT
    assert evidence["channel_type"] == "openApi"
    assert evidence["visitor_found"] == visitor_exists
    assert evidence["bot_user_id"] is None and evidence["im_bot_id"] is None
    assert "must-not-be-copied" not in json.dumps(evidence)
    assert "private-name" not in json.dumps(evidence)

IDENTITY_CONNECTION = {
    "name": "线上空间",
    "purpose": "source_readonly",
    "auth_mode": "platform_identity",
    "base_url": "https://platform.example.com",
    "workspace_id": TENANT,
    "staff_id": STAFF,
}


class FakeTransport:
    """Records calls and serves canned platform payloads; never touches the network."""

    def __init__(self, *, history_total: int | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self._history_total = history_total

    def post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        self.calls.append((url, headers, payload))
        if url.endswith(SESSION_QUERY):
            return {
                "code": 200,
                "data": [
                    {
                        "id": "S-2",
                        "type": "single",
                        "subjectType": "VISITOR",
                        "visitorId": "V-1",
                        "updatedTime": 1_760_000_000_000,
                        "channel": {"type": "web", "sourceType": "web"},
                    },
                    {
                        "id": "S-1",
                        "type": "group",
                        "subjectType": "GROUP",
                        "visitorId": "V-2",
                        "updatedTime": 1_759_000_000_000,
                        "channel": {"type": "sandbox", "sourceType": "sandbox"},
                    },
                    {
                        "id": "S-3",
                        "type": "single",
                        "subjectType": "VISITOR",
                        "visitorId": "V-404",
                        "updatedTime": 1_758_000_000_000,
                        "channel": {"type": "web", "sourceType": "web"},
                    },
                ],
            }
        if url.endswith(VISITOR_BY_IDS):
            return {
                "code": 200,
                "data": [
                    {"id": "V-1", "botUserId": "bot-1", "imBotId": "im-1", "chatType": "single"},
                    {"id": "V-2", "botUserId": "bot-1", "imBotId": "im-1", "chatType": "group"},
                ],
            }
        if url.endswith(MESSAGE_HISTORY):
            if payload["sessionId"] == "S-1":
                return {"code": 200, "data": {"messages": [], "messageTotal": 0}}
            return {
                "code": 200,
                "data": {
                    "messages": [
                        _message(
                            "M-3",
                            3,
                            "agent 1",
                            1_700_000_200_000,
                            agent_logs=[
                                {"type": "task", "name": "Demo-欢迎问候", "costTime": 1200},
                                {"type": "agent", "name": "mainagent"},
                                {"type": "knowledge", "name": None},
                            ],
                        ),
                        _message("M-1", 1, "customer 1", 1_700_000_000_000),
                        _message("M-2", 4, "third party", 1_700_000_100_000),
                        _message("M-4", 1, "customer 2", 1_700_000_300_000),
                        _message("M-5", 3, "agent 2", 1_700_000_400_000),
                        _message("M-6", 1, "customer 3", 1_700_000_500_000),
                        _message("M-7", 3, "agent 3", 1_700_000_600_000),
                    ],
                    "messageTotal": self._history_total or 7,
                },
            }
        return {"code": 200, "data": {}}

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any]
    ) -> Any:
        self.calls.append((url, headers, params))
        if url.endswith(MCP_TOOLS_CONFIG):
            return {
                "code": 200,
                "data": [
                    {"id": "T-1", "oldname": "配置企微标签", "description": "写标签"},
                    {"id": "T-2", "name": "无 oldname 的工具", "description": "按 name 回退"},
                ],
            }
        return {"code": 200, "data": {}}


def _message(
    message_id: str, sender_type: int, content: str, created: int, *, agent_logs: list | None = None
) -> dict:
    payload = {
        "id": message_id,
        "senderType": sender_type,
        "content": content,
        "createdTime": created,
    }
    if agent_logs is not None:
        payload["agentLogs"] = agent_logs
    return payload


def identity() -> RunIdentity:
    return RunIdentity(
        tenant_id=TENANT, staff_id=STAFF, request_id="req-1", source="platform_header"
    )


def open_api_token(
    *,
    tenant_id: str = TENANT,
    staff_id: str | None = STAFF,
    issuer: str = "open-api",
) -> str:
    """Build a synthetic OpenAPI JWT: signature is irrelevant for claim derivation."""

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=")
    claims: dict[str, Any] = {"iss": issuer, "tenantId": tenant_id}
    if staff_id is not None:
        claims["staffId"] = staff_id
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"






def build_source(transport: FakeTransport) -> PlatformCorpusSource:
    return PlatformCorpusSource(
        base_url="https://platform.example.com", identity=identity(), transport=transport
    )




def test_discover_maps_platform_rows_and_sends_tenant_headers() -> None:
    transport = FakeTransport()

    candidates = list(
        build_source(transport).iter_candidates(
            TENANT, CollectionPolicy(quota_per_workspace=5)
        )
    )

    assert [item.session_id for item in candidates] == ["S-2", "S-1", "S-3"]
    first = candidates[0]
    assert first.session_type == "single"
    assert first.channel_type == "web"
    assert first.visitor_found is True
    assert first.bot_user_id == "bot-1"
    assert first.im_bot_id == "im-1"
    assert candidates[1].session_type == "group"
    assert candidates[1].channel_source_type == "sandbox"
    assert candidates[1].chat_type == "group"
    assert candidates[2].visitor_found is False

    url, headers, body = next(call for call in transport.calls if call[0].endswith(SESSION_QUERY))
    assert headers["Tenant_id"] == TENANT
    assert headers["staff_id"] == STAFF
    assert headers["request_id"] == "req-1"
    assert "Authorization" not in headers
    # 查询必须带创建时间窗：平台对没有时间界的会话查询会退化成全表扫描（实测会 504）。
    assert body["start"] == 0 and body["length"] == 100
    assert body["endCreatedTime"] > body["startCreatedTime"] > 0


def test_bearer_mode_source_sends_authorization_header() -> None:
    transport = FakeTransport()
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="bearer-token-1",
        transport=transport,
    )

    list(source.iter_candidates(TENANT, CollectionPolicy(quota_per_workspace=5)))

    _, headers, _ = next(call for call in transport.calls if call[0].endswith(SESSION_QUERY))
    assert headers["Authorization"] == "Bearer bearer-token-1"
    assert headers["Tenant_id"] == TENANT


def test_discover_applies_bot_user_filter_condition() -> None:
    transport = FakeTransport()

    list(
        build_source(transport).iter_candidates(
            TENANT, CollectionPolicy(bot_user_id="bot-9")
        )
    )

    body = next(call[2] for call in transport.calls if call[0].endswith(SESSION_QUERY))
    condition = body["condition"]["conditions"][0]
    assert condition["key"] == "visitorId.botUserId"
    assert condition["value"] == "bot-9"


def test_contact_filters_become_platform_conditions() -> None:
    """字段由调用方填：默认拼成 visitorId.<字段>，点分路径原样传递，多条件按「且」组合。"""

    transport = FakeTransport()
    policy = CollectionPolicy(
        contact_filters=[
            {"field": "botUserId", "operator": "EQ", "value": "bot-9"},
            {"field": "name", "operator": "LIKE", "value": "张"},
            {"field": "phoneNumber", "operator": "ISNULL"},
            {"field": "tag", "operator": "IN", "value": "vip, trial"},
            {"scope": "session", "field": "subjectType", "operator": "EQ", "value": "VISITOR"},
            {"field": "channelId.type", "operator": "NE", "value": "sandbox"},
        ]
    )

    list(build_source(transport).iter_candidates(TENANT, policy))

    body = next(call[2] for call in transport.calls if call[0].endswith(SESSION_QUERY))
    assert body["condition"]["logic"] == "and"
    assert body["condition"]["conditions"] == [
        {"key": "visitorId.botUserId", "operator": "EQ", "value": "bot-9"},
        {"key": "visitorId.name", "operator": "LIKE", "value": "张"},
        {"key": "visitorId.phoneNumber", "operator": "ISNULL"},
        {"key": "visitorId.tag", "operator": "IN", "value": ["vip", "trial"]},
        {"key": "subjectType", "operator": "EQ", "value": "VISITOR"},
        {"key": "channelId.type", "operator": "NE", "value": "sandbox"},
    ]


def test_contact_filter_requires_a_value_unless_the_operator_says_otherwise() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CollectionPolicy(contact_filters=[{"field": "name", "operator": "EQ", "value": " "}])

    empty_ok = CollectionPolicy(contact_filters=[{"field": "name", "operator": "NOTNULL"}])
    assert empty_ok.contact_filters[0].operator.value == "NOTNULL"


def test_hydrate_orders_messages_and_maps_roles() -> None:
    source = build_source(FakeTransport())
    candidate = list(
        source.iter_candidates(TENANT, CollectionPolicy(quota_per_workspace=5))
    )[0]

    history = source.hydrate(candidate)

    assert history.complete is True
    assert [message.seq for message in history.messages] == list(range(1, 8))
    assert [message.content for message in history.messages] == [
        "customer 1",
        "third party",
        "agent 1",
        "customer 2",
        "agent 2",
        "customer 3",
        "agent 3",
    ]
    assert history.messages[0].role is SenderRole.VISITOR
    assert history.messages[1].role is SenderRole.THIRD_PARTY
    assert history.messages[2].role is SenderRole.AI


def test_hydrate_marks_incomplete_when_platform_reports_more_messages() -> None:
    source = build_source(FakeTransport(history_total=99))
    candidate = list(
        source.iter_candidates(TENANT, CollectionPolicy(quota_per_workspace=5))
    )[0]

    assert source.hydrate(candidate).complete is False


class PagedHistoryTransport:
    """Mimics the real `/agent/message/history` contract.

    Pages are newest-first, the server silently caps `length` below what the adapter asks for,
    messages with empty content *and* no attachments are dropped from the payload but still
    counted by `messageTotal`, and the endpoint returns `data: null` once nothing older exists.
    """

    def __init__(
        self,
        *,
        total: int,
        page_cap: int,
        empty_created_times: set[int] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self._empty = empty_created_times or set()
        # createdTime == index; the newest message therefore has the largest index.
        self._messages = [
            {
                "id": f"M-{created}",
                "senderType": 1 if created % 2 == 0 else 3,
                "content": f"msg-{created}",
                "createdTime": created,
            }
            for created in range(total)
        ]
        self._page_cap = page_cap

    def post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        self.calls.append((url, headers, payload))
        if url.endswith(SESSION_QUERY):
            return {
                "code": 200,
                "data": [
                    {
                        "id": "S-1",
                        "type": "single",
                        "subjectType": "VISITOR",
                        "visitorId": "V-1",
                        "updatedTime": 2_000_000_000_000,
                        "channel": {"type": "web", "sourceType": "web"},
                    }
                ],
            }
        if url.endswith(VISITOR_BY_IDS):
            return {
                "code": 200,
                "data": [
                    {
                        "id": "V-1",
                        "botUserId": "bot-1",
                        "imBotId": "im-1",
                        "chatType": "single",
                    }
                ],
            }
        if url.endswith(MESSAGE_HISTORY):
            before = payload.get("beforeCreatedTime")
            matching = [
                message
                for message in self._messages
                if before is None or message["createdTime"] <= before - 1
            ]
            page = sorted(
                matching, key=lambda message: message["createdTime"], reverse=True
            )[: self._page_cap]
            kept = [
                message
                for message in page
                if message["createdTime"] not in self._empty
            ]
            if not kept:
                return {"code": 200, "data": None}
            return {
                "code": 200,
                "data": {"messages": kept, "messageTotal": len(matching)},
            }
        return {"code": 200, "data": {}}


def test_hydrate_walks_to_the_earliest_message_past_short_pages() -> None:
    transport = PagedHistoryTransport(
        total=2600, page_cap=1000, empty_created_times={2400, 2500}
    )
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        transport=transport,
        history_page_size=2000,
    )
    candidate = list(
        source.iter_candidates(TENANT, CollectionPolicy(quota_per_workspace=5))
    )[0]

    history = source.hydrate(candidate)

    # 服务端把单页压到 1000 条，又按空正文过滤掉 2 条；修复前会把它当成“已经到尽头”。
    assert [message.content for message in history.messages[:2]] == ["msg-0", "msg-1"]
    assert len(history.messages) == 2600 - 2
    assert history.complete is True
    history_calls = [call for call in transport.calls if call[0].endswith(MESSAGE_HISTORY)]
    assert len(history_calls) > 1


def test_platform_source_drives_the_funnel() -> None:
    snapshot = CorpusCollector(build_source(FakeTransport())).collect(
        "snap-platform", CollectionPolicy(min_turns=2)
    )

    counts = snapshot.counts
    assert counts.scanned == 3
    assert counts.included == 1
    assert counts.excluded["group_chat"] == 1
    assert counts.excluded["sandbox"] == 1
    assert counts.excluded["visitor_missing"] == 1
    assert counts.removed_third_party_messages == 1
    included = [item for item in snapshot.workspaces[0].sessions if item.included]
    assert [item.session_id for item in included] == ["S-2"]
    assert included[0].publishable_turn_count == 3














def test_platform_identity_connection_requires_tenant() -> None:
    with pytest.raises(ValueError) as excinfo:
        ConnectionUpdateRequest.model_validate(
            {
                "purpose": "source_readonly",
                "auth_mode": "platform_identity",
                "base_url": "https://platform.example.com",
            }
        )

    assert "workspace_id" in str(excinfo.value)










def test_online_agent_logs_become_source_tool_calls() -> None:
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=FakeTransport(),
    )
    candidates = source.iter_candidates(TENANT, CollectionPolicy())
    candidate = next(item for item in candidates if item.session_id == "S-2")

    history = source.hydrate(candidate)
    ai_message = next(message for message in history.messages if message.role is SenderRole.AI)

    assert [call.name for call in ai_message.tool_calls] == ["Demo-欢迎问候", "平台知识库检索"]
    assert ai_message.tool_calls[0].log_type == "task"


class ProfileWriteTransport(FakeTransport):
    """同一个会话多带一条 `internalTool: visitor` 的画像写入日志。"""

    def post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        response = super().post(url, headers=headers, payload=payload)
        if not url.endswith(MESSAGE_HISTORY) or payload.get("sessionId") != "S-2":
            return response
        for message in response["data"]["messages"]:
            if message["id"] == "M-7":
                message["agentLogs"] = [
                    {
                        "type": "internalTool",
                        "name": "visitor",
                        "message": 'city: "" -> "成都"',
                        "entity": {"name": "visitor", "values": ['city: "" -> "成都"']},
                    }
                ]
        return response


def test_online_profile_writes_land_on_the_source_message() -> None:
    """平台自己记账的写入随消息冻结进快照；没有写日志的消息保持空。"""

    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=ProfileWriteTransport(),
    )
    candidate = next(item for item in source.iter_candidates(TENANT, CollectionPolicy()))

    history = source.hydrate(candidate)
    by_content = {message.content: message for message in history.messages}

    assert by_content["agent 3"].profile_writes == {"city": "成都"}
    assert by_content["agent 1"].profile_writes == {}


def test_workspace_tool_catalog_is_read_from_the_source_configuration() -> None:
    transport = FakeTransport()
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    tools = source.available_tools(TENANT)

    assert [tool.name for tool in tools] == ["配置企微标签", "无 oldname 的工具"]
    assert [call[0] for call in transport.calls if call[0].endswith(MCP_TOOLS_CONFIG)]


class FakeBusinessErrorTransport(FakeTransport):
    """平台用「HTTP 200 + 业务错误码」表达失败（例如缺鉴权时的 401 鉴权失败）。"""

    def post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        self.calls.append((url, headers, payload))
        return {"code": 401, "ret": False, "message": "鉴权失败", "errorCode": 401}


def test_transport_timeout_names_the_endpoint_and_the_budget(monkeypatch) -> None:
    """超时要报接口和实际预算，不提示未接入的环境变量。"""

    from golden_eval.integrations.platform_http import UrllibTransport

    def raise_timeout(request, timeout=None):  # noqa: ANN001 - 替代 urllib
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
    transport = UrllibTransport(timeout_seconds=45.0)

    with pytest.raises(PlatformHTTPError) as excinfo:
        transport.post(
            "https://platform.example.com/api/butler/session/query",
            headers={},
            payload={},
        )

    assert excinfo.value.status is None
    assert "timeout after 45s" in excinfo.value.detail
    assert "调用方 HTTP 超时配置" in excinfo.value.detail


def test_business_error_is_raised_instead_of_looking_like_an_empty_page() -> None:
    """鉴权失败必须报错；否则采集会"成功"但一条不采。"""

    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=FakeBusinessErrorTransport(),
    )

    with pytest.raises(PlatformHTTPError) as excinfo:
        list(source.iter_candidates(TENANT, CollectionPolicy(quota_per_workspace=1)))

    assert excinfo.value.status == 401
    assert "鉴权失败" in excinfo.value.detail



class SliceAwareTransport(FakeTransport):
    """只在一个时间分片里返回数据，用来验证"配额不足自动往更早翻"。"""

    def __init__(self, *, data_slice_index: int) -> None:
        super().__init__()
        self._data_slice_index = data_slice_index
        self.windows: list[tuple[int, int]] = []

    def post(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        if not url.endswith(SESSION_QUERY):
            return super().post(url, headers=headers, payload=payload)
        self.calls.append((url, headers, payload))
        self.windows.append((payload["startCreatedTime"], payload["endCreatedTime"]))
        if len(self.windows) - 1 != self._data_slice_index:
            return {"code": 200, "data": [], "total": 0}
        return {
            "code": 200,
            "data": [
                {
                    "id": "SLICE-1",
                    "type": "single",
                    "subjectType": "VISITOR",
                    "visitorId": "V-1",
                    "updatedTime": 1_760_000_000_000,
                    "channel": {"type": "web", "sourceType": "web"},
                }
            ],
            "total": 1,
        }


def test_scan_walks_back_until_it_finds_data_or_hits_three_empty_slices() -> None:
    """显式起止区间内由新到旧扫，第 3 个分片才有数据。"""

    transport = SliceAwareTransport(data_slice_index=2)
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    end = datetime.now(UTC)
    start = end - timedelta(days=180)
    candidates = list(
        source.iter_candidates(TENANT, CollectionPolicy(start=start, end=end))
    )

    assert [item.session_id for item in candidates] == ["SLICE-1"]
    # 第 3 片拿到数据后，又扫了 3 个连续空分片才收手。
    assert len(transport.windows) == 6
    # 由新到旧：每个分片的窗口连续且更早
    assert transport.windows[0][1] > transport.windows[1][1] > transport.windows[2][1]
    assert transport.windows[0][0] == transport.windows[1][1]


def test_scan_stops_after_three_consecutive_empty_slices() -> None:
    """从头到尾都是空分片时，连续三个空分片就收手。"""

    transport = SliceAwareTransport(data_slice_index=99)
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )

    end = datetime.now(UTC)
    start = end - timedelta(days=180)
    assert list(
        source.iter_candidates(TENANT, CollectionPolicy(start=start, end=end))
    ) == []
    assert len(transport.windows) == 3


def test_explicit_start_does_not_extend_further_back() -> None:
    transport = SliceAwareTransport(data_slice_index=99)
    source = PlatformCorpusSource(
        base_url="https://platform.example.com",
        identity=identity(),
        token="t",
        transport=transport,
    )
    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=30)

    list(
        source.iter_candidates(
            TENANT,
            CollectionPolicy(start=start, end=end),
        )
    )

    assert min(window[0] for window in transport.windows) >= int(start.timestamp() * 1000)
