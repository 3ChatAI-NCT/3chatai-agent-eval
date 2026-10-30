"""成对盲评：同一请求的基线输出（生产记录）与候选输出放在一起比较，交换 A/B 顺序独立评审两次。

两次结论一致才成为定论；不一致标为评审分歧，交由人工复核。代码负责盲化、引用校验与聚合，
模型只做语义判断。双方以角色名 baseline（基线）/ candidate（候选）标识。
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .checks import run_checks
from .models import EvalCase, Output

PROMPT_PATH = Path(__file__).parent / "prompts" / "pairwise-business-v3.1.md"
PROMPT_VERSION = "atomic-pairwise-business-v3.1"
DIMENSIONS = ("instruction_following", "groundedness", "task_completion", "tool_use",
              "communication")
SEVERITY_RANK = {"critical": 3, "major": 2, "minor": 1}
RELATIVE = {"candidate": "G", "tie": "S", "baseline": "B", "insufficient_evidence": "U"}
SIDES = ("candidate", "baseline")

Grade = Literal["pass", "minor", "major", "critical", "not_applicable", "insufficient_evidence"]
Dimension = Literal["instruction_following", "groundedness", "task_completion", "tool_use",
                    "communication"]


class JudgeError(RuntimeError):
    """评审模型的输出不可用；与网络失败区分，不自动重试。"""


class _Judged(BaseModel):
    # 模型偶尔附带协议外字段（实测有 reasoning），忽略即可；必填字段与枚举仍严格校验
    model_config = ConfigDict(extra="ignore")


class Citation(_Judged):
    pointer: str
    quote: str


class JudgedDimension(_Judged):
    dimension: Dimension
    A: Grade
    B: Grade
    verdict: Literal["A", "B", "tie", "not_applicable", "insufficient_evidence"]
    note: str = ""


class JudgedIssue(_Judged):
    side: Literal["A", "B"]
    dimension: Dimension
    severity: Literal["minor", "major", "critical"]
    description: str
    citations: list[Citation] = Field(min_length=1)


class Review(_Judged):
    task_type: Literal["customer_reply", "task_routing", "profile_update", "knowledge_lookup",
                       "internal_task", "other"]
    expected_action: str
    winner: Literal["A", "B", "tie", "insufficient_evidence"]
    confidence: Literal["high", "medium", "low"]
    rationale: str
    dimensions: list[JudgedDimension]
    issues: list[JudgedIssue] = Field(default_factory=list)
    contract_conflicts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def prompt() -> dict:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    return {"version": PROMPT_VERSION, "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _view(output: Output) -> dict:
    return {"messages": list(output.messages),
            "calls": [{"name": call.name, "arguments": call.arguments}
                      for call in output.tool_calls]}


def rule_checks(case: EvalCase, side: str, label: str | None = None) -> list[dict]:
    """代码确定性检查；label 给出时把引用改写成盲评 packet 里的 /A、/B 路径。"""

    prefix = f"/{label or side}/calls/"
    return [{"category": issue.dimension, "severity": str(issue.severity),
             "summary": issue.summary,
             "pointer": issue.evidence_ref.replace(f"/outputs/{side}/tool_calls/", prefix)}
            for issue in run_checks(case, side)]


def packet(case: EvalCase, request: dict, order: dict[str, str]) -> dict:
    """order 形如 {"A": "candidate", "B": "baseline"}；请求中的模型名被移除以隐藏身份。"""

    body: dict = {"request": {key: value for key, value in request.items() if key != "model"}}
    for label, side in order.items():
        body[label] = _view(case.output_for(side))
    body["rule_checks"] = {label: rule_checks(case, side, label) for label, side in order.items()}
    return body


def _leaves(value, path: str = ""):
    """逐个产出 (JSON Pointer, 文本)；非字符串的标量按 JSON 文本参与比对。"""

    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from _leaves(item, f"{path}/{token}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaves(item, f"{path}/{index}")
    else:
        yield path, json.dumps(value, ensure_ascii=False)


def _compact(text: str) -> str:
    return "".join(text.split())


ELLIPSIS = re.compile(r"\.{3,}|…+")


def _contains(text: str, parts: list[str]) -> bool:
    position = 0
    for part in parts:
        found = text.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def locate(document, citation: Citation) -> str | None:
    """在引用声明的来源（request、A、B 或 rule_checks）内查找原文，返回实际位置。

    实测评审常把请求中的规则原文引对、但把消息序号数错，所以只要求引用出现在声明的来源里，
    并把指针改写为真实位置；来源之外或原文中不存在的引用一律不成立。
    评审也常用省略号缩短长引用：按省略号切开后，每一段都须原样且按顺序出现在同一处原文中。
    """

    root = citation.pointer.split("/")[1] if citation.pointer.startswith("/") else ""
    parts = [_compact(part) for part in ELLIPSIS.split(citation.quote)]
    parts = [part for part in parts if part]
    if root not in document or not parts or any(len(part) < 2 for part in parts):
        return None
    for path, text in _leaves(document[root], f"/{root}"):
        if _contains(_compact(text), parts):
            return path
    # 工具参数常被重新序列化成 JSON 引用，允许与该侧调用的 JSON 文本比对
    if root in ("A", "B") and _contains(_compact(json.dumps(document[root], ensure_ascii=False)),
                                        parts):
        return f"/{root}"
    return None


def _parse(raw: str) -> Review:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
        # 模型偶尔把结果包进单个外层键（实测为 {"json": {...}}）
        if isinstance(data, dict) and len(data) == 1 and "winner" not in data:
            inner = next(iter(data.values()))
            data = inner if isinstance(inner, dict) else data
        review = Review.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise JudgeError("judge_schema_error") from exc
    if sorted(item.dimension for item in review.dimensions) != sorted(DIMENSIONS):
        raise JudgeError("judge_dimensions_incomplete")
    return review


def _review(judge, body: dict, order: dict[str, str], system_prompt: str,
            attempts: int) -> dict:
    """评审一次，把 A/B 映射回 candidate/baseline；引用不成立的问题作废，但计数留痕。"""

    # 输出格式偶发漂移（外层包裹、缺字段），最多请求 attempts 次，仍不合格才记为失败
    for attempt in range(attempts):
        try:
            review = _parse(judge.complete(system_prompt, body))
            break
        except JudgeError:
            if attempt == attempts - 1:
                raise
    kept, rejected = [], 0
    for issue in review.issues:
        located = [locate(body, citation) for citation in issue.citations]
        on_side = any(path and path.startswith(f"/{issue.side}") for path in located)
        if all(located) and on_side:
            kept.append({"side": order[issue.side], "dimension": issue.dimension,
                         "severity": issue.severity, "description": issue.description,
                         "citations": [{"pointer": path, "quote": citation.quote}
                                       for path, citation in zip(located, issue.citations)]})
        else:
            rejected += 1
    return {
        "order": order, "task_type": review.task_type, "expected_action": review.expected_action,
        "winner": order.get(review.winner, review.winner), "confidence": review.confidence,
        "rationale": review.rationale,
        "dimensions": [{"dimension": item.dimension, order["A"]: item.A, order["B"]: item.B,
                        "verdict": order.get(item.verdict, item.verdict), "note": item.note}
                       for item in sorted(review.dimensions,
                                          key=lambda item: DIMENSIONS.index(item.dimension))],
        "issues": kept, "rejected_issues": rejected,
        "contract_conflicts": review.contract_conflicts, "limitations": review.limitations,
    }


def _worst(reviews: list[dict], side: str) -> str:
    """任一评审指出的最高严重度；用于筛出需要核对的案例，不等于已证实的错误。"""

    rank = max((SEVERITY_RANK[issue["severity"]] for review in reviews
                for issue in review["issues"] if issue["side"] == side), default=0)
    return next((name for name, value in SEVERITY_RANK.items() if value == rank), "none")


def _in_parallel(review, orders):
    """两次评审互不依赖，并行调用使单条样本的等待时间减半。"""

    with ThreadPoolExecutor(max_workers=len(orders)) as pool:
        return list(pool.map(review, orders))


def grade_pair(case: EvalCase, request: dict, judge, *, seed: str, system_prompt: str,
               format_attempts: int = 3) -> dict:
    candidate, baseline = case.output_for("candidate"), case.output_for("baseline")
    if _view(candidate) == _view(baseline):
        # 两侧一字不差：结论只能是持平；只做一次业务合规检查，问题同时适用于双方
        order = {"A": "candidate", "B": "baseline"}
        review = _review(judge, packet(case, request, order), order, system_prompt,
                         format_attempts)
        shared = [issue for issue in review["issues"] if issue["side"] == "candidate"]
        review = {**review, "winner": "tie",
                  "issues": shared + [{**issue, "side": "baseline"} for issue in shared]}
        reviews, relative, agreement = [review], "S", None
        reason = "双方输出完全一致，只做一次业务合规检查，结论为持平"
    else:
        # A/B 位置由案例决定且可复现；第二次交换位置，用来发现位置偏差
        flip = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 2
        first = {"A": SIDES[flip], "B": SIDES[1 - flip]}
        second = {"A": first["B"], "B": first["A"]}
        reviews = _in_parallel(lambda order: _review(judge, packet(case, request, order), order,
                                                     system_prompt, format_attempts),
                               (first, second))
        agreement = reviews[0]["winner"] == reviews[1]["winner"]
        relative = RELATIVE[reviews[0]["winner"]] if agreement else "D"
        reason = "两次盲评结论一致" if agreement else "交换顺序后两次盲评结论不一致，需要人工复核"
    return {
        "verdict": {"relative": relative, "reason": reason, "agreement": agreement,
                    "identical": agreement is None},
        "task_type": reviews[0]["task_type"],
        "reviews": reviews,
        "worst": {side: _worst(reviews, side) for side in SIDES},
        "rule_checks": {side: rule_checks(case, side) for side in SIDES},
    }
