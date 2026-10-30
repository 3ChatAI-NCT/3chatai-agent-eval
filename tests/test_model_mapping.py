import httpx
import pytest
from pydantic import SecretStr, ValidationError

from agent_eval import model_mapping
from agent_eval.config import Settings
from agent_eval.models import SpaceModelIn


def settings():
    return Settings(database_url=SecretStr("postgresql://unused"), environment="development",
                    dev_identity=True, internal_tenant_id="internal-tenant",
                    model_mapping_url="http://gateway.test:8080/",
                    model_gateway_url="https://gateway.example.test/v1",
                    mapping_from_model="agent-model")


class Response:
    def __init__(self, body=None):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def test_query_asks_for_all_tenants_and_keeps_only_mapping_objects(monkeypatch):
    seen = {}

    def post(url, json, timeout, follow_redirects):
        seen.update(url=url, json=json)
        return Response([{"tenantId": "tenant-a", "mappings": [
                            {"fromModel": "weaver", "toModel": "weaver/xxx"}, "junk"]},
                         {"tenantId": "tenant-b", "mappings": []}])

    monkeypatch.setattr(model_mapping.httpx, "post", post)
    found = model_mapping.query(settings(), ["tenant-a", "tenant-b", "tenant-c"])
    assert seen["url"] == "http://gateway.test:8080/internal/model-mappings/tenants/query"
    assert seen["json"] == {"tenantIds": ["tenant-a", "tenant-b", "tenant-c"]}
    assert found == {"tenant-a": [{"fromModel": "weaver", "toModel": "weaver/xxx"}],
                     "tenant-b": [], "tenant-c": []}


def test_update_maps_the_fixed_agent_model_for_every_tenant(monkeypatch):
    seen = {}
    monkeypatch.setattr(model_mapping.httpx, "post",
                        lambda url, json, **_: seen.update(url=url, json=json) or Response())
    model_mapping.update(settings(), ["tenant-a", "tenant-b"], "weaver/xxx")
    assert seen["url"] == "http://gateway.test:8080/internal/model-mappings/tenants"
    assert seen["json"] == {"updates": [
        {"tenantId": "tenant-a", "fromModel": "agent-model", "toModel": "weaver/xxx"},
        {"tenantId": "tenant-b", "fromModel": "agent-model", "toModel": "weaver/xxx"},
    ]}


def test_query_rejects_an_unexpected_shape(monkeypatch):
    monkeypatch.setattr(model_mapping.httpx, "post",
                        lambda *_args, **_kwargs: Response({"tenant-a": []}))
    with pytest.raises(ValueError, match="格式不正确"):
        model_mapping.query(settings(), ["tenant-a"])


@pytest.mark.parametrize(("mappings", "expected"), [
    ([], None),
    ([{"fromModel": "weaver", "toModel": "weaver/xxx"}], None),
    ([{"fromModel": "agent-model", "toModel": "agent-model"}], None),
    ([{"fromModel": "agent-model", "toModel": "qwen3.7-plus"}], "qwen3.7-plus"),
])
def test_current_model_ignores_other_mappings_and_identity(mappings, expected):
    assert model_mapping.current(settings(), mappings) == expected


def test_snapshot_records_why_the_gateway_could_not_be_read(monkeypatch):
    def down(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(model_mapping.httpx, "post", down)
    assert model_mapping.snapshot(settings(), {"target_workspace_id": "tenant-a"}) == {
        "from_model": "agent-model", "error": "网关模型映射接口不可用：无法连接接口"}


@pytest.mark.parametrize("body", [
    {"space_ids": []},
    {"space_ids": ["a", "a"]},
    {"space_ids": ["a"], "model": "bad model"},
    {"space_ids": ["a"], "model": ""},
])
def test_space_model_input_is_validated(body):
    with pytest.raises(ValidationError):
        SpaceModelIn.model_validate(body)


class Reply:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        self.request = httpx.Request("POST", "http://gateway.test/v1/responses")

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, json=self.body, request=self.request)
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)

    def json(self):
        return self.body


@pytest.fixture
def tenant_key(monkeypatch):
    seen = {}

    def key(_settings, tenant):
        seen["tenant"] = tenant
        return "eval-key"

    monkeypatch.setattr(model_mapping.gateway, "api_key", key)
    return seen


def test_probe_calls_the_agent_model_as_the_eval_tenant(monkeypatch, tenant_key):
    seen = {}

    def post(url, headers, json, **_kwargs):
        seen.update(url=url, headers=headers, json=json)
        return Reply(200, {"model": "candidate-model", "status": "completed",
                           "output": [{"type": "message", "content": [
                               {"type": "output_text", "text": "pong"}]}],
                           "usage": {"input_tokens": 7, "output_tokens": 5}})

    monkeypatch.setattr(model_mapping.httpx, "post", post)
    result = model_mapping.call(settings(), "tenant-a")
    assert tenant_key["tenant"] == "tenant-a"
    assert seen["url"].endswith("/v1/responses")
    assert seen["headers"] == {"Authorization": "Bearer eval-key"}
    assert seen["json"]["model"] == "agent-model" and seen["json"]["stream"] is False
    assert result | {"latency_ms": 0} == {
        "ok": True, "model": "candidate-model", "reply": "pong", "input_tokens": 7,
        "output_tokens": 5, "detail": "", "latency_ms": 0}


def test_probe_keeps_the_gateway_error_message(monkeypatch, tenant_key):
    monkeypatch.setattr(model_mapping.httpx, "post", lambda *_args, **_kwargs: Reply(
        404, {"error": {"message": "model weaver/typo not found"}}))
    result = model_mapping.call(settings(), "tenant-a")
    assert result["ok"] is False
    assert result["detail"] == "网关返回 HTTP 404：model weaver/typo not found"


def test_probe_accepts_a_truncated_reasoning_reply(monkeypatch, tenant_key):
    monkeypatch.setattr(model_mapping.httpx, "post", lambda *_args, **_kwargs: Reply(
        200, {"model": "qwen3.7-plus", "status": "incomplete", "output": []}))
    result = model_mapping.call(settings(), "tenant-a")
    assert result["ok"] is True and result["reply"] == ""
    assert "截断" in result["detail"]


def test_probe_reports_a_tenant_without_a_gateway_key(monkeypatch):
    def missing(_settings, _tenant):
        raise ValueError("租户在模型网关没有可用 key")

    monkeypatch.setattr(model_mapping.gateway, "api_key", missing)
    result = model_mapping.call(settings(), "tenant-a")
    assert result["ok"] is False and "entitlement" in result["detail"]
