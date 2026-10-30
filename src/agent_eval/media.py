"""Authenticated draft image previews alongside original source URLs."""

from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException

from golden_eval.application.rubric_draft import NOTE_DRAFT

from .privacy import safe_view

MAX_IMAGE_BYTES = 15_000_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"}


def draft_view(content: dict, draft_id: str) -> dict:
    view = safe_view(content)
    for session in view["items"]:
        for turn_index, turn in enumerate(session["turns"]):
            turn["rubric"] = turn["rubric"].replace(
                "【标注说明】" + NOTE_DRAFT.replace("规则预填", "AI 预填"),
                "【标注说明】" + NOTE_DRAFT,
            )
            for side, messages in (("customer", turn["input"]["customer_messages"]),
                                   ("reference", turn["expected"]["messages"])):
                for message_index, message in enumerate(messages):
                    for attachment_index, attachment in enumerate(message.get("attachments") or []):
                        if str(attachment.get("content_type") or "").startswith("image/"):
                            attachment["preview_url"] = (
                                f"/api/agent-eval/v1/drafts/{quote(draft_id, safe='')}/cases/"
                                f"{quote(session['session_id'], safe='')}/media/"
                                f"{turn_index}/{side}/"
                                f"{message_index}/{attachment_index}"
                            )
    return view


def attachment_at(content, case_id, turn_index, side, message_index, attachment_index):
    if min(turn_index, message_index, attachment_index) < 0:
        raise HTTPException(404, "图片附件不存在")
    try:
        session = next(item for item in content["items"] if item["session_id"] == case_id)
        turn = session["turns"][turn_index]
        messages = (turn["input"]["customer_messages"] if side == "customer"
                    else turn["expected"]["messages"])
        return messages[message_index]["attachments"][attachment_index]
    except (StopIteration, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(404, "图片附件不存在") from exc


def load_image(attachment: dict, allowed_hosts: str = "") -> tuple[bytes, str]:
    url = str(attachment.get("url") or "")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "图片地址不是受支持的媒体来源") from exc
    host = parsed.hostname or ""
    # 仅访问安装者显式配置的精确域名；未配置时禁止向附件来源发起请求。
    allowed = host in {item.strip().lower() for item in allowed_hosts.split(",") if item.strip()}
    if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
            or port not in (None, 443)):
        raise HTTPException(422, "图片地址不是受支持的媒体来源")
    try:
        with httpx.stream("GET", url, timeout=15, follow_redirects=False) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in IMAGE_TYPES:
                raise HTTPException(422, "附件不是可预览的图片")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_IMAGE_BYTES:
                    raise HTTPException(413, "图片超过预览大小限制")
            return bytes(data), content_type
    except httpx.HTTPError as exc:
        raise HTTPException(502, "图片无法读取，来源链接可能已失效") from exc
