from pathlib import Path

from agent_eval.config import Settings


def test_service_addresses_require_explicit_configuration():
    settings = Settings(database_url="postgresql://unused", environment="development",
                        dev_identity=True, _env_file=None)
    for field in ("model_gateway_url", "entitlement_url", "model_mapping_url",
                  "platform_base_url", "public_origin", "oss_endpoint", "media_allowed_hosts"):
        assert getattr(settings, field) == "", field


def test_environment_example_contains_no_connection_or_credential_values():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    values = dict(line.split("=", 1) for line in example.read_text().splitlines()
                  if line and not line.startswith("#"))
    assert "EVAL_DATABASE_URL" in values
    for key, value in values.items():
        if key.endswith(("_URL", "_ENDPOINT", "_KEY", "_SECRET", "_TOKEN", "_ACCESS_KEY_ID")):
            assert value == "", key
