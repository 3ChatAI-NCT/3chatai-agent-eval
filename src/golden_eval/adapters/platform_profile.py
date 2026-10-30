"""Read the target Workspace's profile surface from the platform configuration.

The verified rule (ported from the demo's `RuntimeProfileContract`): a profile field is usable when
the target Workspace exposes it (built-in visitor fields or enabled `CUSTOM` business-object fields)
*and* the published skills actually write it via `tool.update.visitor.<field>`. Fields that fail any
leg are reported with the reason instead of being silently dropped.

The two call families are kept apart because they authenticate differently: Butler configuration
carries the `Tenant_id` identity header, while the Universal business-object API is workspace
shared and is called with the Bearer token alone (verified 2026-09-09 against the target
workspace).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from ..domain.corpus import ProfileContract, ProfileFieldDefinition
from ..domain.models import RunIdentity
from ..integrations.platform_http import HttpTransport, PlatformHTTPError, UrllibTransport

SKILLS_CONFIG = "/api/butler/open/v2/agent/skills/config/both"
GUIDANCE_CONFIG = "/api/butler/open/v2/guidance/config/both"
OBJECT_LIST = "/api/universal/business/object/customize/list"
OBJECT_FIELD_LIST = "/api/universal/business/object/customize/field/list"

BUSINESS_OBJECT_KEY = "butlerVisitor"

GUIDANCE_STYLES: dict[str, str] = {
    "role": "role",
    "role_definition": "role",
    "communicationStyle": "communication",
    "communication_style": "communication",
    "conditions": "context-clarification",
    "context_clarification": "context-clarification",
    "handoffConditions": "handover-escalation",
    "handover_escalation": "handover-escalation",
    "appendix": "appendix",
}

# Built-in visitor fields the platform already exposes on every contact.
VISITOR_BASE_FIELDS: tuple[str, ...] = (
    "name",
    "email",
    "phoneNumber",
    "avatar",
    "language",
)

PROFILE_WRITE_PATTERN = re.compile(r"tool\.update\.visitor\.([A-Za-z][A-Za-z0-9_]*)")


class ProfileContractUnavailableError(RuntimeError):
    """The target configuration a run must read before it can judge profile fields."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class PlatformProfileReader:
    def __init__(
        self,
        *,
        base_url: str,
        identity: RunIdentity,
        token: str | None = None,
        transport: HttpTransport | None = None,
        golden_profile_keys: list[str] | None = None,
        universal_base_url: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # The Universal endpoints are workspace shared; they live on the same gateway in the
        # current deployment, so the connection base URL is the default.
        self._universal_base_url = (universal_base_url or self._base_url).rstrip("/")
        self._identity = identity
        self._token = token
        self._transport = transport or UrllibTransport()
        self._golden_keys = list(golden_profile_keys or [])

    def read(self, workspace_id: str) -> ProfileContract:
        warnings: list[str] = []
        # Skills and business-object fields are required: without them a field cannot be proven
        # storable, so the caller must degrade to the manual override instead of guessing.
        skills = self._required_get(SKILLS_CONFIG, "skills")
        if _data_list(skills) is None:
            raise ProfileContractUnavailableError("skills: data is not a list")
        defined = self._read_defined_fields()
        guidance, guidance_error = self._optional_get(GUIDANCE_CONFIG, "guidance")
        if guidance_error:
            warnings.append(f"guidance_config_unavailable: {guidance_error}")
        normalized_guidance = _normalize_guidance(guidance) if guidance is not None else []
        if guidance is not None and not normalized_guidance:
            warnings.append("guidance_system_empty")

        writable = _skill_writable_fields(skills)
        defined_keys = {field.key for field in defined}
        available = defined_keys | set(VISITOR_BASE_FIELDS)
        golden = set(self._golden_keys)

        fields: list[ProfileFieldDefinition] = []
        for field in defined:
            if golden and field.key not in golden:
                continue
            if field.key not in writable:
                continue
            fields.append(field)
        for key in VISITOR_BASE_FIELDS:
            if golden and key not in golden:
                continue
            if key not in writable or key in defined_keys:
                continue
            fields.append(
                ProfileFieldDefinition(
                    key=key,
                    label=key,
                    type="string",
                    description="platform built-in visitor field",
                    source="visitor_field",
                )
            )

        allowed_keys = {field.key for field in fields}
        candidates = sorted(golden) if golden else sorted(available)
        excluded: dict[str, list[str]] = {}
        for key in candidates:
            if key in allowed_keys:
                continue
            reasons: list[str] = []
            if key not in available:
                reasons.append("missing_from_target_workspace_profile_schema")
            if key not in writable:
                reasons.append("not_written_by_published_system_skills")
            excluded[key] = reasons or ["not_allowed_by_runtime_contract"]

        if not fields:
            warnings.append(
                "target Workspace 没有可用的画像字段：字段必须既存在于 target 配置，"
                "又被已发布技能通过 tool.update.visitor.<field> 写入。"
            )

        contract = ProfileContract(
            workspace_id=workspace_id,
            target_config_hash=_config_hash(skills, guidance, defined),
            fields=fields,
            excluded_fields=excluded,
            guidance_available=bool(normalized_guidance),
            guidance=(
                json.dumps(normalized_guidance, ensure_ascii=False, sort_keys=True)
                if normalized_guidance
                else None
            ),
            skill_rules=_skill_rules(skills),
            warnings=warnings,
            captured_at=datetime.now(UTC),
        )
        return contract

    def defined_fields(self) -> list[ProfileFieldDefinition]:
        """Enabled custom profile fields with their display labels, without the skill check."""

        return self._read_defined_fields()

    def _read_defined_fields(self) -> list[ProfileFieldDefinition]:
        objects = self._required_post(OBJECT_LIST, {"body": {}}, "business_objects")
        rows = _list_items(objects)
        visitor = [
            row
            for row in rows
            if row.get("apiName") == BUSINESS_OBJECT_KEY
            or row.get("entityKey") == BUSINESS_OBJECT_KEY
        ]
        if len(visitor) != 1:
            raise ProfileContractUnavailableError(
                f"expected one {BUSINESS_OBJECT_KEY} business object, found {len(visitor)}"
            )
        object_id = str(visitor[0].get("id") or visitor[0].get("objectId") or "").strip()
        if not object_id:
            raise ProfileContractUnavailableError(
                f"{BUSINESS_OBJECT_KEY} business object has no id"
            )
        payload = self._required_post(
            OBJECT_FIELD_LIST, {"body": {"businessObjectId": object_id}}, "profile_fields"
        )
        rows = [row for row in _list_items(payload) if _is_enabled_custom_field(row)]
        if not rows:
            raise ProfileContractUnavailableError(
                f"{OBJECT_FIELD_LIST} returned no enabled custom fields"
            )
        fields: list[ProfileFieldDefinition] = []
        for row in rows:
            key = str(row.get("apiName") or row.get("key") or "").strip()
            if not key:
                continue
            fields.append(
                ProfileFieldDefinition(
                    key=key,
                    label=str(row.get("name") or row.get("label") or key),
                    type=str(row.get("type") or row.get("fieldType") or "string"),
                    description=str(row.get("describe") or row.get("desc")
                                    or row.get("description") or ""),
                    value_format=str(row.get("valueFormat") or ""),
                    source="custom_field",
                )
            )
        if not fields:
            raise ProfileContractUnavailableError(
                f"{BUSINESS_OBJECT_KEY} exposes no custom profile field with an apiName"
            )
        return fields

    def _required_get(self, path: str, source: str) -> Any:
        try:
            payload = self._get(path)
        except PlatformHTTPError as exc:
            raise ProfileContractUnavailableError(f"{source}: {_brief(exc)}") from exc
        error = _envelope_error(payload)
        if error:
            raise ProfileContractUnavailableError(f"{source}: {error}")
        return payload

    def _optional_get(self, path: str, source: str) -> tuple[Any, str]:
        """Read a source the contract can live without, reporting the gap instead of raising."""

        try:
            payload = self._get(path)
        except PlatformHTTPError as exc:
            return (None, _brief(exc))
        error = _envelope_error(payload)
        if error:
            return (None, error)
        return (payload, "")

    def _required_post(self, path: str, payload: dict, source: str) -> Any:
        try:
            body = self._post(path, payload)
        except PlatformHTTPError as exc:
            raise ProfileContractUnavailableError(f"{source}: {_brief(exc)}") from exc
        error = _envelope_error(body)
        if error:
            raise ProfileContractUnavailableError(f"{source}: {error}")
        return body

    def _headers(self, *, identity_header: bool) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if identity_header:
            headers["Tenant_id"] = self._identity.tenant_id
            if self._identity.staff_id:
                headers["staff_id"] = self._identity.staff_id
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str) -> Any:
        return self._transport.get(
            f"{self._base_url}{path}", headers=self._headers(identity_header=True)
        )

    def _post(self, path: str, payload: dict) -> Any:
        # Universal is workspace shared and verified with the Bearer token alone; only the Butler
        # configuration endpoints take the tenant identity header.
        universal = path.startswith("/api/universal")
        base = self._universal_base_url if universal else self._base_url
        return self._transport.post(
            f"{base}{path}", headers=self._headers(identity_header=not universal), payload=payload
        )


def _brief(exc: BaseException) -> str:
    return str(exc)[:200]


def _envelope_error(payload: Any) -> str:
    """The platform answers 200 with an error envelope; treat that as a failed read."""

    if not isinstance(payload, dict):
        return "unexpected payload"
    code = payload.get("code")
    if code is None or str(code) in {"0", "200", "000000"}:
        return ""
    message = payload.get("message") or payload.get("msg") or payload.get("errorMsg") or ""
    return f"code={code} {str(message)[:120]}".strip()


def _is_enabled_custom_field(row: dict) -> bool:
    """Only enabled `CUSTOM` fields are target-defined profile fields (verified rule)."""

    create_mode = row.get("createMode")
    if create_mode is not None and str(create_mode).upper() != "CUSTOM":
        return False
    status = row.get("status")
    if status is not None and str(status).upper() not in {"ON", "ENABLE", "ENABLED"}:
        return False
    return True


def _skill_writable_fields(skills: Any) -> set[str]:
    """Fields the published system skills write, per the verified `tool.update.visitor` rule."""

    text = json.dumps(_published_skills(skills), ensure_ascii=False)
    return set(PROFILE_WRITE_PATTERN.findall(text))


def _published_skills(skills: Any) -> list[dict]:
    published = []
    for row in _list_items(skills):
        config = row.get("system")
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        if isinstance(config, dict):
            published.append(config)
    return published


def _skill_rules(skills: Any) -> list[dict[str, str]]:
    # 只传已发布的触发条件与执行规则，不混入 sandbox 或运行统计。
    return [{"name": str(config.get("title") or ""),
             "description": str(config.get("triggerPrompt") or ""),
             "instruction": str(config.get("instructionPrompt") or "")}
            for config in _published_skills(skills)
            if config.get("instructionPrompt")]


def _config_hash(skills: Any, guidance: Any, fields: list[ProfileFieldDefinition]) -> str:
    material = json.dumps(
        {
            "skills": skills,
            "guidance": guidance,
            "fields": [field.model_dump(mode="json") for field in fields],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _bounded_text(value: Any, *, text_limit: int = 4000) -> Any:
    if isinstance(value, str):
        return value if len(value) <= text_limit else f"{value[:text_limit]}...[truncated]"
    return value


def _guidance_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "prompt", "value", "content"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return ""


def _normalize_guidance(payload: Any) -> list[dict[str, str]]:
    """对齐 demo：只保留 system 版本、投影为 style/prompt 并截断，不存 envelope 原文。"""

    data = payload.get("data") if isinstance(payload, dict) else payload
    raw = data.get("system") if isinstance(data, dict) and "system" in data else data
    if isinstance(raw, list):
        return [
            {
                "style": str(item.get("style") or ""),
                "prompt": _bounded_text(str(item.get("prompt") or "")),
            }
            for item in raw
            if isinstance(item, dict) and str(item.get("prompt") or "").strip()
        ]
    if isinstance(raw, str):
        return [{"style": "system", "prompt": _bounded_text(raw)}] if raw.strip() else []
    if isinstance(raw, dict):
        prompts: list[dict[str, str]] = []
        for key, style in GUIDANCE_STYLES.items():
            if key not in raw:
                continue
            text = _guidance_text(raw[key])
            if text:
                prompts.append({"style": style, "prompt": _bounded_text(text)})
        return prompts
    return []


def _list_items(payload: Any) -> list[dict]:
    data = _data_list(payload)
    if data is None:
        return []
    return [row for row in data if isinstance(row, dict)]


def _data_list(payload: Any) -> list | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "records", "fields"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return None
