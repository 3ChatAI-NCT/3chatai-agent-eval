import pytest
from pydantic import SecretStr

from agent_eval import gateway
from agent_eval.config import Settings


def settings():
    return Settings(database_url=SecretStr("postgresql://unused"), environment="development",
                    dev_identity=True, internal_tenant_id="internal-tenant")


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


@pytest.fixture(autouse=True)
def empty_key_cache():
    gateway._keys.clear()
    gateway._models[:] = [0.0, []]


def test_gateway_key_is_resolved_for_the_internal_tenant_and_cached(monkeypatch):
    calls = []

    def get(url, params, headers, timeout, follow_redirects):
        calls.append((url, params, headers))
        return Response({"code": 200, "data": {"entity": {"key": "tenant-gateway-key"}}})

    monkeypatch.setattr(gateway.httpx, "get", get)
    assert gateway.api_key(settings()) == "tenant-gateway-key"
    assert gateway.api_key(settings()) == "tenant-gateway-key"
    assert len(calls) == 1
    url, params, headers = calls[0]
    assert url.endswith("/api/entitlements/api-key/default")
    assert params == {"productSource": "3CHAT_BUILDER"}
    assert headers["TENANT_ID"] == "internal-tenant"


@pytest.mark.parametrize("body", [
    {"code": 200, "data": {"entity": None}},
    {"code": 500, "message": "entitlement unavailable"},
])
def test_missing_gateway_key_fails_without_a_fallback(monkeypatch, body):
    monkeypatch.setattr(gateway.httpx, "get", lambda *args, **kwargs: Response(body))
    with pytest.raises(ValueError, match="没有可用 key"):
        gateway.api_key(settings())


def test_chat_models_skip_non_chat_models_and_dedupe(monkeypatch):
    gateway._keys["internal-tenant"] = (float("inf"), "key")
    body = {"data": [{"channels": [
        {"provider": "DASHSCOPE", "models": ["qwen3.7-plus", "text-embedding-v4", "qwen3-rerank"]},
        {"provider": "OPENAI", "models": ["agent-model", "gpt-image-2"]},
        {"provider": "OPENAI", "models": ["qwen3.7-plus"]},
    ]}]}
    monkeypatch.setattr(gateway.httpx, "get", lambda *_args, **_kwargs: Response(body))
    assert gateway.chat_models(settings()) == [
        {"id": "agent-model", "provider": "OPENAI"},
        {"id": "qwen3.7-plus", "provider": "DASHSCOPE"},
    ]


def test_probe_reports_a_model_that_ignores_the_json_contract(monkeypatch):
    gateway._keys["internal-tenant"] = (float("inf"), "key")
    reply = {"choices": [{"message": {"content": "pong"}}]}
    monkeypatch.setattr(gateway.httpx, "post", lambda *_args, **_kwargs: Response(reply))
    result = gateway.probe(settings(), "qwen3.7-plus", 30)
    assert result["ok"] is False and "JSON" in result["detail"]


def test_probe_treats_an_empty_reasoning_reply_as_not_ok(monkeypatch):
    gateway._keys["internal-tenant"] = (float("inf"), "key")
    reply = {"choices": [{"message": {"content": None}}]}
    monkeypatch.setattr(gateway.httpx, "post", lambda *_args, **_kwargs: Response(reply))
    assert gateway.probe(settings(), "qwen3.7-plus", 30)["ok"] is False


def test_judge_body_turns_off_qwen_thinking_only():
    messages = [{"role": "user", "content": "x"}]
    qwen = gateway.judge_body("qwen3.7-plus", messages)
    assert qwen["enable_thinking"] is False and qwen["response_format"] == {"type": "json_object"}
    assert "enable_thinking" not in gateway.judge_body("claude-sonnet-5", messages)
    # 只支持思考模式的千问模型不接受关闭，否则网关返回 400
    assert "enable_thinking" not in gateway.judge_body("qwen3-235b-a22b-thinking-2507", messages)


def test_chat_models_are_cached_briefly(monkeypatch):
    gateway._keys["internal-tenant"] = (float("inf"), "key")
    calls = []

    def get(*_args, **_kwargs):
        calls.append(1)
        channel = {"provider": "OPENAI", "models": ["gpt-5.6-sol"]}
        return Response({"data": [{"channels": [channel]}]})

    monkeypatch.setattr(gateway.httpx, "get", get)
    assert gateway.chat_models(settings()) == gateway.chat_models(settings())
    assert len(calls) == 1
