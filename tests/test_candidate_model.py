import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from agent_eval import engines, vault
from agent_eval.config import Settings
from agent_eval.models import CandidateIn, ExperimentIn, Mode

RESPONSE = {"model": "agent-model", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "有的"}]}],
            "usage": {"input_tokens": 100, "output_tokens": 8}}
CALL = {"call_id": "req-1:attempt:1", "workspace_id": "ws-1", "request_id": "req-1",
        "request": {"model": "agent-model", "input": [{"role": "user", "content": "周六有课吗"}],
                    "stream": False},
        "response": RESPONSE}


def settings():
    return Settings(database_url=SecretStr("postgresql://unused"), environment="development",
                    dev_identity=True, credential_key=SecretStr(Fernet.generate_key().decode()))


def atomic_job():
    return {"input": {"mode": Mode.ATOMIC, "item": CALL, "config": {
        "candidate_model": "candidate-model", "execution_target": "platform",
        "candidate_endpoint": "https://candidate.example.test/v1"}}}


def test_atomic_replay_calls_the_candidates_own_endpoint_not_the_gateway(monkeypatch):
    configured = settings()
    seen = {}

    class Client:
        def __init__(self, endpoint):
            seen["endpoint"] = endpoint

        def create(self, body):
            seen["model"] = body["model"]
            return {**RESPONSE, "model": "candidate-model"}

    monkeypatch.setattr(engines, "HttpResponsesClient", Client)
    monkeypatch.setattr(engines.gateway, "api_key",
                        lambda _settings: pytest.fail("候选模型不走网关"))
    engines.execute(atomic_job(), configured, vault.seal(configured, "candidate-key"))
    assert seen["endpoint"].base_url == "https://candidate.example.test/v1"
    assert seen["endpoint"].api_key.get_secret_value() == "candidate-key"
    assert seen["model"] == "candidate-model"


def test_gateway_candidate_uses_the_platform_gateway_key(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, endpoint):
            seen["endpoint"] = endpoint

        def create(self, body):
            return RESPONSE

    monkeypatch.setattr(engines, "HttpResponsesClient", Client)
    monkeypatch.setattr(engines.gateway, "api_key", lambda _settings: "tenant-gateway-key")
    job = atomic_job()
    job["input"]["config"] |= {"candidate_source": "gateway",
                               "candidate_endpoint": "http://gateway.example.test:8080/v1"}
    engines.execute(job, settings(), None)
    assert seen["endpoint"].base_url == "http://gateway.example.test:8080/v1"
    assert seen["endpoint"].api_key.get_secret_value() == "tenant-gateway-key"


def test_only_one_candidate_source_can_be_chosen():
    with pytest.raises(ValidationError, match="只能"):
        ExperimentIn(name="run", dataset_version_id="d", candidate_preset_id="candidate_1",
                     candidate_gateway_model="gpt-5.6-sol")


def test_atomic_replay_without_a_saved_key_is_rejected():
    with pytest.raises(ValueError, match="API Key"):
        engines.execute(atomic_job(), settings(), None)


@pytest.mark.parametrize(("url", "normalized"), [
    ("https://candidate.example.com/v1/responses", "https://candidate.example.com/v1"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1/", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
])
def test_candidate_endpoint_is_normalized(url, normalized):
    assert CandidateIn(base_url=url, model="m", api_key="k").base_url == normalized


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1", "https://gateway-internal:8080/v1",
    "https://gateway.example.svc.cluster.local/v1", "https://10.0.0.8/v1",
    "https://user:pass@api.example.com/v1", "https://api.example.com/v1?key=x",
])
def test_candidate_endpoint_must_be_a_public_https_domain(url):
    with pytest.raises(ValidationError):
        CandidateIn(base_url=url, model="m", api_key="k")
