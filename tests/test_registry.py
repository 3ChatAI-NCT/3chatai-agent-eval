import pytest
from pydantic import SecretStr

from agent_eval import registry
from agent_eval.config import Settings
from agent_eval.registry import FIELDS, TOKEN_FIELDS, credentials, normalize, records

SPACE = {"id": "rec1", "source_workspace_id": "src", "target_workspace_id": "eval"}


def settings():
    return Settings(database_url=SecretStr("postgresql://unused"),
                    environment="development", dev_identity=True,
                    feishu_app_id="app", feishu_app_secret=SecretStr("secret"),
                    feishu_base_app_token="base", feishu_base_table_id="tbl")


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class Client:
    """Fake Feishu: records every search body and answers with the given rows."""

    def __init__(self, rows):
        self.rows = rows
        self.searches = []

    def post(self, url, json, params=None, headers=None):
        if url.endswith("/tenant_access_token/internal"):
            assert set(json) == {"app_id", "app_secret"}
            return Response({"code": 0, "tenant_access_token": "example"})
        assert url.endswith("/records/search")
        assert headers["Authorization"] == "Bearer example"
        self.searches.append(json)
        return Response({"code": 0, "data": {"items": self.rows, "has_more": False}})


@pytest.fixture(autouse=True)
def empty_credential_cache():
    registry._credentials.clear()


def row(**overrides):
    fields = {"基准名称": "Eval A", "源空间名称": "某某门店的空间", "源空间ID": "src",
              "基准空间ID": "eval", "状态": ["已验证"], "环境": ["sit"],
              "源空间AccessToken": "source-token", "基准空间AccessToken": "eval-token"}
    return {"record_id": "rec1", "fields": {**fields, **overrides}}


def test_registry_never_requests_the_password_column():
    assert "基准空间账号/密码" not in FIELDS + TOKEN_FIELDS
    client = Client([])
    records(settings(), client)
    assert "基准空间账号/密码" not in client.searches[0]["field_names"]


def test_verified_space_with_independent_tokens_is_active():
    record, reason = normalize(row(), settings())
    assert reason is None
    assert record == {"id": "rec1", "name": "Eval A", "source_name": "某某门店的空间",
                      "source_workspace_id": "src",
                      "target_workspace_id": "eval", "status": "active"}


@pytest.mark.parametrize("overrides", [
    {"状态": ["准备中"]},
    {"源空间AccessToken": ""},
    {"基准空间AccessToken": ""},
    {"基准空间AccessToken": "source-token"},
    {"基准空间ID": "src"},
])
def test_space_without_safe_tokens_needs_check(overrides):
    record, _ = normalize(row(**overrides), settings())
    assert record["status"] == "needs_check"


def test_credentials_are_read_by_registered_workspaces_and_cached():
    client = Client([row()])
    assert credentials(settings(), SPACE, client) == ("source-token", "eval-token")
    search = client.searches[0]
    assert search["field_names"] == TOKEN_FIELDS
    assert [(c["field_name"], c["value"]) for c in search["filter"]["conditions"]] == [
        ("源空间ID", ["src"]), ("基准空间ID", ["eval"])]
    assert credentials(settings(), SPACE, client) == ("source-token", "eval-token")
    assert len(client.searches) == 1


@pytest.mark.parametrize("rows", [
    [],
    [{**row(), "record_id": "rec-other"}],
    [row(基准空间AccessToken="source-token")],
])
def test_credentials_refuse_a_registry_that_no_longer_matches(rows):
    with pytest.raises(ValueError):
        credentials(settings(), SPACE, Client(rows))


def test_credentials_cache_does_not_cross_workspace_pairs():
    assert credentials(settings(), SPACE, Client([row()])) == ("source-token", "eval-token")
    changed = {**SPACE, "target_workspace_id": "new-eval"}
    client = Client([row(基准空间ID="new-eval", 基准空间AccessToken="new-eval-token")])
    assert credentials(settings(), changed, client) == ("source-token", "new-eval-token")
    assert client.searches[0]["filter"]["conditions"][1]["value"] == ["new-eval"]
