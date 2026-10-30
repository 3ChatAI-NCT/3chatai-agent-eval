"""Message shaping rules shared by the source adapter and the golden builder.

Ported from the reference implementation (`codex-golden-builder-v9`) so both projects describe the
same platform messages the same way: original text, media kind from messageType/contentType,
normalized attachments, an evidence-layer content (text + attachment urls) and a golden-layer
content (text, or a readable media label with attachment names).
"""

from __future__ import annotations

import hashlib
from typing import Any

MEDIA_KIND_BY_TYPE: dict[int, str] = {2: "audio", 6: "image", 14: "video"}
MEDIA_LABEL: dict[str, str] = {
    "image": "图片",
    "audio": "语音",
    "video": "视频",
    "attachment": "附件",
}

EMPTY_MESSAGE = "[empty-message]"


def stable_hash(value: str, length: int | None = None) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:length] if length else digest


def normalize_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Platform attachments → the v0.4 message attachment shape (name/content_type/url/size)."""

    raw = message.get("attachments")
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "name": str(item.get("name") or "attachment"),
                "content_type": str(item.get("contentType") or "application/octet-stream"),
                "url": str(item.get("url") or "").strip(),
                "size": item.get("size") if isinstance(item.get("size"), int) else None,
            }
        )
    return normalized


def message_kind(message: dict[str, Any]) -> str:
    """Image/audio/video messages carry an empty `content`; the kind comes from messageType."""

    try:
        message_type = int(message.get("messageType"))
    except (TypeError, ValueError):
        message_type = -1
    if message_type in MEDIA_KIND_BY_TYPE:
        return MEDIA_KIND_BY_TYPE[message_type]
    attachments = normalize_attachments(message)
    if attachments:
        families = {
            attachment["content_type"].split("/", 1)[0].lower()
            for attachment in attachments
        }
        media = families & {"image", "audio", "video"}
        if len(media) == 1:
            return next(iter(media))
        return "attachment"
    return "text"


def message_text(message: dict[str, Any]) -> str:
    return str(message.get("content") or "").strip()


def message_content(message: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    """Evidence-layer content: original text plus attachment urls, never blank."""

    text = message_text(message)
    urls = [str(item.get("url") or "") for item in attachments if item.get("url")]
    if text or urls:
        return "\n".join([part for part in (text, *urls) if part])
    if attachments:
        return f"[{message_kind(message)}:missing-url]"
    return EMPTY_MESSAGE


def golden_content(message: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    """Golden-layer content: original text, else a media label with attachment names."""

    text = message_text(message)
    if text:
        return text
    if attachments:
        label = MEDIA_LABEL.get(message_kind(message), "媒体")
        names = "、".join(str(item.get("name") or "attachment") for item in attachments)
        return f"{label}附件：{names}"
    return message_content(message, attachments)


def golden_content_from_source(message: Any) -> str:
    """Same rule, for the domain `SourceMessage` the golden builder already holds.

    `text` is the original text when the adapter filled it; fixture messages only carry `content`,
    so fall back to it before treating the message as media-only.
    """

    text = str(getattr(message, "text", "") or "").strip()
    if text:
        return text
    attachments = list(getattr(message, "attachments", []) or [])
    if attachments:
        label = MEDIA_LABEL.get(str(getattr(message, "content_type", "")), "媒体")
        names = "、".join(str(item.get("name") or "attachment") for item in attachments)
        return f"{label}附件：{names}"
    return str(getattr(message, "content", "") or "").strip() or EMPTY_MESSAGE


def unreplayable_media(message: dict[str, Any]) -> bool:
    """A media message whose attachments have no url can never be replayed."""

    return message_kind(message) != "text" and any(
        not attachment["url"] for attachment in normalize_attachments(message)
    )
