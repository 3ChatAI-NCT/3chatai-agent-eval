"""SLS 采集。

两件事在这里保证：
1. 窗口内日志量不可控，所以单段命中上限就按时间对半切，递归到每段都取尽，
   而不是留一个"结果被截断了"的标记让人自己去猜。
2. 查询语句由代码拼装并强制带上空间条件，调用方只能提供受限的关键词，不能自由写查询。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Protocol

from .config import SlsSettings

FORBIDDEN_IN_TERM = ("|", "\n", '"')
COMPLETE_MARKER = "[LLM请求结束]"
BODY_MARKER = "requestBody"
MIN_SEGMENT_SECONDS = 2


class CaptureError(RuntimeError):
    pass


def build_query(
    workspace_ids: list[str], *, terms: list[str] | None = None, require_body: bool = True
) -> str:
    """参数化拼装查询。空间条件必须存在，且不接受 SQL 聚合或引号注入。

    完整请求体是按空间**按需开启**的，没开的空间只会记调用摘要。默认把 `requestBody`
    也加进查询，直接在 SLS 侧滤掉没有请求体的记录，避免把扫描配额浪费在不可用的日志上。
    """

    if not workspace_ids:
        raise CaptureError("必须指定至少一个空间")
    parts: list[str] = []
    extra = [*(terms or [])]
    if require_body:
        extra.append(BODY_MARKER)
    for term in [*workspace_ids, COMPLETE_MARKER, *extra]:
        text = str(term).strip()
        if not text:
            continue
        if any(bad in text for bad in FORBIDDEN_IN_TERM):
            raise CaptureError(f"查询词包含不允许的字符: {text!r}")
        parts.append(f'"{text}"')
    workspace_clause = " or ".join(f'"{item}"' for item in workspace_ids)
    others = [part for part in parts if part.strip('"') not in workspace_ids]
    return " and ".join([f"({workspace_clause})", *others])


class RecordSource(Protocol):
    """一段时间窗内的原始日志。返回 (记录, 是否已取尽)。"""

    def fetch(self, start: int, end: int, query: str, limit: int) -> tuple[list[dict], bool]: ...


@dataclass
class Segment:
    start: int
    end: int
    rows: int
    exhausted: bool


@dataclass
class CaptureResult:
    records: list[dict] = field(default_factory=list)  # 传了 sink 时为空，记录已逐段交给 sink
    rows: int = 0
    segments: list[Segment] = field(default_factory=list)
    truncated: bool = False  # 有段切到最小粒度仍未取尽

    @property
    def summary(self) -> dict:
        return {
            "rows": self.rows,
            "segments": len(self.segments),
            "truncated": self.truncated,
            "segment_detail": [
                {"start": s.start, "end": s.end, "rows": s.rows, "exhausted": s.exhausted}
                for s in self.segments
            ],
        }


def collect(
    source: RecordSource,
    *,
    start: int,
    end: int,
    query: str,
    segment_cap: int = 1000,
    max_records: int = 20000,
    sink: Callable[[list[dict]], None] | None = None,
) -> CaptureResult:
    """按时间窗取全量记录，单段命中上限就二分。

    高流量空间一天上万条完整日志，全部驻留会撑爆内存；传入 sink 时每段取尽后立即交出，
    不在结果里保留。
    """

    if start >= end:
        raise CaptureError("时间窗必须左闭右开且 start < end")
    result = CaptureResult()
    emit = sink or result.records.extend
    pending = [(start, end)]
    while pending:
        segment_start, segment_end = pending.pop(0)
        if result.rows >= max_records:
            result.truncated = True
            break
        rows, exhausted = source.fetch(segment_start, segment_end, query, segment_cap)
        if exhausted or segment_end - segment_start <= MIN_SEGMENT_SECONDS:
            emit(rows)
            result.rows += len(rows)
            result.segments.append(Segment(segment_start, segment_end, len(rows), exhausted))
            if not exhausted:
                result.truncated = True
            continue
        middle = segment_start + (segment_end - segment_start) // 2
        pending.insert(0, (middle, segment_end))
        pending.insert(0, (segment_start, middle))
    result.segments.sort(key=lambda item: item.start)
    return result


class JsonlRecordSource:
    """离线重放已冻结的原始日志，用于开发与契约测试，不触网。"""

    def __init__(self, records: list[dict], time_key: str = "__time__") -> None:
        self._records = records
        self._time_key = time_key

    def fetch(self, start: int, end: int, query: str, limit: int) -> tuple[list[dict], bool]:
        rows = [
            record for record in self._records if start <= int(record.get(self._time_key, 0)) < end
        ]
        return rows[:limit], len(rows) <= limit


class SlsRecordSource:
    """真实 SLS。凭证来自环境变量，不接受调用方传入明文。"""

    def __init__(self, settings: SlsSettings | None = None, *, page_size: int = 100) -> None:
        self._settings = settings or SlsSettings.from_env()
        self._page_size = page_size
        self._client = None

    def _connect(self):
        if self._client is None:
            try:
                from aliyun.log import LogClient
            except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
                raise CaptureError("需要安装可选依赖：pip install 'atomic-eval[sls]'") from exc
            token = self._settings.security_token
            self._client = LogClient(
                self._settings.endpoint,
                self._settings.access_key_id.get_secret_value(),
                self._settings.access_key_secret.get_secret_value(),
                securityToken=token.get_secret_value() if token else None,
            )
        return self._client

    def fetch(self, start: int, end: int, query: str, limit: int) -> tuple[list[dict], bool]:
        from aliyun.log import GetLogsRequest

        client = self._connect()
        rows: list[dict] = []
        offset = 0
        while offset < limit:
            size = min(self._page_size, limit - offset)
            request = GetLogsRequest(
                self._settings.project,
                self._settings.logstore,
                start,
                end,
                query=query,
                line=size,
                offset=offset,
                reverse=False,
            )
            response = self._with_retry(partial(client.get_logs, request))
            page = [
                {**dict(log.get_contents()), "__time__": log.get_time()}
                for log in response.get_logs()
            ]
            rows.extend(page)
            offset += len(page)
            if len(page) < size:
                return rows, True  # 本段已取尽
        return rows, False  # 命中上限，交给上层二分

    def count(self, start: int, end: int, query: str) -> int:
        """只统计条数、不取内容，用于诊断采集不到样本的原因。"""

        from aliyun.log import GetHistogramsRequest

        request = GetHistogramsRequest(self._settings.project, self._settings.logstore,
                                       start, end, query=query)
        return self._with_retry(partial(self._connect().get_histograms, request)).get_total_count()

    @staticmethod
    def _with_retry(call, attempts: int = 3):
        """只重试可以安全重试的情况；鉴权与参数错误立即失败。"""

        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = call()
            except Exception as exc:  # SDK 异常类型不稳定，按状态码分类
                status = getattr(exc, "status", None) or getattr(exc, "resp_status", None)
                if status in (401, 403, 400, 404):
                    raise CaptureError(f"SLS 调用被拒绝（{status}），请检查凭证与权限") from exc
                last = exc
            else:
                if response.is_completed():
                    return response
                last = CaptureError("SLS 返回结果不完整")
            time.sleep(2**attempt)
        raise CaptureError(f"SLS 重试 {attempts} 次仍未成功: {last}")
