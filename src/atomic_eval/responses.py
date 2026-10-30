"""解析 OpenAI Responses 协议的请求与响应，抽出评审需要的部分。

这里只做结构抽取，不做任何业务判断。解析失败一律返回空值，由上层决定是否隔离该样本。
"""

from __future__ import annotations

import json
import re

from .models import Output, ToolCall

# 运行时上下文卡片有两种写法：独立的【用户长期信息】段落，或【用户信息补充】里的"长期信息："行。
# 字段定义（"长期字段定义："）只是 schema，不能当成客户已有事实。
_PROFILE_SECTION = re.compile(
    r"(?:【用户长期信息】|长期信息[：:])(.*?)(?=【|长期字段定义|长期画像记忆|\Z)", re.DOTALL
)
_SHORT_TERM = re.compile(r"短期信息[：:]\s*(\{.*?\})\s*(?:\n|长期)", re.DOTALL)
_STANDARD_KEYS = ("name", "phoneNumber", "email")
_EMPTY_VALUES = {"无", "空", "未知", "NULL", "null", "None", "-", ""}
_PROFILE_LINE = re.compile(
    r"^\s*[-•]?\s*(?P<key>[^：:（(]+?)\s*(?:（[^）]*）|\([^)]*\))?\s*[：:]\s*(?P<value>.+?)\s*$"
)
_IMAGE_HINT = re.compile(
    r"https?://\S+\.(?:png|jpe?g|webp|gif)|image_url|input_image|\[图片\]", re.IGNORECASE
)


def content_text(content) -> str:
    """把一条消息拍平成文本。

    图片不能直接丢：客户发了张图而历史里什么都看不到，评审器会把基于图片的后续动作
    误判成"编造事实"。这里保留一个可见的占位与链接，图片内容本身是否可判由维度规则决定。
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("input_text")
            if text:
                parts.append(text)
                continue
            url = item.get("image_url") or item.get("file_url") or item.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if url:
                parts.append(f"[客户发送了图片] {url}")
            elif item.get("type") in ("input_image", "input_file"):
                parts.append("[客户发送了图片]")
        return "\n".join(part for part in parts if part)
    return ""


def input_messages(request: dict) -> list[dict]:
    items = request.get("input")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


# 运行时上下文卡片和子任务输入都以 role=user 追加在真实客户消息之后，
# 直接取"最后一条 user 消息"会拿到系统生成的卡片，而不是客户说的话。
CARD_MARKERS = ("【运行时补充上下文卡片】", "【子任务调用输入】", "【用户信息补充】")
_TASK_INPUT = re.compile(r"任务输入[：:]\s*(?P<text>.+?)(?=\n【|\Z)", re.DOTALL)
_TASK_NAME = re.compile(r"任务名称[：:][ \t]*(?P<name>[^\n]+)")
# 主 Agent 的系统提示词里会成对写出技能 ID 与技能名，工具名就是那串数字 ID
_SKILL_PAIR = re.compile(
    r"skill_id[：:=]\s*`?(?P<id>[0-9a-zA-Z-]+)`?\s*[,，]\s*skill_name[：:=]\s*`?(?P<name>[^`\n，。；]+)`?"
)
# 部分空间在画像子任务提示词里写明「字段名：意向分层，记录工具：`tool_update_visitor_xxx`」
_FIELD_TOOL = re.compile(
    r"字段名[：:]\s*(?P<name>[^，,\n`]+?)\s*[，,]\s*记录工具[：:]\s*`?(?P<tool>[A-Za-z0-9_-]+)`?"
)
KNOWLEDGE_LABELS = {
    "knowledge_knowledgeAll_knowledgeAll": "知识库检索 · 全部知识库",
    "knowledge_knowledgeSelected_knowledgeSelected": "知识库检索 · 指定知识库",
}


def _is_card(text: str) -> bool:
    return text.lstrip().startswith(CARD_MARKERS)


def current_turn(request: dict) -> str:
    """当前轮客户消息：最后一条**不是系统卡片**的 user 消息。"""

    user_texts = [
        content_text(item.get("content"))
        for item in input_messages(request)
        if item.get("role") == "user"
    ]
    for text in reversed(user_texts):
        if text and not _is_card(text):
            return text
    return user_texts[-1] if user_texts else ""


def prior_turns(request: dict, *, limit: int = 20, per_message: int = 1500) -> list[dict]:
    """当前轮之前的真实对话。系统卡片与子任务输入不算对话内容。

    没有历史，评审器就无法判断"这一轮有没有触发事实"——客户回一个"2"是什么意思，
    只有看到上一条助手消息才知道。
    """

    turns: list[dict] = []
    for item in input_messages(request):
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        text = content_text(item.get("content")).strip()
        if not text or _is_card(text):
            continue
        turns.append({"role": role, "text": text[:per_message]})
    if turns and turns[-1]["role"] == "user":
        turns = turns[:-1]  # 最后一条客户消息是当前轮，单独给
    return turns[-limit:]


def business_rules(request: dict, *, limit: int = 6000) -> str:
    """租户在 system 里下发的业务规则。它是判定前置条件与依据的合同。"""

    for item in input_messages(request):
        if item.get("role") in ("system", "developer"):
            return content_text(item.get("content")).strip()[:limit]
    return ""


_SECTION_END = re.compile(r"\n[ \t]*(?:\*{3,}|-{3,}|#{1,3}[ \t])")


def tool_rules(request: dict, tools, *, per_tool: int = 4, width: int = 240,
               section_limit: int = 1500) -> dict[str, list[str]]:
    """提示词里与各工具相关的规则原文，供复核时核对写入条件与取值定义。

    有的空间为每个画像字段单列一节（字段名、记录工具、可选值及定义），整节摘出；
    其余空间只在规则里提到工具名，摘出提到它的那几行。
    """

    # 子任务的技能提示词常以 user 消息下发，所以扫描全部输入消息；工具名只出现在规则文本里
    text = "\n".join(content_text(item.get("content")) for item in input_messages(request))
    rules: dict[str, list[str]] = {}
    for tool in tools:
        section = re.search(rf"字段名[：:][^\n]*?{re.escape(tool)}", text)
        if section:
            rest = text[section.start():]
            end = _SECTION_END.search(rest, len(section.group(0)))
            body = rest[: end.start() if end else section_limit]
            rules[tool] = ["\n".join(line.strip() for line in body.splitlines() if line.strip())
                           [:section_limit]]
            continue
        lines: list[str] = []
        for line in text.splitlines():
            line = " ".join(line.split())
            if tool in line and line not in lines:
                lines.append(line[:width])
            if len(lines) >= per_tool:
                break
        if lines:
            rules[tool] = lines
    return rules


def task_input(request: dict) -> str:
    """子任务的执行指令：主 Agent 生成的【子任务调用输入】里的"任务输入"。

    它不是客户原话，但它决定了这一轮该做什么，判定触发必要性时必须看到。
    """

    for item in reversed(input_messages(request)):
        if item.get("role") != "user":
            continue
        text = content_text(item.get("content"))
        if text.lstrip().startswith("【子任务调用输入】"):
            matched = _TASK_INPUT.search(text)
            return (matched.group("text") if matched else text).strip()
    return ""


def task_name(request: dict) -> str:
    """子任务卡片里的"任务名称"。没有就返回空，由上层回退到任务类型。"""

    for item in reversed(input_messages(request)):
        text = content_text(item.get("content"))
        if not _is_card(text):
            continue
        matched = _TASK_NAME.search(text)
        if matched and matched.group("name").strip():
            return matched.group("name").strip()
    return ""


def tool_labels(request: dict) -> dict[str, str]:
    """工具名 → 可读标签。

    线上工具名是技能 ID 或 UUID，报告里直接显示等于没说。按四个来源取名字：
    系统提示词里的 skill_name、画像提示词里的「字段名…记录工具」、工具自己的描述首句、
    知识库工具的固定含义。画像字段的中文名以空间配置为准，这里只是读不到配置时的兜底。
    """

    labels: dict[str, str] = {}
    joined = "\n".join(content_text(item.get("content")) for item in input_messages(request))
    for matched in _SKILL_PAIR.finditer(joined):
        labels[matched.group("id")] = matched.group("name").strip()
    for matched in _FIELD_TOOL.finditer(joined):
        labels[matched.group("tool")] = matched.group("name").strip()
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        name = tool.get("name") or function.get("name")
        if not name:
            continue
        name = str(name)
        if name in KNOWLEDGE_LABELS:
            labels[name] = KNOWLEDGE_LABELS[name]
            continue
        if name in labels:
            continue
        description = (tool.get("description") or function.get("description") or "").strip()
        first = next((line.strip() for line in description.splitlines() if line.strip()), "")
        if first:
            labels[name] = first[:40]
    return labels


def available_tools(request: dict) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name") or (tool.get("function") or {}).get("name")
            if name:
                names.append(str(name))
    return names


def session_facts(request: dict) -> dict[str, str]:
    """短期信息：当前会话里已知的值，存活约一小时，**不代表已经写进画像档案**。

    模型把短期信息里的值写入联系人，是正常的持久化动作，不是重复写入。
    """

    blob = "\n".join(content_text(item.get("content")) for item in input_messages(request))
    short_term = _SHORT_TERM.search(blob)
    if not short_term:
        return {}
    try:
        data = json.loads(short_term.group(1))
    except json.JSONDecodeError:
        return {}
    facts: dict[str, str] = {}
    for key in _STANDARD_KEYS:
        value = data.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip() not in _EMPTY_VALUES:
            facts[key] = str(value).strip()
    for item in data.get("extra") or []:
        if not isinstance(item, dict):
            continue
        key, value = item.get("key"), item.get("value")
        if key and isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text not in _EMPTY_VALUES:
                facts[str(key)] = text
    return facts


def profile_snapshot(request: dict) -> dict[str, str]:
    """长期画像：已经持久化到联系人档案的字段值。

    只有它能用来判断"同值重复写入"。短期信息见 `session_facts`，
    字段定义只是 schema，两者都不进这里。
    """

    blob = "\n".join(content_text(item.get("content")) for item in input_messages(request))
    snapshot: dict[str, str] = {}
    section = _PROFILE_SECTION.search(blob)
    if section:
        for line in section.group(1).splitlines():
            matched = _PROFILE_LINE.match(line)
            if not matched:
                continue
            key = matched.group("key").strip()
            value = matched.group("value").strip()
            if key and value not in _EMPTY_VALUES:
                snapshot[key] = value
    return snapshot


def has_image(request: dict) -> bool:
    blob = json.dumps(request.get("input"), ensure_ascii=False) if request.get("input") else ""
    return bool(_IMAGE_HINT.search(blob))


def to_output(response: dict, side_id: str) -> Output:
    """把 Responses 的 output 数组拆成对客消息与工具调用。"""

    messages: list[str] = []
    calls: list[ToolCall] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            text = content_text(item.get("content"))
            if text:
                messages.append(text)
        elif kind in ("function_call", "custom_tool_call"):
            raw = item.get("arguments")
            arguments: dict = {}
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    arguments = parsed if isinstance(parsed, dict) else {"_raw": raw}
                except json.JSONDecodeError:
                    arguments = {"_raw": raw}
            elif isinstance(raw, dict):
                arguments = raw
            calls.append(ToolCall(name=str(item.get("name") or ""), arguments=arguments))
    return Output(side_id=side_id, messages=messages, tool_calls=calls)


def history_tool_calls(request: dict) -> list[ToolCall]:
    """历史轮次里已经发生过的工具调用，用于识别重复动作。"""

    calls: list[ToolCall] = []
    for item in input_messages(request):
        if item.get("type") in ("function_call", "custom_tool_call"):
            raw = item.get("arguments")
            arguments: dict = {}
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    arguments = parsed if isinstance(parsed, dict) else {"_raw": raw}
                except json.JSONDecodeError:
                    arguments = {"_raw": raw}
            calls.append(
                ToolCall(name=str(item.get("name") or ""), arguments=arguments, status="succeeded")
            )
    return calls
