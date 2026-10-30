from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVAL_", env_file=".env", extra="ignore")

    environment: Literal["development", "production"] = "production"
    database_url: SecretStr
    internal_tenant_id: str = ""
    member_staff_ids: set[str] = set()
    public_origin: str = ""
    gateway_token: SecretStr = SecretStr("")
    dev_identity: bool = False
    worker_concurrency: int = Field(default=4, ge=1, le=10)
    space_concurrency: int = Field(default=2, ge=1, le=10)
    lease_seconds: int = Field(default=120, ge=30, le=3600)
    # 模型调用链路：轮次结束后多久开始到网关日志查询、最晚查到多久为止
    trace_settle_seconds: int = Field(default=120, ge=0, le=3600)
    trace_deadline_seconds: int = Field(default=1800, ge=60, le=86400)
    model_gateway_url: str = ""
    entitlement_url: str = ""
    entitlement_product_source: str = "3CHAT_BUILDER"
    # EVAL 空间执行模型：网关按租户把 Agent 固定请求的模型替换为空间配置的模型
    model_mapping_url: str = ""
    mapping_from_model: str = ""
    judge_model: str = ""
    annotation_model: str = ""
    credential_key: SecretStr = SecretStr("")
    media_allowed_hosts: str = ""
    platform_base_url: str = ""
    platform_http_timeout_seconds: int = Field(default=180, ge=30, le=600)
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_base_app_token: str = ""
    feishu_base_table_id: str = ""
    feishu_base_view_id: str = ""
    storage_backend: Literal["local", "oss"] = "local"
    object_root: str = ".local/objects"
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_prefix: str = "3chat-agent-eval/"
    oss_access_key_id: SecretStr = SecretStr("")
    oss_access_key_secret: SecretStr = SecretStr("")
    oss_security_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_environment(self):
        if self.dev_identity and self.environment != "development":
            raise ValueError("dev_identity requires development environment")
        if not self.dev_identity and (not self.internal_tenant_id or not self.member_staff_ids):
            raise ValueError("internal tenant and member staff IDs are required")
        if self.environment == "production" and self.storage_backend != "oss":
            raise ValueError("production requires OSS object storage")
        if self.environment == "production" and not self.public_origin.startswith("https://"):
            raise ValueError("production requires a HTTPS public origin")
        if self.storage_backend == "oss" and not (self.oss_endpoint.startswith("https://")
                and self.oss_bucket and self.oss_access_key_id.get_secret_value()
                and self.oss_access_key_secret.get_secret_value()):
            raise ValueError("OSS requires HTTPS endpoint, bucket, and server-side credentials")
        if (not self.oss_prefix.endswith("/") or self.oss_prefix.startswith("/")
                or any(part in ("", ".", "..") for part in self.oss_prefix[:-1].split("/"))):
            raise ValueError("OSS prefix must be a relative directory")
        return self
