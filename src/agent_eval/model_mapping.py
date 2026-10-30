"""EVAL 空间执行模型：3Chat 网关按租户把 Agent 请求的模型替换为指定模型。

EVAL Workspace 即网关租户。Agent 固定请求 settings.mapping_from_model，空间配置的只是它的替换目标；
映射到自身等于不替换，因此「恢复默认」不需要删除映射。当前值始终以网关查询为准，
平台只记录谁在何时改成了什么。
"""

import time

import httpx

from . import gateway

PROBE_TIMEOUT_SECONDS = 30


class MappingRejected(ValueError):
    """不能切换执行模型的业务原因；status 对应 HTTP 状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def query(settings, tenant_ids: list[str]) -> dict[str, list[dict]]:
    if not tenant_ids:
        return {}
    response = httpx.post(settings.model_mapping_url.rstrip("/")
                          + "/internal/model-mappings/tenants/query",
                          json={"tenantIds": tenant_ids}, timeout=10, follow_redirects=False)
    response.raise_for_status()
    # 线上返回 [{"tenantId": ..., "mappings": [...]}]；没有映射的租户可能不出现在列表里
    body = response.json()
    if not isinstance(body, list):
        raise ValueError("网关模型映射查询返回格式不正确")
    found = {row.get("tenantId"): row.get("mappings") for row in body if isinstance(row, dict)}
    return {tenant: [item for item in found.get(tenant) or [] if isinstance(item, dict)]
            for tenant in tenant_ids}


def update(settings, tenant_ids: list[str], model: str) -> None:
    response = httpx.post(settings.model_mapping_url.rstrip("/")
                          + "/internal/model-mappings/tenants",
                          json={"updates": [{"tenantId": tenant,
                                             "fromModel": settings.mapping_from_model,
                                             "toModel": model} for tenant in tenant_ids]},
                          timeout=10, follow_redirects=False)
    response.raise_for_status()


def current(settings, mappings: list[dict]) -> str | None:
    """租户当前的替换目标；没有映射或映射到自身时为 None，表示使用默认模型。"""

    for item in mappings:
        if item.get("fromModel") == settings.mapping_from_model:
            model = item.get("toModel") or None
            return None if model == settings.mapping_from_model else model
    return None


def failure(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPError):
        return "网关模型映射接口不可用：" + gateway.failure_reason(exc)
    return "网关模型映射接口返回格式不正确"


def states(store, settings, platform_models: set[str]) -> dict:
    """platform_models 是平台自己经网关调用的模型（Judge、草稿模型）。"""

    spaces = store.spaces()
    found = query(settings, sorted({space["target_workspace_id"] for space in spaces}))
    busy = store.busy_tenants()
    items = []
    for space in spaces:
        tenant = space["target_workspace_id"]
        mappings = found.get(tenant, [])
        shared = tenant == settings.internal_tenant_id
        items.append({
            "space_id": space["id"], "model": current(settings, mappings),
            "other_mappings": sum(item.get("fromModel") != settings.mapping_from_model
                                  for item in mappings),
            "locked": tenant in busy, "shared": shared,
            "protected": shared and settings.mapping_from_model in platform_models,
        })
    return {"from_model": settings.mapping_from_model, "items": items}


def change(store, settings, space_ids: list[str], model: str | None, subject: str,
           platform_models: set[str]) -> dict:
    """model 为 None 表示恢复默认。写入后立即回查，返回网关实际生效的值。"""

    target = model or settings.mapping_from_model

    def apply(spaces: list[dict]) -> list[dict]:
        tenants = sorted({space["target_workspace_id"] for space in spaces})
        if (settings.internal_tenant_id in tenants
                and settings.mapping_from_model in platform_models):
            # 平台也以内部租户身份经网关调用；Judge 或草稿模型正好是被替换的模型时会一起被换掉
            names = [space["name"] for space in spaces
                     if space["target_workspace_id"] == settings.internal_tenant_id]
            raise MappingRejected(
                422, f"{'、'.join(names)} 与评测平台同租户，且 Judge 或草稿模型就是 "
                     f"{settings.mapping_from_model}，切换会连评分模型一起替换")
        before = query(settings, tenants)
        update(settings, tenants, target)
        after = query(settings, tenants)
        return [{"space_id": space["id"], "tenant_id": space["target_workspace_id"],
                 "from_model": settings.mapping_from_model,
                 "previous_model": current(settings, before[space["target_workspace_id"]]),
                 "model": current(settings, after[space["target_workspace_id"]])}
                for space in spaces]

    changes = store.change_space_models(space_ids, apply, subject)
    expected = None if target == settings.mapping_from_model else target
    unknown = None
    if expected:
        try:
            unknown = expected not in {item["id"] for item in gateway.chat_models(settings)}
        except Exception:
            pass  # 只是提示；映射已写入，任何异常都不能把这次写入报成失败
    return {"changes": changes,
            "mismatched": [item["space_id"] for item in changes if item["model"] != expected],
            "unknown_model": unknown}


def snapshot(settings, space: dict) -> dict:
    """创建逐轮回放时冻结的执行模型；查询失败不阻塞创建，只记下原因。"""

    try:
        mappings = query(settings, [space["target_workspace_id"]])[space["target_workspace_id"]]
    except (httpx.HTTPError, ValueError) as exc:
        return {"from_model": settings.mapping_from_model, "error": failure(exc)}
    return {"from_model": settings.mapping_from_model, "model": current(settings, mappings)}


def call(settings, tenant: str) -> dict:
    """以 EVAL 租户身份、按 Agent 的方式请求被替换的模型，验证映射后的模型真实可用；结果不落库。"""

    result = {"ok": False, "model": None, "reply": "", "input_tokens": None,
              "output_tokens": None, "detail": ""}
    started = time.monotonic()

    def done(**changes) -> dict:
        return result | changes | {"latency_ms": round((time.monotonic() - started) * 1000)}

    try:
        key = gateway.api_key(settings, tenant)
    except (httpx.HTTPError, ValueError):
        return done(detail="EVAL 租户在模型网关没有可用 key，请检查 entitlement 授权")
    try:
        response = httpx.post(settings.model_gateway_url.rstrip("/") + "/responses",
                              headers={"Authorization": "Bearer " + key},
                              json={"model": settings.mapping_from_model, "input": "ping",
                                    "max_output_tokens": 16, "stream": False},
                              timeout=PROBE_TIMEOUT_SECONDS, follow_redirects=False)
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        return done(detail=f"网关返回 HTTP {exc.response.status_code}"
                    + (f"：{message}" if (message := error_message(exc.response)) else ""))
    except (httpx.HTTPError, ValueError) as exc:
        return done(detail=gateway.failure_reason(exc))
    if not isinstance(body, dict):
        return done(detail="响应格式不符合 OpenAI Responses")
    text = "".join(part.get("text") or "" for item in body.get("output") or []
                   if isinstance(item, dict) for part in item.get("content") or []
                   if isinstance(part, dict))
    usage = body.get("usage") or {}
    # 能响应即可用；推理模型可能把 16 个 token 用在思考上，正文为空或被截断不算失败
    return done(ok=True, model=body.get("model"), reply=text[:200],
                input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                detail="" if body.get("status") == "completed" else "模型已响应，但输出被截断")


def error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else (
        body.get("message") if isinstance(body, dict) else None)
    return str(message)[:200] if message else ""
