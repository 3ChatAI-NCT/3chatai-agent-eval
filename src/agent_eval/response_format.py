"""按冻结请求中的 IM Schema 校验最终回复；不改写原始输出或历史 Judge 结论。"""

import json
import re
from collections import Counter
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from atomic_eval.responses import content_text, input_messages

SIDES = ("candidate", "baseline")


@lru_cache(maxsize=128)
def _schema(text):
    # 只读取 system/developer 中明确给出的 Schema，不从用户消息或模型输出反推约束。
    marker = re.search(r"JSON\s*Schema", text, re.IGNORECASE)
    if not marker:
        return None
    decoder = json.JSONDecoder()
    for start in re.finditer(r"\{", text[marker.end():]):
        try:
            value, _ = decoder.raw_decode(text, marker.end() + start.start())
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        properties = value.get("properties")
        messages = properties.get("messages") if isinstance(properties, dict) else None
        if (isinstance(messages, dict) and messages.get("type") == "array"
                and "messages" in value.get("required", [])):
            try:
                Draft202012Validator.check_schema(value)
            except SchemaError:
                return None
            # 空 Registry 禁止解析远程引用；仅使用请求中自带的 $defs。
            return Draft202012Validator(value, registry=Registry())
    return None


def _contract(request):
    for message in input_messages(request):
        if message.get("role") in ("system", "developer"):
            validator = _schema(content_text(message.get("content")))
            if validator is not None:
                return validator
    return None


def check(request, output):
    validator = _contract(request)
    if validator is None:
        return {"status": "not_applicable", "reason": "no_im_schema", "issues": []}
    if output is None:
        return {"status": "unavailable", "issues": []}
    if output.get("tool_calls"):
        return {"status": "not_applicable", "reason": "tool_call", "issues": []}
    raw = "\n".join(output.get("messages") or [])
    try:
        value = json.loads(raw)
    except ValueError:
        issues = [{"code": "invalid_json", "path": "/",
                   "summary": "要求严格 JSON，实际回复无法解析为 JSON"}]
    else:
        try:
            errors = list(validator.iter_errors(value))
        except Unresolvable:
            return {"status": "unavailable", "reason": "schema_reference", "issues": []}
        issues = []
        for error in errors:
            path = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                    for part in error.absolute_path)
            string_messages = path == "/messages" and isinstance(error.instance, str)
            # 不引用错误正文（可能含客户信息）；只展示字段路径和违反的 Schema 规则。
            description = "messages 应为数组，实际返回字符串" if string_messages else (
                f"{path} 不符合输入 Schema 的 {error.validator} 约束")
            issues.append({"code": "messages_string" if string_messages else "schema_violation",
                           "path": path, "summary": description})
    return {"status": "invalid" if issues else "valid",
            "issues": [{**issue, "severity": "major"} for issue in issues]}


def annotate(request, result):
    if result is None:
        return None
    return {**result, **{side: {**result[side], "format_check": check(request, result[side])}
                         for side in SIDES if result.get(side) is not None}}


def execution_checks(job):
    if "format_checks" in job:
        return job["format_checks"]
    request = job["input"]["item"].get("request") or {}
    result = job.get("result") or {}
    return {side: check(request, result.get(side)) for side in SIDES}


def summarize(executions):
    counts = {side: Counter() for side in SIDES}
    for job in executions:
        for side, result in execution_checks(job).items():
            own = counts[side]
            own[result["status"]] += 1
            if result["status"] == "invalid":
                # 同一次响应的同类问题只计一次，避免多条 Schema 错误抬高分子。
                own.update({issue["code"] for issue in result["issues"]})
    return {side: {"checked": value["valid"] + value["invalid"],
                   **{key: value[key] for key in ("invalid", "messages_string", "invalid_json",
                                                 "not_applicable", "unavailable")}}
            for side, value in counts.items()}
