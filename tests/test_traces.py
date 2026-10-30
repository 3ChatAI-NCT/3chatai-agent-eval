"""模型调用链路：网关日志解析、排序、冻结时机与请求体日志预检。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_eval import traces
from atomic_eval.gateway import ParseError, parse_span
from atomic_eval.sls import JsonlRecordSource

REQUEST_ID = "a" * 32
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def record(*, request_id=REQUEST_ID, model="agent-model", ended="12:00:03.500", elapsed="1500",
           body=True, path="/v1/responses", when=None):
    message = (f"[LLM请求结束] path={path} status=200 requestId={request_id} attempt=1 "
               f"upstreamModel={model} promptTokens=120 completionTokens=30")
    if body:
        message += (f" requestBody={json.dumps({'model': model, 'input': 'hi'})} "
                    f"responseBody={json.dumps({'status': 'completed', 'output': []})}")
    return {"msg": message, "consumeTime": elapsed, "tenantId": "target-ws",
            "@timestamp": f"2026-10-13T{ended}+0000",
            "__time__": int((when or NOW - timedelta(minutes=1)).timestamp())}


def trace(**overrides):
    return {"id": "trace_1", "experiment_id": "exp_1", "request_id": REQUEST_ID,
            "workspace_id": "target-ws", "created_at": NOW - timedelta(minutes=5),
            "settled_at": NOW - timedelta(minutes=3), "deadline_at": NOW + timedelta(minutes=27),
            "expected_calls": None, "call_count": None, "poll_count": 0, **overrides}


def test_span_keeps_calls_without_body_as_summaries():
    full = parse_span(record())
    assert full["has_body"] and full["request"]["model"] == "agent-model"
    assert (full["prompt_tokens"], full["elapsed_ms"]) == (120, 1500.0)
    summary = parse_span(record(body=False, path="/v1/chat/completions"))
    assert summary["has_body"] is False and summary["body_error"] == "body_logging_disabled"
    assert summary["path"] == "/v1/chat/completions"
    truncated = record()["msg"].split(" requestBody=")[0] + " requestBody={"
    broken = parse_span({**record(), "msg": truncated})
    assert broken["has_body"] is False and broken["body_error"] == "request_body_invalid_json"
    with pytest.raises(ParseError):
        parse_span({"msg": "[其他日志]"})


def test_calls_sharing_a_request_id_stay_distinct_and_ordered_by_start():
    source = JsonlRecordSource([
        record(model="main", ended="12:00:05.000", elapsed="4000"),   # 01.000 开始
        record(model="tool", ended="12:00:02.000", elapsed="500"),    # 01.500 开始
        record(model="other", request_id="b" * 32),                   # 别的轮次
    ])
    spans = traces.fetch(source, trace(), NOW)
    assert [(span["seq"], span["model"]) for span in spans] == [(1, "main"), (2, "tool")]
    assert len({span["span_id"] for span in spans}) == 2


def span(has_body=True):
    return {"has_body": has_body}


@pytest.mark.parametrize("state,spans,expected", [
    ({}, [span()], None),                                            # 第一次查询，还不知道是否写完
    ({"call_count": 2}, [span(), span()], "complete"),               # 两次查询条数一致
    ({"call_count": 1}, [span(), span()], None),                     # 还在增加
    ({"call_count": 2}, [span(False), span(False)], "summary_only"),
    ({"call_count": 2}, [span(), span(False)], "partial"),
    ({"call_count": 2, "expected_calls": 3}, [span(), span()], None),  # Agent 日志说有 3 次
    ({"call_count": 2, "expected_calls": 3, "deadline_at": NOW}, [span(), span()], "partial"),
    ({"deadline_at": NOW}, [], "not_found"),
    ({}, [], None),
])
def test_trace_is_frozen_only_once_the_logs_have_settled(state, spans, expected):
    assert traces.decide(trace(**state), spans, NOW) == expected


def test_summary_never_carries_bodies():
    spans = traces.fetch(JsonlRecordSource([record()]), trace(), NOW)
    assert "request" not in traces.summary(spans)[0] and "response" not in traces.summary(spans)[0]


class Counting:
    def __init__(self, calls, bodies):
        self.calls, self.bodies = calls, bodies

    def count(self, start, end, query):
        hours = (end - start) // 3600
        if "requestBody" in query:
            return self.bodies.get(hours, 0)
        return self.calls.get(hours, 0)


@pytest.mark.parametrize("calls,bodies,status,hours", [
    ({24: 10}, {24: 10}, "enabled", 24),
    ({24: 10}, {}, "disabled", 24),
    ({168: 3}, {168: 3}, "enabled", 168),
    ({}, {}, "unknown", 168),
])
def test_readiness_infers_body_logging_from_gateway_logs(calls, bodies, status, hours):
    result = traces.readiness(Counting(calls, bodies), "target-ws", NOW)
    assert (result["status"], result["window_hours"]) == (status, hours)


def test_overview_averages_only_collected_turns():
    calls = [{"prompt_tokens": 100, "completion_tokens": 10}] * 2
    rows = [{"status": "complete", "summary": calls},
            {"status": "pending", "summary": None}]
    assert traces.overview(rows) == {
        "statuses": {"complete": 1, "pending": 1}, "turns": 2, "collected": 1, "avg_calls": 2.0,
        "avg_prompt_tokens": 200.0, "avg_completion_tokens": 20.0}
