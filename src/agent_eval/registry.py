"""Register EVAL workspaces from the Feishu Base and read their access tokens on demand."""

import time

import httpx

FIELDS = ["基准名称", "源空间名称", "源空间ID", "基准空间ID", "状态", "环境"]
TOKEN_FIELDS = ["源空间AccessToken", "基准空间AccessToken"]
API = "https://open.feishu.cn/open-apis"
CREDENTIAL_TTL = 300.0

# 金标执行按轮次调用，每轮都查飞书会触发限流；token 只在进程内存里短暂缓存
_credentials: dict[tuple[str, str, str], tuple[float, str, str]] = {}


def _value(fields, name):
    value = fields.get(name)
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return str(value[0].get("text") or value[0].get("name") or "").strip()
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def _search(settings, body, client, limit):
    if not all((settings.feishu_app_id, settings.feishu_app_secret.get_secret_value(),
                settings.feishu_base_app_token, settings.feishu_base_table_id)):
        raise ValueError("Feishu registry credentials and coordinates are not configured")
    http = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        response = http.post(API + "/auth/v3/tenant_access_token/internal", json={
            "app_id": settings.feishu_app_id,
            "app_secret": settings.feishu_app_secret.get_secret_value(),
        })
        response.raise_for_status()
        token_payload = response.json()
        if token_payload.get("code") != 0 or not token_payload.get("tenant_access_token"):
            raise ValueError("Feishu app authentication failed")
        headers = {"Authorization": f"Bearer {token_payload['tenant_access_token']}"}
        path = (API + f"/bitable/v1/apps/{settings.feishu_base_app_token}"
                f"/tables/{settings.feishu_base_table_id}/records/search")
        page = None
        found = []
        while True:
            params = {"page_size": 200}
            if page:
                params["page_token"] = page
            response = http.post(path, params=params, json=body, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise ValueError("Feishu registry read failed")
            data = payload.get("data") or {}
            found.extend(data.get("items") or [])
            if len(found) > limit:
                raise ValueError("Feishu registry exceeds safe read limit")
            page = data.get("page_token") if data.get("has_more") else None
            if not page:
                return found
    finally:
        if client is None:
            http.close()


def records(settings, client=None):
    body = {"field_names": FIELDS + TOKEN_FIELDS}
    if settings.feishu_base_view_id:
        body["view_id"] = settings.feishu_base_view_id
    return _search(settings, body, client, limit=2000)


def credentials(settings, space, client=None):
    """Return (source_token, target_token) for a registered space; tokens are never persisted."""

    cache_key = (space["id"], space["source_workspace_id"], space["target_workspace_id"])
    cached = _credentials.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1], cached[2]
    # 按平台登记的两个空间 ID 过滤：台账被改成别的空间时查不到，而不是拿到错误空间的 token
    rows = _search(settings, {"field_names": TOKEN_FIELDS, "filter": {
        "conjunction": "and",
        "conditions": [
            {"field_name": "源空间ID", "operator": "is", "value": [space["source_workspace_id"]]},
            {"field_name": "基准空间ID", "operator": "is", "value": [space["target_workspace_id"]]},
        ],
    }}, client, limit=10)
    rows = [row for row in rows if row.get("record_id") == space["id"]]
    if len(rows) != 1:
        raise ValueError("飞书台账中找不到与平台登记一致的空间")
    fields = rows[0].get("fields") or {}
    source_token = _value(fields, "源空间AccessToken")
    target_token = _value(fields, "基准空间AccessToken")
    if not source_token or not target_token or source_token == target_token:
        raise ValueError("飞书台账中来源与 EVAL 空间 token 未独立配置")
    _credentials[cache_key] = (time.monotonic() + CREDENTIAL_TTL, source_token, target_token)
    return source_token, target_token


def normalize(record, settings):
    fields = record.get("fields") or {}
    record_id = record.get("record_id") or ""
    source = _value(fields, "源空间ID")
    target = _value(fields, "基准空间ID")
    if not (record_id and source and target):
        return None, "missing_workspace_pair"
    environment = _value(fields, "环境")
    expected = {"development": "sit", "production": "prod"}[
        settings.environment
    ]
    if environment != expected:
        return None, "other_environment"
    source_token = _value(fields, "源空间AccessToken")
    target_token = _value(fields, "基准空间AccessToken")
    ready = (_value(fields, "状态") == "已验证" and source != target
             and source_token and target_token and source_token != target_token)
    return {"id": record_id, "name": _value(fields, "基准名称") or record_id,
            "source_name": _value(fields, "源空间名称"),
            "source_workspace_id": source, "target_workspace_id": target,
            "status": "active" if ready else "needs_check"}, None


def sync(store, settings, subject, client=None):
    output = {"synced": 0, "active": 0, "needs_check": 0,
              "skipped": {"missing_workspace_pair": 0, "other_environment": 0}}
    for item in records(settings, client):
        record, reason = normalize(item, settings)
        if reason:
            output["skipped"][reason] += 1
            continue
        store.upsert_space(record, subject)
        output["synced"] += 1
        output[record["status"]] += 1
    return output
