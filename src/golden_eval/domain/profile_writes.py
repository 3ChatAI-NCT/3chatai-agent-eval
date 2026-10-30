"""画像写入：平台日志里直接报告的联系人字段变更。

线上 AI 回复的 `agentLogs` 会带 `internalTool` 条目（实体 `visitor`），message 形如
`WztPq_c: "" -> "桌椅维修"`。这是平台自己记账的「我写了哪个字段、写成了什么」，因此这些字段
不必再让模型猜：金标构建阶段直接把它们当成本轮的画像增量，模型只补没有被平台写过的字段。

这个模块只负责把日志解析成 `{字段: 新值}`；字段是否属于 target 画像 schema 由调用方过滤，
因为解析发生在采集适配器里，而 schema 是构建金标时才知道的。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

PROFILE_WRITE_LOG_TYPE = "internalTool"
PROFILE_WRITE_LOG_NAME = "visitor"

# `key: "<old>" -> "<new>"`；箭头容忍 `->`、`=>` 与 `→`，值可以是引号字符串、JSON 字面量或裸文本。
_WRITE_PATTERN = re.compile(
    r"^\s*(?P<key>[^:\s][^:]*?)\s*:\s*(?P<old>.*?)\s*(?:->|=>|→)\s*(?P<new>.*?)\s*$"
)
_WRITE_SEPARATORS = re.compile(r"[;；\n]")
_NULLISH = {"", '""', "''", "null", "None", "NULL", "无"}


def profile_writes_from_logs(raw_logs: Iterable[Any] | None) -> dict[str, Any]:
    """`agentLogs` → `{画像字段: 新值}`；同一字段出现多次时以最后一条为准。

    只认平台明确记为「写联系人字段」的日志：类型 `internalTool`、名字 `visitor`。其它日志
    （task 的指令回显、knowledge、agent…）里也可能出现同样的文本，但那是提示词或检索结果，
    不是写入记录，必须忽略。
    """

    writes: dict[str, Any] = {}
    for item in raw_logs or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() != PROFILE_WRITE_LOG_TYPE:
            continue
        if str(item.get("name") or "").strip() != PROFILE_WRITE_LOG_NAME:
            continue
        for text in _write_texts(item):
            for part in _WRITE_SEPARATORS.split(text):
                match = _WRITE_PATTERN.match(part)
                if match is None:
                    continue
                key = match.group("key").strip()
                if key:
                    writes[key] = parse_write_value(match.group("new"))
    return writes


def parse_write_value(raw: str) -> Any:
    """写入值：引号字符串去引号，JSON 字面量按 JSON 解析，空值统一成空字符串。"""

    text = raw.strip()
    if text in _NULLISH:
        return ""
    if text[:1] in {'"', "[", "{"}:
        try:
            return json.loads(text)
        except ValueError:
            if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
                return text[1:-1].replace('\\"', '"')
    return text


def _write_texts(item: dict[str, Any]) -> list[str]:
    """一条日志里的待解析文本：`message` 与 `entity.values`（平台两种形状都出现过）。"""

    texts: list[str] = []
    message = item.get("message")
    if isinstance(message, str) and message.strip():
        texts.append(message)
    entity = item.get("entity")
    if isinstance(entity, dict):
        values = entity.get("values")
        if isinstance(values, list):
            texts.extend(value for value in values if isinstance(value, str) and value.strip())
    return texts
