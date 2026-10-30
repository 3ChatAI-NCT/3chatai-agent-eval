"""运行时配置，全部来自环境变量（本地开发可用 .env，容器里用 Secret 注入）。

密钥用 SecretStr 包住，避免随手 print 或写进 manifest。缺变量时一次性报出缺哪些，
不要让人在容器里一个个试。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, SecretStr


class ConfigError(RuntimeError):
    pass


def _require(prefix: str, names: dict[str, str]) -> dict[str, str]:
    values, missing = {}, []
    for field, env_name in names.items():
        value = os.environ.get(env_name, "").strip()
        if not value:
            missing.append(env_name)
        values[field] = value
    if missing:
        raise ConfigError(f"{prefix} 缺少环境变量: {', '.join(missing)}")
    return values


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlsSettings(Strict):
    endpoint: str
    project: str
    logstore: str
    access_key_id: SecretStr
    access_key_secret: SecretStr
    security_token: SecretStr | None = None

    @classmethod
    def from_env(cls) -> SlsSettings:
        values = _require(
            "SLS",
            {
                "endpoint": "SLS_ENDPOINT",
                "project": "SLS_PROJECT",
                "logstore": "SLS_LOGSTORE",
                "access_key_id": "ALIBABA_CLOUD_ACCESS_KEY_ID",
                "access_key_secret": "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
            },
        )
        endpoint = values["endpoint"]
        if not endpoint.startswith("https://"):
            raise ConfigError("SLS_ENDPOINT 必须是 https 地址")
        token = os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", "").strip()
        return cls(
            endpoint=endpoint,
            project=values["project"],
            logstore=values["logstore"],
            access_key_id=SecretStr(values["access_key_id"]),
            access_key_secret=SecretStr(values["access_key_secret"]),
            security_token=SecretStr(token) if token else None,
        )


class ModelEndpoint(Strict):
    """评审模型或候选模型的调用配置。"""

    base_url: str
    model: str
    api_key: SecretStr

    @classmethod
    def from_env(cls, prefix: str) -> ModelEndpoint:
        values = _require(
            prefix,
            {
                "base_url": f"{prefix}_BASE_URL",
                "model": f"{prefix}_MODEL",
                "api_key": f"{prefix}_API_KEY",
            },
        )
        return cls(
            base_url=values["base_url"].rstrip("/"),
            model=values["model"],
            api_key=SecretStr(values["api_key"]),
        )


class Limits(Strict):
    """服务端固定的保护性上限，用户不可在请求里覆盖。"""

    max_sample_size: int = 300
    max_scan_records: int = 20000
    page_size: int = 100
    segment_cap: int = 1000
    min_window_delay_seconds: int = 600
    log_retention_days: int = 7  # SLS 只保留最近 7 天

    @classmethod
    def from_env(cls) -> Limits:
        def read(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} 必须是整数") from exc

        return cls(
            max_sample_size=read("EVAL_MAX_SAMPLE_SIZE", 300),
            max_scan_records=read("EVAL_MAX_SCAN_RECORDS", 20000),
            page_size=read("EVAL_PAGE_SIZE", 100),
            segment_cap=read("EVAL_SEGMENT_CAP", 1000),
            min_window_delay_seconds=read("EVAL_MIN_WINDOW_DELAY_SECONDS", 600),
            log_retention_days=read("EVAL_LOG_RETENTION_DAYS", 7),
        )
