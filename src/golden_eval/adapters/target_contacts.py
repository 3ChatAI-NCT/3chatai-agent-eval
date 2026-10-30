"""Target-side contact lifecycle: create visitor, verify routing, converse, read profile back.

Endpoint paths come from the verified reference implementation: `/api/open/v4/butler/*` for the
open API (Bearer) and `/api/butler/visitor/*` for internal reads (tenant identity header). Both
header families are sent when available, because the open path is documented as Bearer-protected
while the internal reads are tenant-scoped.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urlsplit

from ..domain.models import ConversationMessage, ConversationReply, RunIdentity
from ..integrations.platform_http import (
    HttpTransport,
    PlatformHTTPError,
    UrllibTransport,
    ensure_business_ok,
)

CHAT_MESSAGES = "/api/open/v4/butler/chat/messages"
MESSAGE_HISTORY = "/api/butler/session/agent/message/history"
VISITOR_CREATE = "/api/open/v4/butler/visitor/create"
VISITOR_UPDATE = "/api/butler/visitor/updateVisitor"
VISITOR_BY_ACCOUNT = "/api/butler/visitor/get/byAccountId"

STANDARD_CONTACT_FIELDS = frozenset(
    {"name", "email", "phoneNumber", "avatar", "language", "remark"}
)


class PlatformTargetContact:
    """Talks to the real test Workspace."""

    target = "platform"

    def __init__(
        self,
        *,
        base_url: str,
        identity: RunIdentity,
        token: str | None = None,
        transport: HttpTransport | None = None,
        contact_field_map: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._identity = identity
        self._token = token
        self._transport = transport or UrllibTransport()
        self._contact_field_map = contact_field_map or {}

    # ------------------------------------------------------------------ lifecycle

    def create_contact(self, *, account_id: str, profile: dict) -> str | None:
        # Parity with the reference implementation: seed only the fields the golden actually has;
        # platform built-ins go on the top level, everything else into `extendMap`. Inventing a
        # contact name (or writing empty strings) would show the tested Agent a profile that does
        # not exist in production and would leak straight into the profile readback.
        payload: dict[str, Any] = {"accountId": account_id}
        custom: dict[str, Any] = {}
        for key, value in profile.items():
            if value in (None, "", []):
                continue
            if key in STANDARD_CONTACT_FIELDS:
                payload[self._contact_field_map.get(key, key)] = value
            else:
                custom[self._contact_field_map.get(key, key)] = value
        if custom:
            payload["extendMap"] = custom
        body = self._post(VISITOR_CREATE, payload)
        data = _data(body)
        if isinstance(data, dict):
            return str(data.get("id") or data.get("visitorId") or "") or None
        return None

    def update_routing(
        self, *, visitor_id: str | None, account_id: str, bot_user_id: str, im_bot_id: str
    ) -> None:
        payload: dict[str, Any] = {"accountId": account_id}
        if visitor_id:
            payload["id"] = visitor_id
        if bot_user_id:
            payload["botUserId"] = bot_user_id
        if im_bot_id:
            payload["imBotId"] = im_bot_id
        self._post(VISITOR_UPDATE, payload)

    def read_contact(self, *, account_id: str, timeout_seconds: float | None = None) -> dict:
        body = self._get(VISITOR_BY_ACCOUNT, {"accountId": account_id}, timeout_seconds)
        data = _data(body)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        return {}

    def send_messages(
        self, *, account_id: str, messages: list[ConversationMessage],
        request_id: str | None = None,
    ) -> ConversationReply:
        # Message ids must be stable across retries of the same Turn, and timestamps must be
        # strictly increasing so the current input can never sort before the history it follows.
        base_ms = int(time.time() * 1000)
        payload = {
            "userId": account_id,
            "async": False,
            "messages": [
                {
                    "id": _message_id(account_id, index, message.role),
                    "role": message.role,
                    "content": message.content,
                    "createdAt": base_ms + index,
                    **_attachment_payload(message),
                }
                for index, message in enumerate(messages, start=1)
            ],
        }
        # 指定了 request_id 时只用在对话请求上：网关把它透传到这一轮的每次模型调用日志，
        # 建联系人、写路由、回读画像仍各用随机 ID，避免混进模型调用链路
        echoed: dict[str, str] = {}
        headers = self._headers() | ({"request_id": request_id} if request_id else {})
        body = self._transport.post(
            f"{self._base_url}{CHAT_MESSAGES}", headers=headers, payload=payload,
            response_headers=echoed,
        )
        ensure_business_ok(body, CHAT_MESSAGES)
        data = _data(body)
        dialog_id = str(data.get("dialogId") or "") if isinstance(data, dict) else ""
        agent_logs = _agent_logs(data)
        transport = "sync"
        if not agent_logs and dialog_id:
            # The reply and its execution log arrive on the same call; if a Workspace ever returns
            # the log only on the session history, read it back once instead of dropping the
            # evidence. A failed readback must not turn a delivered reply into an error.
            try:
                agent_logs = self._history_agent_logs(dialog_id, base_ms)
                transport = "history_readback" if agent_logs else "sync_without_logs"
            except PlatformHTTPError:
                transport = "sync_without_logs"
        return ConversationReply(
            messages=_reply_messages(body),
            dialog_id=dialog_id or None,
            agent_logs=agent_logs,
            chat_transport=transport,
            request_id_echoed=echoed.get("request_id") or None,
        )

    # ------------------------------------------------------------------ internals

    def _history_agent_logs(self, dialog_id: str, after_ms: int) -> list[dict[str, Any]]:
        """Fallback read: the same AgentLogs, attached to the assistant messages of the session."""

        body = self._post(MESSAGE_HISTORY, {"sessionId": dialog_id, "length": 50})
        data = _data(body)
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return []
        logs: list[dict[str, Any]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get("senderType") or "") not in {"3", "assistant"}:
                continue
            created = item.get("createdTime") or item.get("sendTime") or 0
            try:
                if int(created) and int(created) < after_ms:
                    continue
            except (TypeError, ValueError):
                pass
            raw = item.get("agentLogs")
            if isinstance(raw, list):
                logs.extend(entry for entry in raw if isinstance(entry, dict))
        return logs

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

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        body = self._transport.post(
            f"{self._base_url}{path}", headers=self._headers(), payload=payload
        )
        return ensure_business_ok(body, path)

    def _get(self, path: str, params: dict[str, Any], timeout_seconds: float | None = None) -> Any:
        body = self._transport.get(
            f"{self._base_url}{path}", headers=self._headers(), params=params,
            **({"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}),
        )
        return ensure_business_ok(body, path)


class FixtureTargetContact:
    """Deterministic local target for demos and tests; never calls the platform."""

    target = "fixture"

    def __init__(self) -> None:
        self.contacts: dict[str, dict[str, Any]] = {}

    def create_contact(self, *, account_id: str, profile: dict) -> str | None:
        visitor_id = f"fixture-visitor-{len(self.contacts) + 1}"
        self.contacts[account_id] = {
            "id": visitor_id,
            "accountId": account_id,
            **{key: value for key, value in profile.items()},
        }
        return visitor_id

    def update_routing(
        self, *, visitor_id: str | None, account_id: str, bot_user_id: str, im_bot_id: str
    ) -> None:
        contact = self.contacts.setdefault(account_id, {"accountId": account_id})
        contact["botUserId"] = bot_user_id
        contact["imBotId"] = im_bot_id

    def read_contact(self, *, account_id: str, timeout_seconds: float | None = None) -> dict:
        return dict(self.contacts.get(account_id, {}))

    def send_messages(
        self, *, account_id: str, messages: list[ConversationMessage],
        request_id: str | None = None,
    ) -> ConversationReply:
        last_user = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        return ConversationReply(
            messages=[
                ConversationMessage(role="assistant", content=f"[fixture] 收到：{last_user}")
            ],
            chat_transport="fixture",
            request_id_echoed=request_id,
        )


def _message_id(account_id: str, index: int, role: str) -> str:
    """Deterministic id: the same Turn always produces the same message ids."""

    return f"{account_id}-{index:04d}-{role}"


def _attachment_payload(message: ConversationMessage) -> dict[str, Any]:
    """Media must travel with the message: the tested Agent cannot resolve a bare label.

    The reference implementation sends `{name, type, url, size}` and the platform fetches the URL
    itself, so the original CDN link is sent — not our local evidence copy.
    """

    if not message.attachments:
        return {}
    return {
        "attachments": [
            {
                "name": attachment.name,
                "type": attachment.content_type,
                "url": attachment.url,
                "size": attachment.size,
            }
            for attachment in message.attachments
        ]
    }


def _data(body: Any) -> Any:
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _agent_logs(data: Any) -> list[dict[str, Any]]:
    """The chat reply carries the Agent's own execution log (`agentLogs`)."""

    if not isinstance(data, dict):
        return []
    raw = data.get("agentLogs")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _reply_messages(body: Any) -> list[ConversationMessage]:
    data = _data(body)
    candidates: Any = data
    if isinstance(data, dict):
        candidates = data.get("messages") or data.get("replies") or []
    replies: list[ConversationMessage] = []
    if isinstance(candidates, list):
        for item in candidates:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "assistant")
            content = item.get("content")
            attachments = _reply_attachments(item)
            if content is None and not attachments:
                continue
            replies.append(
                ConversationMessage(
                    role="assistant" if role != "user" else "user",
                    content=str(content or ""),
                    attachments=attachments,
                )
            )
    return replies


def _reply_attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Media the platform sent with the reply; the frontend renders image previews from it."""

    raw = item.get("attachments")
    if not isinstance(raw, list):
        raw = []
    attachments: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        content_type = str(entry.get("contentType") or entry.get("content_type") or "")
        # 平台部分图片只带 type=image 或文件后缀；预览仍由媒体代理校验真实响应类型。
        if content_type in {"", "application/octet-stream"}:
            extension = urlsplit(url).path.rsplit(".", 1)[-1].lower()
            content_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                            "webp": "image/webp", "gif": "image/gif", "avif": "image/avif",
                            "bmp": "image/bmp"}.get(extension, "application/octet-stream")
        attachments.append(
            {
                "name": str(entry.get("name") or "attachment"),
                "content_type": content_type,
                "url": url,
                "size": entry.get("size") if isinstance(entry.get("size"), int) else None,
            }
        )
    return attachments
