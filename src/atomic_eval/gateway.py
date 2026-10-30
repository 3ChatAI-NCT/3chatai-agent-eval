"""网关日志 → 标准模型调用记录。

这是整条链路里最容易随上游变化的一层，所以：解析失败一律带**稳定的原因码**返回，
由采样漏斗计数并可下钻；不静默丢弃，也不把残缺记录当成合格样本。
"""

from __future__ import annotations

import hashlib
import json
import re

from .models import ModelCall

FIELD = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9]*)=([^\s]+)")
COMPLETE_MARKER = "[LLM请求结束]"
RESPONSES_PATH = "/v1/responses"


class ParseError(ValueError):
    """带原因码的解析失败，原因码进漏斗统计。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


def _decode_pair(rest: str) -> tuple[dict, dict]:
    decoder = json.JSONDecoder()
    try:
        request, end = decoder.raw_decode(rest)
    except json.JSONDecodeError as exc:
        raise ParseError("request_body_invalid_json", str(exc)) from exc
    suffix = rest[end:].lstrip()
    if not suffix.startswith("responseBody="):
        raise ParseError("response_body_missing", "完整请求体之后没有 responseBody")
    try:
        response, end = decoder.raw_decode(suffix[len("responseBody=") :].lstrip())
    except json.JSONDecodeError as exc:
        raise ParseError("response_body_invalid_json", str(exc)) from exc
    return request, response


def parse_record(record: dict) -> ModelCall:
    message = record.get("msg") or ""
    if not message.startswith(COMPLETE_MARKER):
        raise ParseError("not_complete_call_record")
    prefix, separator, rest = message.partition(" requestBody=")
    fields = dict(FIELD.findall(prefix))
    if fields.get("path") != RESPONSES_PATH:
        raise ParseError("not_responses_call", fields.get("path", ""))
    if not separator:
        # 这是一条正常的模型调用日志，只是该空间没有开启完整请求体记录。
        # 这类记录无法用于评估，但它说明"这里本来有样本"，和"这条不是模型调用"是两回事。
        raise ParseError("body_logging_disabled", fields.get("model", ""))

    request, response = _decode_pair(rest)
    request_id = fields.get("requestId") or record.get("requestId") or ""
    if not request_id:
        raise ParseError("request_id_missing")
    if record.get("requestId") and record["requestId"] != request_id:
        raise ParseError("request_id_conflict")

    status = fields.get("status", "")
    if status and status != "200":
        raise ParseError("http_error", status)
    if response.get("status") not in (None, "completed"):
        raise ParseError("response_incomplete", str(response.get("status")))

    elapsed = record.get("consumeTime")

    def as_int(name: str) -> int | None:
        raw = fields.get(name)
        return int(raw) if raw and raw.isdigit() else None

    return ModelCall(
        call_id=f"{request_id}:attempt:{fields.get('attempt', '1')}",
        workspace_id=str(record.get("tenantId") or fields.get("tenantId") or "unknown"),
        request=request,
        response=response,
        request_id=request_id,
        timestamp=str(record.get("@timestamp") or record.get("__time__") or ""),
        http_status=status or "200",
        upstream_model=fields.get("upstreamModel", ""),
        channel_id=fields.get("channelId", ""),
        attempt=fields.get("attempt", "1"),
        retry_count=fields.get("retryCount", "0"),
        prompt_tokens=as_int("promptTokens"),
        completion_tokens=as_int("completionTokens"),
        elapsed_ms=float(elapsed) if elapsed not in (None, "") else None,
    )


def parse_span(record: dict) -> dict:
    """模型调用链路里的一次调用。

    与 parse_record 不同：一个 Turn 的全部调用都要留下，所以不限 /v1/responses，
    没有请求体或请求体解析失败的记录也作为摘要保留，并写明原因。
    同一 Turn 的调用共用 requestId，调用自身用日志时间与内容 hash 区分。
    """

    message = record.get("msg") or ""
    if not message.startswith(COMPLETE_MARKER):
        raise ParseError("not_complete_call_record")
    prefix, separator, rest = message.partition(" requestBody=")
    fields = dict(FIELD.findall(prefix))
    request_id = fields.get("requestId") or record.get("requestId") or ""
    if not request_id:
        raise ParseError("request_id_missing")
    request = response = None
    body_error = None if separator else "body_logging_disabled"
    if separator:
        try:
            request, response = _decode_pair(rest)
        except ParseError as exc:
            body_error = exc.reason

    def as_int(name: str) -> int | None:
        raw = fields.get(name)
        return int(raw) if raw and raw.isdigit() else None

    elapsed = record.get("consumeTime")
    return {
        "span_id": hashlib.sha256(message.encode()).hexdigest()[:12],
        "request_id": request_id,
        "time": int(record.get("__time__") or 0),
        "timestamp": str(record.get("@timestamp") or ""),
        "path": fields.get("path", ""),
        "http_status": fields.get("status", ""),
        "model": (request or {}).get("model") or fields.get("model", ""),
        "upstream_model": fields.get("upstreamModel", ""),
        "attempt": fields.get("attempt", "1"),
        "prompt_tokens": as_int("promptTokens"),
        "completion_tokens": as_int("completionTokens"),
        "elapsed_ms": float(elapsed) if elapsed not in (None, "") else None,
        "has_body": request is not None,
        "body_error": body_error,
        "request": request,
        "response": response,
    }


def parse_records(records: list[dict]) -> tuple[list[ModelCall], list[dict]]:
    """返回 (解析成功的调用, 被排除的记录及原因)。"""

    calls: list[ModelCall] = []
    excluded: list[dict] = []
    for index, record in enumerate(records):
        try:
            calls.append(parse_record(record))
        except ParseError as exc:
            excluded.append({"row": index, "reason": exc.reason, "detail": str(exc)[:200]})
        except (KeyError, TypeError, ValueError) as exc:  # 结构完全不符合预期
            excluded.append({"row": index, "reason": "unparsable_record", "detail": str(exc)[:200]})
    return calls, excluded
