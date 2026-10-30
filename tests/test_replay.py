from __future__ import annotations

import httpx
import pytest

from atomic_eval.models import ModelCall
from atomic_eval.replay import (
    ReplayResult,
    blocked_reason,
    replay_call,
    replay_calls,
    request_digest,
    swap_model,
)

REQUEST = {
    "model": "agent-model",
    "input": [{"role": "user", "content": "周六有课吗"}],
    "tools": [{"name": "knowledge_knowledgeAll_knowledgeAll"}],
    "reasoning": {"effort": "none"},
    "stream": False,
    "tool_choice": "auto",
}


def make_call(index: int = 1, **overrides) -> ModelCall:
    request = {**REQUEST, **overrides}
    return ModelCall(
        call_id=f"req-{index}:attempt:1",
        workspace_id="ws-1",
        request=request,
        response={"status": "completed", "output": []},
        request_id=f"req-{index}",
    )


class FakeClient:
    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.response = response or {
            "model": "gpt-5.6-sol",
            "status": "completed",
            "output": [{"type": "message", "content": [{"text": "有的"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 8},
        }
        self.error = error
        self.seen: list[dict] = []

    def create(self, request: dict) -> dict:
        self.seen.append(request)
        if self.error:
            raise self.error
        return self.response


# --- 只换 model -----------------------------------------------------------------


def test_only_the_model_field_changes():
    replayed, changed = swap_model(REQUEST, "candidate-model")
    assert changed == ["model"]
    assert replayed["model"] == "candidate-model"
    assert {k: v for k, v in replayed.items() if k != "model"} == {
        k: v for k, v in REQUEST.items() if k != "model"
    }
    assert REQUEST["model"] == "agent-model"  # 原请求没有被就地修改


def test_replay_records_request_digest_and_changed_fields():
    client = FakeClient()
    result = replay_call(make_call(), client, side_id="candidate", model="candidate-model")
    assert result.status == "completed"
    assert result.changed_fields == ("model",)
    assert result.request_sha256 == request_digest(client.seen[0])
    assert result.returned_model == "gpt-5.6-sol"  # 供应商声明的模型单独留档
    assert result.usage == {"input_tokens": 100, "output_tokens": 8}


def test_same_request_sent_to_both_sides_has_the_same_digest_except_model():
    baseline, _ = swap_model(REQUEST, "agent-model")
    candidate, _ = swap_model(REQUEST, "candidate-model")
    assert request_digest(baseline) != request_digest(candidate)
    assert request_digest({**baseline, "model": "x"}) == request_digest({**candidate, "model": "x"})


# --- 不可等价重放的请求 ----------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"previous_response_id": "resp_123"},
        {"conversation": "conv_1"},
        {"input": [{"type": "item_reference", "id": "msg_1"}]},
    ],
)
def test_requests_depending_on_server_state_are_blocked(overrides):
    call = make_call(**overrides)
    client = FakeClient()
    result = replay_call(call, client, side_id="candidate", model="m")
    assert result.status == "blocked"
    assert client.seen == []  # 没有真的发出去
    assert blocked_reason(call.request)


# --- 失败处理 -------------------------------------------------------------------


def test_failure_is_recorded_not_retried():
    client = FakeClient(error=httpx.ReadTimeout("timeout"))
    result = replay_call(make_call(), client, side_id="candidate", model="m")
    assert result.status == "error"
    assert "ReadTimeout" in result.error
    assert len(client.seen) == 1  # 不自动重试，避免重复计费


def test_resume_skips_completed_calls():
    calls = [make_call(i) for i in range(4)]
    client = FakeClient()
    done = {calls[0].call_id, calls[1].call_id}
    results = replay_calls(calls, client, side_id="candidate", model="m", concurrency=1, done=done)
    assert len(results) == 2
    assert len(client.seen) == 2


def test_concurrent_replay_covers_every_call():
    calls = [make_call(i) for i in range(6)]
    client = FakeClient()
    results = replay_calls(calls, client, side_id="candidate", model="m", concurrency=3)
    assert {r.call_id for r in results} == {c.call_id for c in calls}
    assert all(r.status == "completed" for r in results)


def test_result_serialises_for_jsonl():
    row = ReplayResult(call_id="c1", side_id="candidate", status="blocked", error="x").to_dict()
    assert row["call_id"] == "c1"
    assert row["changed_fields"] == []


# --- 端到端：采样 → 重放 → 评审 ---------------------------------------------------
