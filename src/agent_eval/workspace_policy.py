"""只读获取 EVAL 空间业务配置；每次评测共享同一份判分依据。"""

import httpx

from golden_eval.adapters.platform_profile import (
    PlatformProfileReader,
    ProfileContractUnavailableError,
)
from golden_eval.domain.models import GoldenShared, RunIdentity

from . import registry


def prepare(job, store, settings, manifest):
    if "workspace_policy" in manifest:
        return manifest["workspace_policy"]
    # 沿用本次运行已固定的空间对；凭据读取会校验台账仍与这两个 ID 一致。
    space = job["input"]["config"]["space"]
    workspace_id = space["target_workspace_id"]
    try:
        _, token = registry.credentials(settings, space)
        contract = PlatformProfileReader(base_url=settings.platform_base_url,
            identity=RunIdentity(tenant_id=workspace_id, source="connection_settings"),
            token=token).read(workspace_id)
        if any(warning.startswith("guidance_config_unavailable:") for warning in contract.warnings):
            raise ValueError("guidance_unavailable")
    except (httpx.HTTPError, ProfileContractUnavailableError, ValueError) as exc:
        from .engines import ExecutionError

        # 尚未发送聊天，配置读取失败不能记为投递未知或 Agent 业务不通过。
        raise ExecutionError("workspace_rules_unavailable") from exc
    policy = {
        "workspace_id": workspace_id,
        "captured_at": contract.captured_at.isoformat(),
        "config_sha256": contract.target_config_hash,
        "basis": "target_workspace_read_for_this_evaluation",
        "execution_reused": manifest.get("execution_reused") is True,
        "context": {"guidance": contract.guidance, "skill_rules": contract.skill_rules,
                    "profile_schema": [field.model_dump(mode="json") for field in contract.fields]},
        "warnings": contract.warnings,
    }
    return store.freeze_workspace_policy(job, policy)


def judging_shared(raw, policy):
    """只替换判分配置，原参考、历史、画像预期和执行观测保持原样。"""

    shared = GoldenShared.model_validate(raw)
    if not policy:
        return shared
    data = shared.model_dump(mode="json")
    data["context"].update(policy["context"])
    return GoldenShared.model_validate(data)
