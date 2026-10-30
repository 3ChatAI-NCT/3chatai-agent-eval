from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from atomic_eval.batch import BatchSpec, run_batch, validate
from atomic_eval.config import ConfigError, Limits, ModelEndpoint, SlsSettings
from atomic_eval.sls import CaptureError, JsonlRecordSource, RecordSource, build_query, collect

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


# --- 查询拼装 -----------------------------------------------------------------


def test_query_always_carries_the_workspace_clause():
    query = build_query(["ws-1", "ws-2"], terms=["agent-model"])
    assert query.startswith('("ws-1" or "ws-2")')
    assert '"[LLM请求结束]"' in query
    assert '"agent-model"' in query


@pytest.mark.parametrize("term", ["a | select count(*)", 'x" or "1', "line\nbreak"])
def test_query_rejects_injection_attempts(term):
    with pytest.raises(CaptureError):
        build_query(["ws-1"], terms=[term])


def test_query_requires_a_workspace():
    with pytest.raises(CaptureError):
        build_query([])


# --- 时间窗二分 ---------------------------------------------------------------


class CappedSource:
    """模拟真实 SLS：窗口越宽命中越多，超过 limit 就返回未取尽。"""

    def __init__(self, per_second: float = 1.0) -> None:
        self.per_second = per_second
        self.calls: list[tuple[int, int, int]] = []

    def fetch(self, start: int, end: int, query: str, limit: int) -> tuple[list[dict], bool]:
        self.calls.append((start, end, limit))
        total = int((end - start) * self.per_second)
        rows = [{"__time__": start + i, "msg": f"row-{start}-{i}"} for i in range(total)]
        return rows[:limit], total <= limit


def test_collect_bisects_until_each_segment_is_exhausted():
    source = CappedSource(per_second=1.0)
    result = collect(source, start=0, end=400, query="q", segment_cap=100)
    assert result.truncated is False
    assert len(result.records) == 400
    assert all(segment.exhausted for segment in result.segments)
    assert len(result.segments) > 1  # 确实切过段
    # 分段是连续且不重叠的
    ordered = sorted(result.segments, key=lambda s: s.start)
    assert ordered[0].start == 0 and ordered[-1].end == 400
    for left, right in pairwise(ordered):
        assert left.end == right.start


def test_collect_marks_truncated_when_it_cannot_split_further():
    source = CappedSource(per_second=500.0)  # 每秒 500 条，切到最小粒度也超
    result = collect(source, start=0, end=8, query="q", segment_cap=100)
    assert result.truncated is True


def test_collect_stops_at_max_records():
    source = CappedSource(per_second=1.0)
    result = collect(source, start=0, end=10000, query="q", segment_cap=100, max_records=300)
    assert result.truncated is True
    assert len(result.records) <= 400


def test_collect_hands_segments_to_sink_without_keeping_them():
    received = []
    result = collect(CappedSource(per_second=1.0), start=0, end=400, query="q",
                     segment_cap=100, sink=received.extend)
    assert result.records == [] and result.rows == 400 == len(received)
    assert result.summary["rows"] == 400


def test_jsonl_source_filters_by_window():
    records = [{"__time__": t, "msg": f"m{t}"} for t in (5, 15, 25)]
    rows, exhausted = JsonlRecordSource(records).fetch(10, 20, "q", 100)
    assert [row["__time__"] for row in rows] == [15]
    assert exhausted is True


# --- 批次 ---------------------------------------------------------------------


def make_spec(**overrides) -> BatchSpec:
    data = {
        "workspace_ids": ["ws-1"],
        "start": NOW - timedelta(hours=2),
        "end": NOW - timedelta(hours=1),
        "size": 5,
    }
    data.update(overrides)
    return BatchSpec(**data)


def test_batch_id_is_stable_and_spec_sensitive():
    assert make_spec().batch_id == make_spec().batch_id
    assert make_spec().batch_id != make_spec(size=6).batch_id


def test_window_must_be_closed_and_delayed():
    limits = Limits()
    with pytest.raises(CaptureError):
        validate(make_spec(end=NOW), limits, now=NOW)  # 结束时间太新
    with pytest.raises(CaptureError):
        validate(
            make_spec(start=NOW - timedelta(hours=1), end=NOW - timedelta(hours=2)), limits, now=NOW
        )


def test_size_cap_is_enforced_server_side():
    with pytest.raises(CaptureError):
        validate(make_spec(size=500), Limits(max_sample_size=100), now=NOW)


def gateway_record(index: int, at: int, workspace: str = "ws-1") -> dict:
    request = {
        "model": "agent-model",
        "input": [{"role": "user", "content": f"客户问题 {index}"}],
        "tools": [],
    }
    response = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"text": "回复"}]}],
    }
    message = (
        f"[LLM请求结束] path=/v1/responses status=200 requestId=req-{index} attempt=1 "
        f"requestBody={json.dumps(request, ensure_ascii=False)} "
        f"responseBody={json.dumps(response, ensure_ascii=False)}"
    )
    return {"msg": message, "tenantId": workspace, "__time__": at, "consumeTime": "100"}


def test_run_batch_freezes_cases_and_manifest(tmp_path):
    start = int((NOW - timedelta(hours=2)).timestamp())
    records = [gateway_record(i, start + i) for i in range(20)]
    records.append(gateway_record(99, start + 1, workspace="ws-9"))  # 其他空间
    records.append({"msg": "无关日志", "__time__": start + 2})  # 不可解析
    source: RecordSource = JsonlRecordSource(records)

    manifest = run_batch(make_spec(), source, output_dir=tmp_path / "b1", now=NOW)

    assert manifest["cases"] == 5
    assert manifest["scanned"] == 22
    assert manifest["funnel"]["excluded"]["not_complete_call_record"] == 1
    assert manifest["funnel"]["excluded"]["workspace_mismatch"] == 1
    cases = [
        json.loads(line) for line in (tmp_path / "b1" / "cases.jsonl").read_text().splitlines()
    ]
    assert len(cases) == 5
    assert all(case["outputs"][0]["side_id"] == "baseline" for case in cases)
    assert manifest["query"].startswith('("ws-1")')


def test_run_batch_is_reproducible(tmp_path):
    start = int((NOW - timedelta(hours=2)).timestamp())
    records = [gateway_record(i, start + i) for i in range(20)]
    first = run_batch(make_spec(), JsonlRecordSource(records), output_dir=tmp_path / "a", now=NOW)
    second = run_batch(make_spec(), JsonlRecordSource(records), output_dir=tmp_path / "b", now=NOW)
    assert first["cases_sha256"] == second["cases_sha256"]
    assert first["batch_id"] == second["batch_id"]


def test_shortfall_is_reported_not_hidden(tmp_path):
    start = int((NOW - timedelta(hours=2)).timestamp())
    records = [gateway_record(i, start + i) for i in range(3)]
    manifest = run_batch(
        make_spec(size=10), JsonlRecordSource(records), output_dir=tmp_path / "c", now=NOW
    )
    assert manifest["cases"] == 3
    assert manifest["funnel"]["shortfall"] == 7


# --- 配置 ---------------------------------------------------------------------


def test_missing_env_lists_all_missing_names(monkeypatch):
    for name in ("SLS_ENDPOINT", "SLS_PROJECT", "SLS_LOGSTORE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "secret")
    with pytest.raises(ConfigError) as excinfo:
        SlsSettings.from_env()
    assert "SLS_ENDPOINT" in str(excinfo.value)
    assert "SLS_LOGSTORE" in str(excinfo.value)


def test_secrets_are_not_printed(monkeypatch):
    monkeypatch.setenv("JUDGE_BASE_URL", "https://gateway.internal/v1")
    monkeypatch.setenv("JUDGE_MODEL", "some-model")
    monkeypatch.setenv("JUDGE_API_KEY", "super-secret-value")
    endpoint = ModelEndpoint.from_env("JUDGE")
    assert "super-secret-value" not in repr(endpoint)
    assert endpoint.api_key.get_secret_value() == "super-secret-value"


# --- 生产约束：保留期与请求体开关 ------------------------------------------------


def summary_record(index: int, at: int, workspace: str = "ws-1") -> dict:
    """没有开启完整请求体的空间只会记录这种摘要日志。"""

    message = (
        "[LLM请求结束] success=true "
        f"requestId=req-s{index} path=/v1/responses model=agent-model "
        "upstreamModel=agent-model channelId=azure-aoai22 apiKeyId=azure-aoai22-key "
        "promptTokens=1858 completionTokens=89 totalTokens=1947 status=200 attempt=1 retryCount=0"
    )
    return {"msg": message, "tenantId": workspace, "__time__": at}


def test_summary_only_record_is_reported_as_body_logging_disabled():
    from atomic_eval.gateway import ParseError, parse_record

    with pytest.raises(ParseError) as excinfo:
        parse_record(summary_record(1, 0))
    assert excinfo.value.reason == "body_logging_disabled"


def test_query_filters_out_records_without_body():
    from atomic_eval.sls import build_query

    assert '"requestBody"' in build_query(["ws-1"])
    assert '"requestBody"' not in build_query(["ws-1"], require_body=False)


def test_window_cannot_exceed_log_retention():
    limits = Limits(log_retention_days=7)
    spec = make_spec(start=NOW - timedelta(days=9), end=NOW - timedelta(days=8))
    with pytest.raises(CaptureError) as excinfo:
        validate(spec, limits, now=NOW)
    assert "7 天" in str(excinfo.value)


def test_guidance_explains_why_samples_are_missing(tmp_path):
    start = int((NOW - timedelta(hours=2)).timestamp())
    records = [summary_record(i, start + i) for i in range(6)]
    records += [gateway_record(i, start + 100 + i) for i in range(2)]
    manifest = run_batch(
        make_spec(size=10), JsonlRecordSource(records), output_dir=tmp_path / "g", now=NOW
    )
    assert manifest["funnel"]["excluded"]["body_logging_disabled"] == 6
    joined = " ".join(manifest["guidance"])
    assert "完整请求体" in joined
    assert "按需开启" in joined


def test_no_guidance_when_the_batch_is_complete(tmp_path):
    start = int((NOW - timedelta(hours=2)).timestamp())
    records = [gateway_record(i, start + i) for i in range(20)]
    manifest = run_batch(
        make_spec(), JsonlRecordSource(records), output_dir=tmp_path / "ok", now=NOW
    )
    assert manifest["guidance"] == []


def test_token_usage_is_captured_for_later_cost_reporting():
    from atomic_eval.gateway import parse_record

    record = gateway_record(1, 0)
    record["msg"] = record["msg"].replace(
        "attempt=1 ", "attempt=1 promptTokens=4000 completionTokens=28 channelId=azure-aoai22 "
    )
    call = parse_record(record)
    assert call.prompt_tokens == 4000
    assert call.completion_tokens == 28
    assert call.channel_id == "azure-aoai22"
