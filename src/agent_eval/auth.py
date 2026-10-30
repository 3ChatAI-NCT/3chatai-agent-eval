import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    staff_id: str

    @property
    def subject(self) -> str:
        return f"{self.tenant_id}:{self.staff_id}"


GATEWAY_TOKEN_HEADER = "x-agent-eval-gateway-token"


def authenticate(request: Request) -> Principal:
    settings = request.app.state.settings
    if settings.dev_identity:
        return Principal("local", "developer")

    # 共享口令用于校验可信网关；部署时还应限制 API 的直接网络访问。
    expected = settings.gateway_token.get_secret_value()
    if expected:
        supplied = request.headers.getlist(GATEWAY_TOKEN_HEADER)
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0], expected):
            raise HTTPException(403, "仅限已授权的内部成员访问")

    tenants = request.headers.getlist("tenant_id")
    staffs = request.headers.getlist("staff_id")
    # 身份头只能由 Gateway 写入且只有一个值；出现多个说明客户端伪造的头没被清掉
    if len(tenants) != 1 or len(staffs) != 1:
        raise HTTPException(403, "仅限已授权的内部成员访问")
    tenant_id, staff_id = tenants[0].strip(), staffs[0].strip()
    # 成员名单写 "*" 表示内部租户下所有登录成员都可使用；租户本身始终必须是内部租户，
    # 因为客户也走同一个登录体系，而平台里存有多个客户的真实会话样本。
    members = settings.member_staff_ids
    allowed = staff_id and ("*" in members or staff_id in members)
    if tenant_id != settings.internal_tenant_id or not allowed:
        raise HTTPException(403, "仅限已授权的内部成员访问")
    return Principal(tenant_id, staff_id)
