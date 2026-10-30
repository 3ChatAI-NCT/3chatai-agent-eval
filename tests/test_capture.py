from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_eval import capture
from agent_eval.api import capture_summary
from agent_eval.models import Mode

SPACE = {"id": "rec1", "source_workspace_id": "src", "target_workspace_id": "eval"}


def atomic_job(start, end):
    return {"mode": Mode.ATOMIC, "name": "Atomic window",
            "spec": {"start": start.isoformat(), "end": end.isoformat(), "size": 1}}


@pytest.fixture
def batches(monkeypatch):
    specs = []

    def collect_batch(spec, _source):
        specs.append(spec)
        calls = [{"request": {"model": "agent-model"}}]
        return SimpleNamespace(calls=calls, excluded=[], manifest={
            "funnel": {}, "capture": {}, "guidance": [], "calls_sha256": "sha"})

    monkeypatch.setattr(capture, "collect_batch", collect_batch)
    monkeypatch.setattr(capture, "SlsRecordSource", lambda: None)
    return specs


def test_atomic_window_ending_now_is_clamped_to_settled_logs(batches):
    now = datetime.now(UTC)
    _, summary, _, _ = capture.collect(atomic_job(now - timedelta(hours=24), now), SPACE, None)
    assert batches[0].end <= now - timedelta(minutes=10)
    assert batches[0].start == now - timedelta(hours=24)
    assert summary["window"] == {"start": batches[0].start.isoformat(),
                                 "end": batches[0].end.isoformat()}
    assert summary["models"] == {"agent-model": 1}


def test_atomic_window_entirely_within_the_log_delay_is_rejected(batches):
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="过新"):
        capture.collect(atomic_job(now - timedelta(minutes=5), now), SPACE, None)
    assert not batches


@pytest.mark.parametrize(("calls", "excluded", "reason"), [
    (334, {}, "未开启完整请求体日志"),
    (0, {}, "没有符合条件的模型调用"),
    (0, {"http_error": 2}, "都不可等价回放"),
    (0, {"model_mismatch": 5}, "没有模型"),
])
def test_empty_capture_explains_why_nothing_can_be_replayed(monkeypatch, calls, excluded, reason):
    queries = []

    class Source:
        def count(self, _start, _end, query):
            queries.append(query)
            return calls

    def collect_batch(_spec, _source):
        return SimpleNamespace(calls=[], excluded=[], manifest={"funnel": {"excluded": excluded}})

    monkeypatch.setattr(capture, "collect_batch", collect_batch)
    monkeypatch.setattr(capture, "SlsRecordSource", Source)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match=reason):
        capture.collect(atomic_job(now - timedelta(hours=24), now), SPACE, None)
    assert all("requestBody" not in query for query in queries)


def test_capture_summary_preserves_the_jobs_scan_limit():
    row = {"id": "capture-1", "spec": {"policy": {
        "quota_per_workspace": 50, "min_turns": 3, "max_scanned_sessions": 1000,
        "contact_filters": [{"field": "botUserId", "value": "private-routing"}],
    }}}
    public = capture_summary(row)
    assert public["request"]["max_scanned_sessions"] == 1000
    assert "contact_filters" not in public["request"]
    assert "spec" not in public
