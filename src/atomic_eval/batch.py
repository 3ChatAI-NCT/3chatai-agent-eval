"""一次采样批次：用户给空间、时间窗和数量，这里产出冻结好的样本。

产物是不可变的：cases / excluded / manifest 三个文件，manifest 里记录查询、分段、漏斗与内容哈希，
后续重放与评审都引用这个批次 ID。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Limits
from .gateway import parse_records
from .sampling import Sampler, to_case
from .sls import CaptureError, RecordSource, build_query, collect


@dataclass(frozen=True)
class BatchSpec:
    workspace_ids: list[str]
    start: datetime
    end: datetime
    size: int
    seed: str | None = None
    model: str | None = None
    terms: list[str] = field(default_factory=list)

    def canonical(self) -> dict:
        return {
            "workspace_ids": sorted(self.workspace_ids),
            "start": int(self.start.timestamp()),
            "end": int(self.end.timestamp()),
            "size": self.size,
            "model": self.model,
            "terms": sorted(self.terms),
            "seed": self.seed,
        }

    @property
    def batch_id(self) -> str:
        blob = json.dumps(self.canonical(), ensure_ascii=False, sort_keys=True)
        return "batch-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def validate(spec: BatchSpec, limits: Limits, *, now: datetime | None = None) -> None:
    moment = now or datetime.now(UTC)
    if spec.start.tzinfo is None or spec.end.tzinfo is None:
        raise CaptureError("时间窗必须带时区")
    if spec.start >= spec.end:
        raise CaptureError("时间窗必须左闭右开且 start < end")
    delay = (moment - spec.end).total_seconds()
    if delay < limits.min_window_delay_seconds:
        raise CaptureError(
            f"结束时间必须早于当前时间 {limits.min_window_delay_seconds} 秒，"
            "否则日志可能还没写完，同一查询之后会得到不同结果"
        )
    age_days = (moment - spec.start).total_seconds() / 86400
    if age_days > limits.log_retention_days:
        earliest = moment - timedelta(days=limits.log_retention_days)
        raise CaptureError(
            f"SLS 只保留最近 {limits.log_retention_days} 天的日志，"
            f"开始时间不能早于 {earliest.astimezone(spec.start.tzinfo):%Y-%m-%d %H:%M}"
        )
    if not 1 <= spec.size <= limits.max_sample_size:
        raise CaptureError(f"size 必须在 1..{limits.max_sample_size} 之间")
    if not spec.workspace_ids:
        raise CaptureError("必须指定至少一个空间")


def guidance(funnel: dict, capture: dict, spec: BatchSpec, limits: Limits) -> list[str]:
    """样本不足时给出能直接照做的提示，而不是只丢一个数字。"""

    hints: list[str] = []
    if funnel["shortfall"] <= 0 and not capture["truncated"]:
        return hints
    excluded = funnel["excluded"]
    disabled = excluded.get("body_logging_disabled", 0)
    if disabled:
        hints.append(
            f"窗口内有 {disabled} 条模型调用只记录了摘要、没有完整请求体。"
            "完整请求体是按空间在阿里云侧按需开启的，请确认这些空间已开启后再采集。"
        )
    if funnel["eligible"] == 0 and not disabled:
        hints.append(
            "窗口内没有可用的模型调用记录。先确认空间 ID 是否正确，"
            f"以及该时间窗内是否确实有 {spec.model or '目标模型'} 的流量。"
        )
    if excluded.get("http_error"):
        hints.append(f"另有 {excluded['http_error']} 条调用是失败请求，不计入语义评估样本。")
    if capture["truncated"]:
        hints.append(
            "扫描已达上限，窗口内还有未取尽的日志。缩短时间窗后分批采集，可以拿到更完整的候选集。"
        )
    if funnel["shortfall"] > 0 and funnel["eligible"] > 0:
        hints.append(
            f"合格候选只有 {funnel['eligible']} 条，少于请求的数量。"
            f"可以扩大时间窗（注意日志只保留最近 {limits.log_retention_days} 天）或降低 size。"
        )
    return hints


def _serialize(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _digest(rows: list[dict]) -> str:
    """与落盘文件逐字节一致的哈希，manifest 里的值可以直接校验 cases.jsonl。"""

    return hashlib.sha256(_serialize(rows).encode()).hexdigest()


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    text = _serialize(rows)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class BatchResult:
    """一次采集的全部产物。落盘还是入库由调用方决定。"""

    manifest: dict
    cases: list[dict]
    calls: list[dict]
    excluded: list[dict]


def collect_batch(
    spec: BatchSpec,
    source: RecordSource,
    *,
    limits: Limits | None = None,
    now: datetime | None = None,
) -> BatchResult:
    """采集 → 筛选 → 抽样 → 冻结。纯数据，不碰文件系统。"""

    limits = limits or Limits()
    validate(spec, limits, now=now)
    seed = spec.seed or spec.batch_id
    query = build_query(spec.workspace_ids, terms=spec.terms)

    sampler = Sampler(size=spec.size, seed=seed, workspace_ids=spec.workspace_ids,
                      model=spec.model)
    scanned = 0

    def consume(rows: list[dict]) -> None:
        # 逐段解析后只把候选交给抽样器，解析结果不随窗口内调用总量累积
        nonlocal scanned
        for row in rows:
            calls, parse_excluded = parse_records([row])
            sampler.excluded.extend({**item, "row": scanned} for item in parse_excluded)
            scanned += 1
            for call in calls:
                sampler.add(call)

    captured = collect(
        source,
        start=int(spec.start.timestamp()),
        end=int(spec.end.timestamp()),
        query=query,
        segment_cap=limits.segment_cap,
        max_records=limits.max_scan_records,
        sink=consume,
    )
    result = sampler.result()

    cases = [to_case(call).model_dump() for call in result.selected]
    calls = [call.model_dump() for call in result.selected]
    cases_sha = _digest(cases)
    calls_sha = _digest(calls)

    manifest = {
        "batch_id": spec.batch_id,
        "created_at": (now or datetime.now(UTC)).isoformat(),
        "spec": spec.canonical(),
        "seed": seed,
        "query": query,
        "capture": captured.summary,
        "scanned": captured.rows,
        "funnel": result.funnel,
        "cases": len(cases),
        "cases_sha256": cases_sha,
        "calls_sha256": calls_sha,
        "guidance": guidance(result.funnel, captured.summary, spec, limits),
    }
    return BatchResult(manifest=manifest, cases=cases, calls=calls, excluded=result.excluded)


def run_batch(
    spec: BatchSpec,
    source: RecordSource,
    *,
    output_dir: Path,
    limits: Limits | None = None,
    now: datetime | None = None,
) -> dict:
    """采集并落盘成批次目录，CLI 用这个。"""

    result = collect_batch(spec, source, limits=limits, now=now)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    # 冻结原始调用：重放需要完整请求体，评审只需要 cases。
    _write_jsonl(output_dir / "cases.jsonl", result.cases)
    _write_jsonl(output_dir / "calls.jsonl", result.calls)
    _write_jsonl(output_dir / "excluded.jsonl", result.excluded)
    (output_dir / "manifest.json").write_text(
        json.dumps(result.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result.manifest
