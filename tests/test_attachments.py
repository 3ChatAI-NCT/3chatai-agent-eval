from __future__ import annotations

from datetime import UTC, datetime

from golden_eval.adapters.platform_source import (
    golden_content,
    message_content,
    message_kind,
    message_text,
    normalize_attachments,
)
from golden_eval.application.golden_build import build_session
from golden_eval.domain.corpus import SenderRole, SessionOutcome, SourceMessage

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def image_message() -> dict:
    """Shape the platform returns for an image message (verified against the live workspace)."""

    return {
        "id": "m-image",
        "senderType": 1,
        "messageType": 6,
        "content": "",
        "createdTime": 1_760_000_000_000,
        "attachments": [
            {
                "name": "imagetMKHuEmczCJ4Ii4hwm1cE",
                "contentType": "image/*",
                "url": "https://test-media-bucket.oss-cn-beijing.aliyuncs.com/x/y.jpg",
                "size": 3125,
            }
        ],
    }


def test_media_message_gets_kind_attachment_and_labels() -> None:
    message = image_message()
    attachments = normalize_attachments(message)

    assert message_kind(message) == "image"
    assert message_text(message) == ""
    # Evidence layer keeps the url so the message can be replayed; golden layer uses a label.
    assert message_content(message, attachments) == "https://test-media-bucket.oss-cn-beijing.aliyuncs.com/x/y.jpg"
    assert golden_content(message, attachments) == "图片附件：imagetMKHuEmczCJ4Ii4hwm1cE"
    assert attachments == [
        {
            "name": "imagetMKHuEmczCJ4Ii4hwm1cE",
            "content_type": "image/*",
            "url": "https://test-media-bucket.oss-cn-beijing.aliyuncs.com/x/y.jpg",
            "size": 3125,
        }
    ]


def test_captioned_media_keeps_the_caption() -> None:
    message = {
        "messageType": 0,
        "content": "到店先按门店指引完成手机号绑定和人脸录入。",
        "attachments": [{"name": "f_x.png", "contentType": "image/png", "url": "https://x/y.png"}],
    }
    attachments = normalize_attachments(message)

    assert message_kind(message) == "image"
    assert golden_content(message, attachments) == "到店先按门店指引完成手机号绑定和人脸录入。"
    assert message_content(message, attachments).splitlines()[0] == (
        "到店先按门店指引完成手机号绑定和人脸录入。"
    )
    assert "https://x/y.png" in message_content(message, attachments)


def test_business_text_preserves_contact_details_and_inline_urls() -> None:
    text = "我手机 13812346621，邮箱 jane@example.com，链接 https://example.com/order?id=9 请查收"
    message = {"content": text}
    assert message_text(message) == text
    assert message_content(message, []) == text
    assert golden_content(message, []) == text


def test_missing_url_and_empty_message_markers() -> None:
    missing = {
        "messageType": 6,
        "content": "",
        "attachments": [{"name": "x", "contentType": "image/*"}],
    }
    empty = {"messageType": 0, "content": "   "}

    assert message_content(missing, normalize_attachments(missing)) == "[image:missing-url]"
    assert golden_content(missing, normalize_attachments(missing)) == "图片附件：x"
    assert message_content(empty, []) == "[empty-message]"
    assert golden_content(empty, []) == "[empty-message]"


def outcome_with(messages: list[SourceMessage]) -> SessionOutcome:
    return SessionOutcome(
        workspace_id="ws-1",
        session_id="GD-ATT-0001",
        included=True,
        reasons=[],
        first_customer_message_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        publishable_turn_count=1,
        human_reply_turn_count=0,
        removed_third_party_message_count=0,
        bot_user_id="bot-1",
        im_bot_id="im-1",
        messages=messages,
    )


def test_golden_build_carries_attachments_and_media_label() -> None:
    messages = [
        SourceMessage(
            seq=1,
            role=SenderRole.VISITOR,
            content="https://cdn.example.com/customer.jpg",
            text="",
            content_type="image",
            sent_at=BASE,
            attachments=[
                {
                    "name": "customer.jpg",
                    "content_type": "image/*",
                    "url": "https://cdn.example.com/customer.jpg",
                    "size": 1024,
                }
            ],
        ),
        SourceMessage(
            seq=2,
            role=SenderRole.AI,
            content="收到您的截图。",
            text="收到您的截图。",
            content_type="text",
            sent_at=BASE,
        ),
    ]

    session, anomalies = build_session(
        outcome_with(messages), snapshot_id="snap-att", profile_keys=[], annotated_at="2026-09-01"
    )

    assert session is not None
    assert [anomaly.code for anomaly in anomalies] == []
    customer_message = session.turns[0].input.customer_messages[0]
    assert customer_message.content == "图片附件：customer.jpg"
    assert customer_message.type == "image"
    assert [attachment.url for attachment in customer_message.attachments or []] == [
        "https://cdn.example.com/customer.jpg"
    ]


def test_golden_build_flags_unreplayable_media_and_empty_messages() -> None:
    messages = [
        SourceMessage(
            seq=1,
            role=SenderRole.VISITOR,
            content="[image:missing-url]",
            text="",
            content_type="image",
            sent_at=BASE,
            attachments=[{"name": "x.jpg", "content_type": "image/*", "url": ""}],
        ),
        SourceMessage(
            seq=2,
            role=SenderRole.AI,
            content="[empty-message]",
            text="",
            content_type="text",
            sent_at=BASE,
        ),
        SourceMessage(
            seq=3,
            role=SenderRole.VISITOR,
            content="还有别的问题吗",
            text="还有别的问题吗",
            content_type="text",
            sent_at=BASE,
        ),
    ]

    _, anomalies = build_session(
        outcome_with(messages), snapshot_id="snap-att", profile_keys=[], annotated_at="2026-09-01"
    )

    codes = [anomaly.code for anomaly in anomalies]
    assert codes == [
        "contains_unreplayable_media",
        "contains_empty_message",
        "no_reply_turn_excluded",
    ]




class MediaTransport:
    """Source transport that also serves the attachment CDN."""

    def __init__(self) -> None:
        self.downloaded: list[str] = []

    def post(self, url: str, *, headers, payload):  # noqa: ANN001
        if url.endswith("/api/butler/session/agent/message/history"):
            return {
                "code": 200,
                "data": {
                    "messages": [
                        {
                            "id": "m-1",
                            "senderType": 1,
                            "messageType": 6,
                            "content": "",
                            "createdTime": 1_760_000_000_000,
                            "attachments": [
                                {
                                    "name": "photo.jpg",
                                    "contentType": "image/*",
                                    "url": "https://cdn.example.com/photo.jpg",
                                    "size": 12,
                                }
                            ],
                        },
                        {
                            "id": "m-2",
                            "senderType": 3,
                            "messageType": 0,
                            "content": "收到您的截图。",
                            "createdTime": 1_760_000_060_000,
                        }
                    ],
                    "messageTotal": 2,
                },
            }
        return {"code": 200, "data": {}}

    def get(self, url: str, *, headers, params=None):  # noqa: ANN001
        return {"code": 200, "data": {}}

    def get_bytes(self, url: str, *, max_bytes: int = 20_000_000):
        self.downloaded.append(url)
        if "cdn.example.com" not in url:
            raise AssertionError(f"unexpected download {url}")
        return b"jpeg-bytes", "image/jpeg"


def candidate():
    from golden_eval.domain.corpus import SessionCandidate

    return SessionCandidate(
        workspace_id="ws-1",
        session_id="S-1",
        updated_at=BASE,
        session_type="single",
        subject_type="VISITOR",
        chat_type="single",
        channel_type="web",
        channel_source_type="web",
        visitor_found=True,
        bot_user_id="bot-1",
        im_bot_id="im-1",
    )


def test_source_to_turn_request_preserves_business_values() -> None:
    from golden_eval.adapters.platform_source import PlatformCorpusSource
    from golden_eval.application.turn_projection import build_turn_tasks
    from golden_eval.domain.models import RunIdentity

    text = "13812346621 jane@example.com https://example.com/order?id=9"

    class ContactTransport(MediaTransport):
        def post(self, url, *, headers, payload):
            body = super().post(url, headers=headers, payload=payload)
            for message in body.get("data", {}).get("messages", []):
                message["content"] = text
            return body

    source = PlatformCorpusSource(base_url="https://platform.example.com",
        identity=RunIdentity(tenant_id="ws-1", source="connection_settings"),
        transport=ContactTransport())
    history = source.hydrate(candidate())
    assert all(message.text == text for message in history.messages)
    session, _ = build_session(outcome_with(history.messages), snapshot_id="snap-contact",
                              profile_keys=[], annotated_at="2026-09-01")
    tasks = build_turn_tasks(run_id="test-contact", session=session, repeat_index=1,
                            account_prefix="eval", bot_user_id="bot-1", im_bot_id="im-1")
    assert tasks[0].to_conversation_request().messages[0].content == text
    assert tasks[0].messages[0].attachments[0].url == "https://cdn.example.com/photo.jpg"
