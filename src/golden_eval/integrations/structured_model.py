"""结构化模型 Adapter：把「System Prompt + 结构化输入」变成「结构化输出」。

只依赖标准库：判定网关基本都是 OpenAI-compatible 的 `/chat/completions`，不需要为此引入 SDK。
密钥、模型名、超时与重试全部来自服务端配置（`.env` + 设置页），不进入浏览器请求。

模型返回必须是一个 JSON 对象；代码侧仍然会按合同再校验一遍（字段、证据引用、ratio 算术），
模型说什么不算数，代码复核后才算数。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.5
RAW_EXCERPT_LIMIT = 4000


class StructuredModelError(RuntimeError):
    """模型调用失败；`code` 会原样出现在响应与运行事件里。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class StructuredModelConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    json_mode: bool = True
    max_retries: int = DEFAULT_MAX_RETRIES
    # 供应商专有参数，例如千问的 enable_thinking；原样并入请求体
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructuredModelCall:
    """一次调用的结果与审计信息。"""

    payload: dict[str, Any]
    latency_ms: int
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw_excerpt: str = ""
    attempts: int = 1


class StructuredModelPort(Protocol):
    def generate(
        self,
        *,
        prompt_version: str,
        system_prompt: str,
        input_document: dict,
        output_schema: dict,
        model_profile: str = "",
    ) -> StructuredModelCall: ...


class OpenAICompatibleStructuredModel:
    """OpenAI-compatible `/chat/completions`（公司内部网关通常同形）。"""

    def __init__(self, config: StructuredModelConfig) -> None:
        if not config.base_url or not config.model or not config.api_key:
            raise StructuredModelError(
                "model_not_configured",
                "judge model requires base_url, model and api_key",
            )
        self._config = config

    @property
    def model(self) -> str:
        return self._config.model

    def generate(
        self,
        *,
        prompt_version: str,
        system_prompt: str,
        input_document: dict,
        output_schema: dict,
        model_profile: str = "",
    ) -> StructuredModelCall:
        body: dict[str, Any] = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _serialize_input(input_document),
                },
            ],
        }
        if self._config.json_mode:
            # OpenAI 与多数网关支持；不支持时可用 GOLDEN_EVAL_JUDGE_JSON_MODE=0 关掉。
            body["response_format"] = {"type": "json_object"}
        body.update(self._config.extra_body)

        started = time.monotonic()
        last_error: StructuredModelError | None = None
        for attempt in range(1, self._config.max_retries + 2):
            try:
                envelope = self._post(body)
            except StructuredModelError as exc:
                last_error = exc
                if not exc.retryable or attempt > self._config.max_retries:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            text = _message_text(envelope)
            payload = _parse_json_object(text)
            return StructuredModelCall(
                payload=payload,
                latency_ms=latency_ms,
                model=str(envelope.get("model") or self._config.model),
                usage=envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {},
                raw_excerpt=text[:RAW_EXCERPT_LIMIT],
                attempts=attempt,
            )
        raise last_error or StructuredModelError("model_unavailable", "model call failed")

    # ------------------------------------------------------------------ internals

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            _completion_url(self._config.base_url),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._config.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._config.timeout_seconds
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", errors="replace")
            raise StructuredModelError(
                f"model_http_{exc.code}",
                f"{exc.code}: {detail}",
                status=exc.code,
                retryable=exc.code in {408, 409, 425, 429} or exc.code >= 500,
            ) from exc
        except TimeoutError as exc:
            raise StructuredModelError("model_timeout", str(exc), retryable=True) from exc
        except urllib.error.URLError as exc:
            raise StructuredModelError(
                "model_network", str(exc.reason), retryable=True
            ) from exc
        try:
            envelope = json.loads(raw)
        except ValueError as exc:
            raise StructuredModelError(
                "model_envelope_invalid", raw[:300].decode("utf-8", errors="replace")
            ) from exc
        if not isinstance(envelope, dict):
            raise StructuredModelError("model_envelope_invalid", "response is not an object")
        return envelope


class FixtureStructuredModel:
    """测试用：按顺序返回预设载荷，不发起任何网络调用。"""

    def __init__(self, payloads: list[dict[str, Any]] | None = None) -> None:
        self.payloads = list(payloads or [])
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        prompt_version: str,
        system_prompt: str,
        input_document: dict,
        output_schema: dict,
        model_profile: str = "",
    ) -> StructuredModelCall:
        self.calls.append(
            {
                "prompt_version": prompt_version,
                "model_profile": model_profile,
                "items": [
                    (item.get("turn_id"), item.get("repeat_index"))
                    for item in input_document.get("items", [])
                ],
            }
        )
        payload = self.payloads.pop(0) if self.payloads else {"items": []}
        return StructuredModelCall(payload=payload, latency_ms=0, model="fixture-model")


def _completion_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"


def _serialize_input(input_document: dict[str, Any]) -> str:
    """工单必须能原样发给模型；不可序列化时给出明确错误，而不是抛 500。"""

    try:
        return json.dumps(input_document, ensure_ascii=False)
    except TypeError as exc:
        raise StructuredModelError(
            "model_input_not_serializable", f"judge input is not JSON-serializable: {exc}"
        ) from exc


def _message_text(envelope: dict[str, Any]) -> str:
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise StructuredModelError(
            "model_empty_response", json.dumps(envelope, ensure_ascii=False)[:300]
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        # 有些网关把内容拆成 parts。
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise StructuredModelError("model_empty_response", "message.content is empty")
    return content


def _parse_json_object(text: str) -> dict[str, Any]:
    """把模型输出解析成一个 JSON 对象。

    真实模型对「一条工单里多个 item」的返回方式很随意，实测 DeepSeek 对逐 Turn 的画像工单会
    **每行输出一个 JSON 对象**（NDJSON），也见过整个数组。这两种都不是单个对象，但语义是清楚的：
    就是逐条结果。统一收成 `{"items": [...]}`，让选择 `items` 的调用方（判定、画像补全）直接可用。

    容忍：```json 围栏、前后解释文字、NDJSON、顶层数组。
    """

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```", 2)[1] if candidate.count("```") >= 2 else candidate
        candidate = candidate.removeprefix("json").strip()
    try:
        payload = json.loads(candidate)
    except ValueError:
        listed = _parse_ndjson(candidate)
        if listed is not None:
            return {"items": listed}
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise StructuredModelError(
                "model_invalid_json", candidate[:300]
            ) from None
        try:
            payload = json.loads(candidate[start : end + 1])
        except ValueError as exc:
            raise StructuredModelError("model_invalid_json", candidate[:300]) from exc
    if isinstance(payload, list):
        return {"items": payload}
    if not isinstance(payload, dict):
        raise StructuredModelError("model_invalid_json", "model output is not an object")
    return payload


def _parse_ndjson(candidate: str) -> list[Any] | None:
    """每行一个 JSON 对象时按行解析；不是这种形状返回 None，交给别的分支。"""

    lines = [line.strip().rstrip(",") for line in candidate.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    values: list[Any] = []
    for line in lines:
        try:
            values.append(json.loads(line))
        except ValueError:
            return None
    if not all(isinstance(value, dict) for value in values):
        return None
    return values
