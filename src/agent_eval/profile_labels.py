"""画像字段的中文名与说明：报告里把 tool_update_visitor_xxx 与字段键显示成空间配置的字段信息。"""

import logging
import time

from golden_eval.adapters.platform_profile import PlatformProfileReader
from golden_eval.domain.models import RunIdentity

from .registry import credentials

logger = logging.getLogger(__name__)

# 平台内置访客字段不在自定义字段列表里，名称固定
BUILT_IN = {key: {"label": label, "description": "平台内置访客字段"} for key, label in {
    "name": "姓名", "phoneNumber": "手机号", "email": "邮箱", "avatar": "头像", "language": "语言",
}.items()}
FRESH_SECONDS, RETRY_SECONDS = 600.0, 60.0

# 字段名只用于展示，按空间在进程内短暂缓存；读取失败也缓存一小段时间，避免每次打开报告都重试
_cache: dict[str, tuple[float, dict[str, dict]]] = {}


def profile_fields(settings, space: dict) -> dict[str, dict]:
    """源空间启用的自定义画像字段 → {label, description}；读取失败时只返回内置字段。"""

    cached = _cache.get(space["id"])
    if cached and cached[0] > time.monotonic():
        return cached[1]
    fields, ttl = dict(BUILT_IN), FRESH_SECONDS
    try:
        token, _ = credentials(settings, space)
        reader = PlatformProfileReader(
            base_url=settings.platform_base_url, token=token,
            identity=RunIdentity(tenant_id=space["source_workspace_id"],
                                 source="connection_settings"))
        fields |= {field.key: {"label": field.label if field.label != field.key else "",
                               "description": field.description}
                   for field in reader.defined_fields()}
    except Exception as exc:  # 台账、网关或平台接口任一不可用都只影响展示
        logger.warning("profile_labels_unavailable space_id=%s error_type=%s", space["id"],
                       type(exc).__name__)
        ttl = RETRY_SECONDS
    _cache[space["id"]] = (time.monotonic() + ttl, fields)
    return fields
