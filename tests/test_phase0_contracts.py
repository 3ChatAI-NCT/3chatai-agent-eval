from __future__ import annotations

import pytest
from pydantic import ValidationError

from golden_eval.domain.models import ConversationRequest


def test_conversation_request_rejects_scoring_fields() -> None:
    with pytest.raises(ValidationError):
        ConversationRequest(
            account_id="account-1",
            messages=[{"role": "user", "content": "hello"}],
            profile_before={},
            bot_user_id="bot-1",
            im_bot_id="im-1",
            expected={},
        )
