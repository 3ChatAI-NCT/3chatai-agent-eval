"""Encrypt user-supplied model keys at rest with the platform credential key."""

from cryptography.fernet import Fernet, InvalidToken


def _fernet(settings) -> Fernet:
    key = settings.credential_key.get_secret_value()
    if not key:
        raise ValueError("平台未配置凭据加密密钥，无法保存候选模型 API Key")
    return Fernet(key.encode())


def seal(settings, value: str) -> str:
    return _fernet(settings).encrypt(value.encode()).decode()


def unseal(settings, token: str) -> str:
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("候选模型 API Key 无法解密，请重新创建评测") from exc
