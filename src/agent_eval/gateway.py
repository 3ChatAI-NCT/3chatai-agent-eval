"""3Chat model gateway: an OpenAI-compatible endpoint keyed per tenant by entitlement-service."""

import time

import httpx

KEY_TTL = 300.0

# 网关 key 只在进程内存里短暂缓存，不落库、不进日志
_keys: dict[str, tuple[float, str]] = {}


def api_key(settings, tenant: str | None = None) -> str:
    """Resolve a tenant's gateway key (the internal tenant by default); no static fallback key."""

    tenant = tenant or settings.internal_tenant_id
    cached = _keys.get(tenant)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    response = httpx.get(settings.entitlement_url.rstrip("/") + "/api/entitlements/api-key/default",
                         params={"productSource": settings.entitlement_product_source},
                         headers={"TENANT_ID": tenant, "Accept": "application/json"},
                         timeout=10, follow_redirects=False)
    response.raise_for_status()
    body = response.json()
    entity = (body.get("data") or {}).get("entity") if body.get("code") == 200 else None
    key = entity.get("key") if isinstance(entity, dict) else None
    if not key:
        raise ValueError("租户在模型网关没有可用 key，请检查 entitlement 授权")
    _keys[tenant] = (time.monotonic() + KEY_TTL, key)
    return key


def judge_extras(model: str) -> dict:
    """千问混合思考模型默认开启思考，而思考模式不支持 JSON 结构化输出，实测会输出半截 JSON，
    因此关闭思考。只支持思考模式的千问模型（名称带 thinking）不接受关闭；其他厂商不认识该参数。
    """

    hybrid_qwen = model.startswith("qwen") and "thinking" not in model
    return {"enable_thinking": False} if hybrid_qwen else {}


def judge_body(model: str, messages: list[dict], **extra) -> dict:
    """Judge 请求体：温度 0、强制 JSON 输出。"""

    return {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": messages, **judge_extras(model), **extra}


# 网关模型列表里混有向量、重排、语音、图像等非对话模型，不能用作 Judge
NON_CHAT = ("embedding", "rerank", "asr", "tts", "ocr", "image", "-mt-")


MODELS_TTL = 60.0
_models: list = [0.0, []]


def chat_models(settings) -> list[dict]:
    """网关当前可路由的对话模型，按模型名去重，保留所属供应商便于选择异源 Judge。

    创建评测、批量开跑与设置页都会读取，短暂缓存避免一批评测反复请求网关。
    """

    if _models[0] > time.monotonic():
        return _models[1]
    response = httpx.get(settings.model_gateway_url.rstrip("/") + "/models",
                         headers={"Authorization": "Bearer " + api_key(settings)},
                         timeout=10, follow_redirects=False)
    response.raise_for_status()
    found: dict[str, str] = {}
    for group in response.json().get("data") or []:
        for channel in group.get("channels") or []:
            for model in channel.get("models") or []:
                if isinstance(model, str) and not any(word in model for word in NON_CHAT):
                    found.setdefault(model, channel.get("provider") or "")
    models = [{"id": model, "provider": provider} for model, provider in sorted(found.items())]
    _models[:] = [time.monotonic() + MODELS_TTL, models]
    return models


def probe(settings, model: str, timeout: int) -> dict:
    """用极小的 JSON 请求确认该模型可经网关调用且支持 JSON 输出。"""

    key = api_key(settings)
    started = time.monotonic()
    try:
        response = httpx.post(settings.model_gateway_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json=judge_body(model, [{"role": "system", "content": '只输出 JSON：{"ok": true}'},
                                    {"role": "user", "content": "ping"}], max_tokens=200),
            timeout=timeout, follow_redirects=False)
        response.raise_for_status()
        # 推理类模型可能把 token 用在思考上而正文为空，按空文本处理而不是报 500
        content = str(response.json()["choices"][0]["message"].get("content") or "")
        ok = '"ok"' in content
        detail = "" if ok else "模型已响应，但没有按要求输出 JSON"
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        ok, detail = False, failure_reason(exc)
    return {"ok": ok, "latency_ms": round((time.monotonic() - started) * 1000), "detail": detail}


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "调用超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"接口返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "无法连接接口"
    return "响应格式不符合 OpenAI Chat Completions"
