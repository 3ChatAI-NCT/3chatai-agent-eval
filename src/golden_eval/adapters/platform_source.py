"""Read-only source adapter for the 3Chat platform.

Authentication follows the builder's verified contract: an internal service presents
`Tenant_id: {workspace_id}` (plus `staff_id` for audit on writes). We never forward a user session.
Only read endpoints are implemented here — the target-side write path lives in a later stage.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any

from ..domain.corpus import (
    NO_VALUE_OPERATORS,
    CollectionPolicy,
    ContactFilter,
    FilterOperator,
    FilterScope,
    ObservedToolCall,
    SenderRole,
    SessionCandidate,
    SessionHistory,
    SourceMessage,
    ToolDefinition,
    WorkspaceConfiguration,
)
from ..domain.message_shaping import (  # noqa: F401  (re-exported for callers and tests)
    golden_content,
    message_content,
    message_kind,
    message_text,
    normalize_attachments,
    stable_hash,
    unreplayable_media,
)
from ..domain.models import RunIdentity
from ..domain.profile_writes import profile_writes_from_logs
from ..domain.tool_diagnostics import dedupe_calls
from ..integrations.platform_http import (
    HttpTransport,
    PlatformHTTPError,
    UrllibTransport,
    ensure_business_ok,
)

SESSION_QUERY = "/api/butler/session/query"
SESSION_GET = "/api/butler/session/get"
VISITOR_BY_IDS = "/api/butler/visitor/get/byIds"
MESSAGE_HISTORY = "/api/butler/session/agent/message/history"
GUIDANCE_CONFIG = "/api/butler/open/v2/guidance/config/both"
SKILLS_CONFIG = "/api/butler/open/v2/agent/skills/config/both"
MCP_TOOLS_CONFIG = "/api/butler/internal/mcp/tools"


class PlatformCorpusSource:
    """Implements `CorpusSourcePort` against the platform's read APIs."""

    mode = "platform"

    def __init__(
        self,
        *,
        base_url: str,
        identity: RunIdentity,
        token: str | None = None,
        transport: HttpTransport | None = None,
        page_size: int = 100,
        history_page_size: int = 2000,
        object_store: Any | None = None,
        persist_attachments: bool = True,
        max_attachment_bytes: int = 15_000_000,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._identity = identity
        self._token = token
        self._transport = transport or UrllibTransport()
        self._page_size = page_size
        self._history_page_size = history_page_size
        self._visitor_cache: dict[str, dict[str, Any]] = {}
        # 平台附件是 CDN 链接，可能过期；留证时把字节落到对象存储并记录 stored_url。
        self._object_store = object_store
        self._persist_attachments = persist_attachments and object_store is not None
        self._max_attachment_bytes = max_attachment_bytes
        self._stored_attachments: dict[str, dict[str, Any]] = {}
        # 当前正在扫的时间分片（毫秒），供采集进度显示"已经翻到多早"。
        self._scan_position: tuple[int, int] | None = None

    # ------------------------------------------------------------------ CorpusSourcePort

    @property
    def scan_position(self) -> tuple[int, int] | None:
        return self._scan_position

    def list_workspace_ids(self) -> list[str]:
        return [self._identity.tenant_id]

    def routing_evidence(self, session_id: str) -> dict[str, Any]:
        """只读核对无机器人 ID 的来源会话；仅保留路由字段，不带出联系人或渠道凭据。"""

        body = self._post(SESSION_GET, {"id": session_id})
        row = body.get("data") if isinstance(body, dict) else None
        if not isinstance(row, dict) or str(row.get("id")) != session_id:
            raise ValueError("source_session_unavailable")
        visitor_id = str(row.get("visitorId") or "")
        visitor = self._fetch_visitors([visitor_id]).get(visitor_id) if visitor_id else None
        channel = row.get("channel") if isinstance(row.get("channel"), dict) else {}
        return {"source_workspace_id": self._identity.tenant_id, "session_id": session_id,
                "captured_at": datetime.now(UTC).isoformat(), "channel_type": channel.get("type"),
                "session_type": row.get("type"), "subject_type": row.get("subjectType"),
                "visitor_found": visitor is not None,
                "bot_user_id": (visitor or {}).get("botUserId"),
                "im_bot_id": (visitor or {}).get("imBotId")}

    def iter_candidates(self, workspace_id: str, policy: CollectionPolicy):
        """按时间分片扫描会话索引，最新的分片先给，直到调用方停手。

        平台对**没有时间界**的会话查询会退化成全表扫描：实测同一平台上一个空间 1 秒返回、
        另一个空间 60 秒后直接被网关 504。窗口越大越慢（1 个月 ≈ 25s、2 个月 ≈ 54s、3 个月 504），
        所以这里按 30 天切片、由新到旧地扫；通常第一个分片就够配额，配额不足才继续往前翻。
        """

        seen: set[str] = set()
        conditions = [_filter_condition(item) for item in policy.contact_filters]
        empty_streak = 0
        for lower, upper in _window_slices(policy):
            self._scan_position = (lower, upper)
            offset = 0
            slice_rows = 0
            while True:
                body: dict[str, Any] = {
                    "start": offset,
                    "length": self._page_size,
                    "startCreatedTime": lower,
                    "endCreatedTime": upper,
                }
                if conditions:
                    body["condition"] = {"logic": "and", "conditions": conditions}
                page = self._post(SESSION_QUERY, body)
                rows = _rows(page)
                if not rows:
                    break
                slice_rows += len(rows)
                visitor_ids = [
                    str(row.get("visitorId"))
                    for row in rows
                    if row.get("visitorId") and str(row.get("visitorId")) not in self._visitor_cache
                ]
                if visitor_ids:
                    self._visitor_cache.update(self._fetch_visitors(visitor_ids))
                page_candidates: list[SessionCandidate] = []
                for row in rows:
                    session_id = str(row.get("id") or "")
                    if not session_id or session_id in seen:
                        continue
                    seen.add(session_id)
                    visitor = self._visitor_cache.get(str(row.get("visitorId")))
                    page_candidates.append(_candidate(workspace_id, row, visitor))
                page_candidates.sort(key=lambda item: item.updated_at, reverse=True)
                yield from page_candidates
                offset += len(rows)
                if len(rows) < self._page_size:
                    break
            # 连续三个空分片认为更早的历史也没有了；起止时间是显式给定的，区间内由新到旧扫完。
            empty_streak = empty_streak + 1 if slice_rows == 0 else 0
            if empty_streak >= 3:
                break

    def hydrate(self, candidate: SessionCandidate) -> SessionHistory:
        messages: dict[str, dict[str, Any]] = {}
        expected_total = 0
        before_created_time: int | None = None
        previous_cursor: int | None = None
        reached_end = False
        fingerprints: set[str] = set()
        while True:
            body: dict[str, Any] = {
                "sessionId": candidate.session_id,
                "length": self._history_page_size,
            }
            if before_created_time is not None:
                body["beforeCreatedTime"] = before_created_time
            payload = self._post(MESSAGE_HISTORY, body)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                # 平台返回 null 数据，是“没有更早的消息”的唯一可靠信号；据此停止。
                reached_end = True
                break
            page = data.get("messages") or []
            expected_total = max(expected_total, int(data.get("messageTotal") or 0))
            fingerprint = hashlib.sha256(
                "|".join(str(item.get("id", "")) for item in page).encode("utf-8")
            ).hexdigest()
            if fingerprint in fingerprints:
                break
            fingerprints.add(fingerprint)
            for message in page:
                message_id = str(message.get("id") or "")
                if message_id:
                    messages[message_id] = message
            if not page:
                reached_end = True
                break
            times = [_message_time(item) for item in page]
            numeric = [value for value in times if value is not None]
            if not numeric:
                break
            before_created_time = min(numeric)
            # 平台分页用 `beforeCreatedTime` 表示“严格早于”游标；游标不往前退说明翻页没有
            # 前进，继续只会重复请求，必须停手而不是伪造“已经取到最早”。
            if previous_cursor is not None and before_created_time >= previous_cursor:
                break
            previous_cursor = before_created_time
        ordered = sorted(
            messages.values(),
            key=lambda item: (_message_time(item) or 0, str(item.get("id") or "")),
        )
        # `messageTotal` 会把平台在返回时按“空正文且无附件”过滤掉的消息也算进去，所以不能用
        # `len(messages) == messageTotal` 作为是否完整的判据；走到了平台明确给的“没有更多”才算完整。
        complete = (
            expected_total == 0 or reached_end or len(messages) >= expected_total
        )
        return SessionHistory(
            session_id=candidate.session_id,
            complete=complete,
            expected_message_total=expected_total or len(ordered),
            messages=[
                self._source_message(message, index)
                for index, message in enumerate(ordered, start=1)
            ],
        )

    def _source_message(self, message: dict[str, Any], seq: int) -> SourceMessage:
        attachments = self._persist_attachments_for(message)
        return SourceMessage(
            seq=seq,
            role=_role(message),
            content=message_content(message, attachments),
            text=message_text(message),
            content_type=message_kind(message),
            sent_at=_sent_at(message),
            attachments=attachments,
            tool_calls=_observed_tool_calls(message),
            profile_writes=profile_writes_from_logs(message.get("agentLogs")),
        )

    def _persist_attachments_for(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Copy media bytes into object storage so the evidence outlives the CDN link."""

        attachments = normalize_attachments(message)
        if not self._persist_attachments or not attachments:
            return attachments
        downloader = getattr(self._transport, "get_bytes", None)
        output: list[dict[str, Any]] = []
        for item in attachments:
            item = dict(item)
            url = str(item.get("url") or "")
            family = str(item.get("content_type") or "").split("/")[0].lower()
            if url and family in {"image", "audio", "video"} and downloader is not None:
                cached = self._stored_attachments.get(url)
                if cached is None:
                    try:
                        data, content_type = downloader(
                            url, max_bytes=self._max_attachment_bytes
                        )
                        ref = self._object_store.put(
                            data,
                            content_type=content_type or str(item["content_type"]),
                            name=str(item.get("name") or ""),
                        )
                        cached = {
                            "stored": True,
                            "stored_url": f"/api/v1/objects/{ref}",
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size": len(data),
                        }
                    except Exception:
                        # 平台 CDN 不可达/过大时保留原始链接，并显式标记未留存。
                        cached = {"stored": False}
                    self._stored_attachments[url] = cached
                item.update(cached)
            output.append(item)
        return output

    def current_configuration(self, workspace_id: str) -> WorkspaceConfiguration:
        """Best-effort configuration snapshot; a partial read is still recorded."""

        material: dict[str, Any] = {}
        for name, path in (
            ("guidance", GUIDANCE_CONFIG),
            ("skills", SKILLS_CONFIG),
            ("mcp_tools", MCP_TOOLS_CONFIG),
        ):
            try:
                material[name] = (
                    self._get(path, {})
                    if path == MCP_TOOLS_CONFIG
                    else self._post(path, {"workspaceId": workspace_id})
                )
            except PlatformHTTPError:
                material[name] = None
        digest = hashlib.sha256(
            repr(sorted(material.items())).encode("utf-8")
        ).hexdigest()[:16]
        return WorkspaceConfiguration(
            workspace_id=workspace_id,
            config_hash=f"platform-{digest}",
            captured_at=datetime.now(UTC),
        )

    def available_tools(self, workspace_id: str) -> list[ToolDefinition]:
        """The Workspace's declared callable actions, read from the source configuration surface.

        Golden turns must only reference tools this list contains, so the publisher's contract
        check has something real to validate against. An unavailable read returns an empty list;
        the draft then falls back to the actions actually observed in the collected history.
        """

        try:
            payload = self._get(MCP_TOOLS_CONFIG, {})
        except PlatformHTTPError:
            return []
        rows = _rows(payload)
        tools: list[ToolDefinition] = []
        for item in rows:
            name = str(item.get("oldname") or item.get("name") or "").strip()
            if not name:
                continue
            tools.append(
                ToolDefinition(
                    name=name,
                    description=str(item.get("description") or name),
                    params=[
                        {
                            "name": str(param.get("name")),
                            "type": str(param.get("type") or ""),
                            "required": bool(param.get("required")),
                            "description": str(param.get("description") or ""),
                        }
                        for param in (item.get("collectDataInput") or [])
                        if isinstance(param, dict) and param.get("name")
                    ],
                )
            )
        return tools

    # ------------------------------------------------------------------ internals

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Tenant_id": self._identity.tenant_id,
            "request_id": self._identity.request_id
            or hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:32],
        }
        if self._identity.staff_id:
            headers["staff_id"] = self._identity.staff_id
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return ensure_business_ok(
            self._transport.post(
                f"{self._base_url}{path}", headers=self._headers(), payload=body
            ),
            path,
        )

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        reader = getattr(self._transport, "get", None)
        if reader is None:
            raise PlatformHTTPError(path, None, "transport does not support GET")
        return ensure_business_ok(
            reader(f"{self._base_url}{path}", headers=self._headers(), params=params),
            path,
        )

    def _fetch_visitors(self, visitor_ids: list[str]) -> dict[str, dict[str, Any]]:
        payload = self._post(VISITOR_BY_IDS, {"ids": visitor_ids})
        rows = payload.get("data") if isinstance(payload, dict) else None
        return {
            str(row.get("id")): row
            for row in (rows or [])
            if isinstance(row, dict) and row.get("id")
        }


def _rows(payload: Any) -> list[dict[str, Any]]:
    """平台可能返回 {data: [...]} 或 {data: {records|list: [...]}}。"""

    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        records = data.get("records") or data.get("list") or []
        return [row for row in records if isinstance(row, dict)]
    return []


SLICE_DAYS = 30


def _window_slices(
    policy: CollectionPolicy, *, now_ms: int | None = None
) -> list[tuple[int, int]]:
    """把扫描范围切成由新到旧的 30 天分片，返回 [(start_ms, end_ms), ...]。

    起止时间都显式给定时就只扫这个区间；缺省时 `end` 取当前时刻、`start` 往前推 3 个月。
    """

    moment = now_ms if now_ms is not None else int(time.time() * 1000)
    end = int(policy.end.timestamp() * 1000) if policy.end else moment
    if policy.start:
        start = int(policy.start.timestamp() * 1000)
    else:
        start = end - 3 * 30 * 24 * 3600 * 1000
    step = SLICE_DAYS * 24 * 3600 * 1000
    slices: list[tuple[int, int]] = []
    upper = end
    while upper > start:
        lower = max(start, upper - step)
        slices.append((lower, upper))
        upper = lower
    return slices or [(start, end)]


def _candidate(
    workspace_id: str, row: dict[str, Any], visitor: dict[str, Any] | None
) -> SessionCandidate:
    channel = row.get("channel") if isinstance(row.get("channel"), dict) else {}
    return SessionCandidate(
        workspace_id=workspace_id,
        session_id=str(row.get("id") or ""),
        updated_at=_from_millis(row.get("updatedTime")),
        session_type=str(row.get("type") or ""),
        subject_type=row.get("subjectType"),
        chat_type=(visitor or {}).get("chatType") or row.get("chatType"),
        channel_type=channel.get("type"),
        channel_source_type=channel.get("sourceType"),
        visitor_found=bool(row.get("visitorId")) and visitor is not None,
        bot_user_id=(visitor or {}).get("botUserId"),
        im_bot_id=(visitor or {}).get("imBotId"),
    )


def _role(message: dict[str, Any]) -> SenderRole:
    try:
        return SenderRole(int(message.get("senderType", -1)))
    except ValueError:
        return SenderRole.UNKNOWN


def _filter_condition(item: ContactFilter) -> dict[str, Any]:
    """一条过滤条件 → 平台 `condition.conditions[]`。

    联系人字段拼成 `visitorId.<field>`；`session` 作用域与点分路径按原样传递。
    `ISNULL`/`NOTNULL` 不带值，`IN`/`NIN` 传数组。
    """

    condition: dict[str, Any] = {
        "key": _query_key(item),
        "operator": item.operator.value,
    }
    if item.operator in NO_VALUE_OPERATORS:
        return condition
    if item.operator in {FilterOperator.IN, FilterOperator.NIN}:
        condition["value"] = [
            value.strip() for value in item.value.split(",") if value.strip()
        ]
        return condition
    condition["value"] = item.value.strip()
    return condition


def _query_key(item: ContactFilter) -> str:
    if item.scope is FilterScope.SESSION or "." in item.field:
        return item.field
    return f"visitorId.{item.field}"


def _observed_tool_calls(message: dict[str, Any]) -> list[ObservedToolCall]:
    """Online AgentLogs are the golden side of 6.2; without them no gold turn can require a tool."""

    raw_logs = message.get("agentLogs")
    if not isinstance(raw_logs, list):
        return []
    return [
        ObservedToolCall(
            name=str(call["name"]),
            log_type=str(call.get("log_type") or ""),
            status=call.get("status"),
        )
        for call in dedupe_calls(raw_logs)
    ]


def _message_time(message: dict[str, Any]) -> int | None:
    for key in ("createdTime", "sendTime"):
        value = message.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _sent_at(message: dict[str, Any]) -> datetime:
    value = _message_time(message)
    if value is None:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _from_millis(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tz=UTC)


def _attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    return normalize_attachments(message)
