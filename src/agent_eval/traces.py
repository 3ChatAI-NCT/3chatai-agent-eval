"""逐轮回放的模型调用链路：按回放请求 ID 到网关日志采集一个 Turn 内的全部模型调用并冻结。

网关把对话请求头里的 request_id 写进这一轮每次模型调用日志的 requestId。日志写入 SLS 有延迟，
所以轮次结束后延迟查询，调用数在两次查询间稳定后才冻结；SLS 只保留 7 天，冻结进对象存储的副本
是唯一的长期证据。链路只作诊断与训练素材，不进 Judge，也不改变评测结论。
"""

from collections import Counter
from datetime import datetime, timedelta

from atomic_eval.gateway import ParseError, parse_span
from atomic_eval.sls import build_query, collect

WINDOW_BEFORE_SECONDS = 60
WINDOW_AFTER_SECONDS = 300
MAX_POLL_DELAY_SECONDS = 600
SPAN_INDEX = ("seq", "span_id", "started_at_ms", "path", "http_status", "model",
              "upstream_model", "attempt", "prompt_tokens", "completion_tokens", "elapsed_ms",
              "has_body", "body_error")


def _ended_ms(span: dict) -> int:
    try:
        moment = datetime.strptime(span["timestamp"], "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return span["time"] * 1000
    return int(moment.timestamp() * 1000)


def fetch(source, trace: dict, now: datetime) -> list[dict]:
    """取该回放请求 ID 在时间窗内的全部调用，按开始时间排序编号。"""

    start = int(trace["created_at"].timestamp()) - WINDOW_BEFORE_SECONDS
    end = min(int(trace["settled_at"].timestamp()) + WINDOW_AFTER_SECONDS, int(now.timestamp()))
    query = build_query([trace["workspace_id"]], terms=[trace["request_id"]], require_body=False)
    records = collect(source, start=start, end=end, query=query, segment_cap=200,
                      max_records=500).records
    spans: dict[str, dict] = {}
    for record in records:
        try:
            span = parse_span(record)
        except ParseError:
            continue
        # 全文检索可能命中正文里恰好出现该 ID 的其他调用，只认 requestId 完全相同的
        if span["request_id"] == trace["request_id"]:
            ended = _ended_ms(span)
            spans[span["span_id"]] = {**span, "started_at_ms": ended - int(span["elapsed_ms"] or 0)}
    ordered = sorted(spans.values(), key=lambda item: (item["started_at_ms"], item["span_id"]))
    return [{**span, "seq": index} for index, span in enumerate(ordered, start=1)]


def decide(trace: dict, spans: list[dict], now: datetime) -> str | None:
    """返回冻结时的状态；还需要再等一轮查询时返回 None。"""

    count = len(spans)
    expected = trace["expected_calls"]
    # 与上一次查询条数相同才算日志写完；Agent 日志记过模型调用次数时还要达到该数
    settled = count > 0 and count == trace["call_count"] and (not expected or count >= expected)
    if not settled and now < trace["deadline_at"]:
        return None
    if not count:
        return "not_found"
    if expected and count < expected:
        return "partial"
    bodies = [span["has_body"] for span in spans]
    return "complete" if all(bodies) else "partial" if any(bodies) else "summary_only"


def summary(spans: list[dict]) -> list[dict]:
    """数据库里只留索引，不含任何请求或响应正文。"""

    return [{key: span[key] for key in SPAN_INDEX} for span in spans]


def poll_delay(poll_count: int) -> timedelta:
    return timedelta(seconds=min(60 * 2 ** poll_count, MAX_POLL_DELAY_SECONDS))


def poll(store, objects, source, trace: dict, now: datetime) -> str | None:
    spans = fetch(source, trace, now)
    status = decide(trace, spans, now)
    if status is None:
        store.reschedule_trace(trace, len(spans), now + poll_delay(trace["poll_count"]))
        return None
    key = sha = None
    if spans:
        key = f"traces/{trace['experiment_id']}/{trace['id']}.json.gz"
        sha = objects.put(key, {"request_id": trace["request_id"],
                                "workspace_id": trace["workspace_id"], "spans": spans})
    store.finish_trace(trace, status=status, summary=summary(spans), object_key=key,
                       content_sha256=sha)
    return status


def overview(rows: list[dict]) -> dict:
    """报告里的链路覆盖：各状态计数，以及已冻结轮次的平均调用数与 tokens（只用索引）。"""

    collected = [row for row in rows if row["summary"]]
    spans = [span for row in collected for span in row["summary"]]

    def mean(total: float) -> float | None:
        return round(total / len(collected), 1) if collected else None

    return {"statuses": dict(Counter(row["status"] for row in rows)), "turns": len(rows),
            "collected": len(collected), "avg_calls": mean(len(spans)),
            "avg_prompt_tokens": mean(sum(span["prompt_tokens"] or 0 for span in spans)),
            "avg_completion_tokens": mean(sum(span["completion_tokens"] or 0 for span in spans))}


def readiness(source, workspace_id: str, now: datetime) -> dict:
    """从网关日志推断该空间是否开启了完整请求体日志：最近 24 小时没有调用时扩到 7 天。"""

    end = int(now.timestamp()) - 60
    for hours in (24, 24 * 7):
        start = end - hours * 3600
        calls = source.count(start, end, build_query([workspace_id], require_body=False))
        if calls:
            bodies = source.count(start, end, build_query([workspace_id]))
            return {"status": "enabled" if bodies else "disabled", "calls": calls,
                    "body_calls": bodies, "window_hours": hours}
    return {"status": "unknown", "calls": 0, "body_calls": 0, "window_hours": 24 * 7}
