"""身份校验：只认 Gateway 写入的内部租户身份。"""

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.datastructures import Headers

from agent_eval.auth import authenticate
from agent_eval.config import Settings


class FakeRequest:
    def __init__(self, members: set[str], headers: list[tuple[str, str]], token: str = ""):
        settings = Settings(database_url=SecretStr("postgresql://x/y"), environment="development",
                            internal_tenant_id="internal", member_staff_ids=members,
                            gateway_token=SecretStr(token))
        self.app = type("App", (), {"state": type("State", (), {"settings": settings})()})()
        self.headers = Headers(raw=[(k.encode(), v.encode()) for k, v in headers])


def call(members, headers, token=""):
    return authenticate(FakeRequest(members, headers, token))


def test_wildcard_admits_every_staff_of_the_internal_tenant():
    principal = call({"*"}, [("tenant_id", "internal"), ("staff_id", "anyone")])
    assert principal.subject == "internal:anyone"


@pytest.mark.parametrize("headers", [
    [("tenant_id", "customer"), ("staff_id", "anyone")],  # 客户租户即使开放全员也不行
    [("tenant_id", "internal"), ("staff_id", "")],  # 没有 staff 身份
    [("tenant_id", "internal")],  # 缺头
    [("tenant_id", "customer"), ("tenant_id", "internal"), ("staff_id", "x")],  # 伪造头没被覆盖
])
def test_wildcard_never_opens_it_beyond_the_internal_tenant(headers):
    with pytest.raises(HTTPException) as caught:
        call({"*"}, headers)
    assert caught.value.status_code == 403


def test_explicit_member_list_still_works():
    assert call({"s1"}, [("tenant_id", "internal"), ("staff_id", "s1")]).subject == "internal:s1"
    with pytest.raises(HTTPException):
        call({"s1"}, [("tenant_id", "internal"), ("staff_id", "s2")])


IDENTITY = [("tenant_id", "internal"), ("staff_id", "s1")]


def test_gateway_token_blocks_requests_that_bypass_the_gateway():
    """ECI 上 NetworkPolicy 不生效：配置口令后，没有经过 Gateway 的请求即使带着合法身份头也拒绝。"""

    with pytest.raises(HTTPException):
        call({"*"}, IDENTITY, token="secret-from-gateway")
    with pytest.raises(HTTPException):
        call({"*"}, IDENTITY + [("x-agent-eval-gateway-token", "guess")],
             token="secret-from-gateway")
    principal = call({"*"}, IDENTITY + [("x-agent-eval-gateway-token", "secret-from-gateway")],
                     token="secret-from-gateway")
    assert principal.subject == "internal:s1"


def test_without_a_configured_token_behaviour_is_unchanged():
    assert call({"*"}, IDENTITY).subject == "internal:s1"
