"""Credential filtering for authenticated evaluation views and reports."""

import json
import re

PRIVATE_KEYS = {"token", "apikey", "authorization", "accountid", "visitorid",
                "rawagentlogs", "rawresponse", "requestbody", "tokenenv",
                "accesstoken", "accesskeyid", "accesskeysecret", "clientsecret",
                "password", "cookie", "setcookie", "credential", "secret"}
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{10,}", re.I)
API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def safe_view(value):
    if isinstance(value, dict):
        return {key: safe_view(item) for key, item in value.items()
                if key.lower().replace("_", "").replace("-", "") not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [safe_view(item) for item in value]
    if isinstance(value, str):
        return API_KEY.sub("[密钥]", BEARER.sub("[凭据]", value))
    return value


def evidence_view(value):
    """证据包保留请求体、原始响应和业务标识，递归过滤凭据及嵌套 JSON 字符串。"""

    hidden = (PRIVATE_KEYS - {"rawagentlogs", "rawresponse", "requestbody",
                              "accountid", "visitorid"}) | {
        "sealedkey", "candidateapikey", "gatewaytoken", "xagentevalgatewaytoken", "xapikey",
        "proxyauthorization", "securitytoken", "sessiontoken", "refreshtoken", "leasetoken",
        "ossaccesskeyid", "ossaccesskeysecret", "osssecuritytoken", "secretaccesskey",
        "credentialkey", "encryptionkey",
    }
    if isinstance(value, dict):
        return {key: evidence_view(item) for key, item in value.items()
                if key.lower().replace("_", "").replace("-", "") not in hidden}
    if isinstance(value, (list, tuple)):
        return [evidence_view(item) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                filtered = evidence_view(parsed)
                if filtered != parsed:
                    return json.dumps(filtered, ensure_ascii=False)
        return API_KEY.sub("[密钥]", BEARER.sub("[凭据]", value))
    return value
