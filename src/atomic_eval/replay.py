"""重放：用冻结的请求体调候选模型，只替换 model 字段。

三条纪律：
1. 除 model 外不改任何字段，并记录请求哈希与变更字段，证明两侧收到的是同一个请求。
2. 有些请求天然无法等价重放（引用了服务端存储的会话或响应），直接标记跳过，不假装重放成功。
3. 失败不自动重试，避免重复计费；用 --resume 显式续跑。
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import ModelEndpoint
from .models import ModelCall

# 这些字段意味着请求依赖服务端保存的状态，换一个供应商不可能等价重放。
NON_REPLAYABLE_FIELDS = (
    "previous_response_id",
    "conversation",
    "prompt",
    "prompt_cache_key",
)


def request_digest(request: dict) -> str:
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def blocked_reason(request: dict) -> str | None:
    for field in NON_REPLAYABLE_FIELDS:
        if request.get(field):
            return f"请求依赖服务端状态字段 {field}，无法等价重放"
    for item in request.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "item_reference":
            return "请求引用了服务端存储的 item，无法等价重放"
    return None


def swap_model(request: dict, model: str) -> tuple[dict, list[str]]:
    """只换 model，其余原样。返回 (新请求, 变更字段)。"""

    replayed = dict(request)
    changed = []
    if request.get("model") != model:
        replayed["model"] = model
        changed.append("model")
    return replayed, changed


@dataclass(frozen=True)
class ReplayResult:
    call_id: str
    side_id: str
    status: str  # completed | blocked | error
    request_sha256: str = ""
    changed_fields: tuple[str, ...] = ()
    response: dict | None = None
    returned_model: str = ""
    elapsed_ms: int | None = None
    usage: dict | None = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "side_id": self.side_id,
            "status": self.status,
            "request_sha256": self.request_sha256,
            "changed_fields": list(self.changed_fields),
            "response": self.response,
            "returned_model": self.returned_model,
            "elapsed_ms": self.elapsed_ms,
            "usage": self.usage,
            "error": self.error,
        }


class ResponsesClient(Protocol):
    def create(self, request: dict) -> dict: ...


class HttpResponsesClient:
    """任意兼容 /v1/responses 的端点。Key 只从配置读取，不接受明文传参。"""

    def __init__(self, endpoint: ModelEndpoint, *, timeout: float = 300.0) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

    def create(self, request: dict) -> dict:
        response = httpx.post(
            f"{self._endpoint.base_url}/responses",
            json=request,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._endpoint.api_key.get_secret_value()}",
            },
            timeout=self._timeout,
            follow_redirects=False,
        )
        response.raise_for_status()
        return response.json()


def replay_call(
    call: ModelCall, client: ResponsesClient, *, side_id: str, model: str
) -> ReplayResult:
    blocked = blocked_reason(call.request)
    if blocked:
        return ReplayResult(call_id=call.call_id, side_id=side_id, status="blocked", error=blocked)

    request, changed = swap_model(call.request, model)
    digest = request_digest(request)
    started = time.monotonic()
    try:
        response = client.create(request)
    except (httpx.HTTPError, ValueError) as exc:
        return ReplayResult(
            call_id=call.call_id,
            side_id=side_id,
            status="error",
            request_sha256=digest,
            changed_fields=tuple(changed),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
    return ReplayResult(
        call_id=call.call_id,
        side_id=side_id,
        status="completed",
        request_sha256=digest,
        changed_fields=tuple(changed),
        response=response,
        returned_model=str(response.get("model") or ""),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        usage=response.get("usage") if isinstance(response.get("usage"), dict) else None,
    )


def replay_calls(
    calls: list[ModelCall],
    client: ResponsesClient,
    *,
    side_id: str,
    model: str,
    concurrency: int = 2,
    done: set[str] | None = None,
) -> list[ReplayResult]:
    """并发有上限；已完成的 call 可以跳过，避免续跑时重复计费。"""

    pending = [call for call in calls if not done or call.call_id not in done]
    if concurrency <= 1:
        return [replay_call(call, client, side_id=side_id, model=model) for call in pending]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(replay_call, call, client, side_id=side_id, model=model) for call in pending
        ]
        return [future.result() for future in futures]
