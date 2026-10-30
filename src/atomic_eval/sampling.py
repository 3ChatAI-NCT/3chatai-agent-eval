"""采样漏斗：筛掉不可用记录，按分层抽样，结果可复现。

抽样用"种子 + 稳定哈希"而不是随机数：同样的种子与候选集永远得到同一批样本，
与记录顺序无关，也不会因为候选集新增记录而打乱已选中的样本。
"""

from __future__ import annotations

import hashlib
import heapq
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import EvalCase, ModelCall
from .responses import (
    available_tools,
    business_rules,
    current_turn,
    has_image,
    history_tool_calls,
    prior_turns,
    profile_snapshot,
    session_facts,
    task_input,
    task_name,
    to_output,
    tool_labels,
)

DEPTH_BUCKETS = ((3, "短"), (9, "中"), (10**9, "长"))


def depth_bucket(message_count: int) -> str:
    for limit, label in DEPTH_BUCKETS:
        if message_count <= limit:
            return label
    return "长"


def stratum_of(call: ModelCall, task_type: str = "unknown") -> str:
    """分层键：只用请求里能直接算出来的信号。

    精确任务名需要 skill_id→名称映射，属于外部配置；没有时用 unknown，不参与分层。
    """

    messages = call.request.get("input") or []
    tool_calls = [
        item
        for item in (call.response.get("output") or [])
        if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call")
    ]
    parts = [
        task_type,
        "有工具" if tool_calls else "无工具",
        "有图" if has_image(call.request) else "无图",
        depth_bucket(len(messages)),
    ]
    return "|".join(parts)


def _rank(seed: str, call_id: str) -> str:
    return hashlib.sha256(f"{seed}:{call_id}".encode()).hexdigest()


def _allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """按各层占比分配名额，余数给小数部分最大的层（最大余数法）。"""

    population = sum(sizes.values())
    if population == 0 or total <= 0:
        return {key: 0 for key in sizes}
    quotas = {key: min(count, total * count / population) for key, count in sizes.items()}
    allocation = {key: int(value) for key, value in quotas.items()}
    remainder = total - sum(allocation.values())
    leftovers = sorted(sizes, key=lambda key: (-(quotas[key] - allocation[key]), key))
    index = 0
    while remainder > 0 and index < len(leftovers) * 2:
        key = leftovers[index % len(leftovers)]
        if allocation[key] < sizes[key]:
            allocation[key] += 1
            remainder -= 1
        index += 1
    return allocation


@dataclass(frozen=True)
class SampleResult:
    selected: list[ModelCall]
    excluded: list[dict]
    strata: dict[str, int] = field(default_factory=dict)
    selected_strata: dict[str, int] = field(default_factory=dict)
    shortfall: int = 0

    @property
    def funnel(self) -> dict:
        reasons = Counter(item["reason"] for item in self.excluded)
        return {
            "eligible": sum(self.strata.values()),
            "selected": len(self.selected),
            "shortfall": self.shortfall,
            "excluded": dict(reasons),
            "strata": self.strata,
            "selected_strata": self.selected_strata,
        }


def exclusion(
    call: ModelCall, *, workspace_ids: list[str] | None = None, model: str | None = None
) -> str | None:
    if workspace_ids and call.workspace_id not in workspace_ids:
        return "workspace_mismatch"
    if model and call.request.get("model") != model:
        return "model_mismatch"
    if not (call.response.get("output") or []):
        return "empty_output"
    if not current_turn(call.request):
        return "no_current_turn"
    return None


class Sampler:
    """逐条接收候选的分层抽样。

    任一层分到的名额都不超过 size，所以每层只需保留排名最前的 size 条，
    内存只与样本量有关，与窗口内的调用总量无关。
    """

    def __init__(
        self,
        *,
        size: int,
        seed: str,
        workspace_ids: list[str] | None = None,
        model: str | None = None,
        stratify: bool = True,
    ) -> None:
        self.size, self.seed, self.stratify = size, seed, stratify
        self.workspace_ids, self.model = workspace_ids, model
        self.excluded: list[dict] = []
        self._sizes: Counter[str] = Counter()
        self._top: dict[str, list[tuple[int, str, ModelCall]]] = {}

    def add(self, call: ModelCall) -> None:
        reason = exclusion(call, workspace_ids=self.workspace_ids, model=self.model)
        if reason:
            self.excluded.append({"call_id": call.call_id, "reason": reason})
            return
        key = stratum_of(call) if self.stratify else "all"
        self._sizes[key] += 1
        # 取反放进最小堆，堆顶就是当前保留项里排名最靠后的
        entry = (-int(_rank(self.seed, call.call_id), 16), call.call_id, call)
        heap = self._top.setdefault(key, [])
        if len(heap) < self.size:
            heapq.heappush(heap, entry)
        elif self.size:
            heapq.heappushpop(heap, entry)

    def result(self) -> SampleResult:
        sizes = dict(self._sizes)
        allocation = _allocate(sizes, self.size)
        chosen: list[tuple[str, ModelCall]] = []
        for key, heap in self._top.items():
            ordered = sorted(heap, reverse=True)
            chosen.extend((key, call) for _, _, call in ordered[: allocation[key]])
        chosen.sort(key=lambda item: _rank(self.seed, item[1].call_id))
        return SampleResult(
            selected=[call for _, call in chosen],
            excluded=self.excluded,
            strata=sizes,
            selected_strata=dict(Counter(key for key, _ in chosen)),
            shortfall=max(0, self.size - len(chosen)),
        )


def sample(
    calls: Iterable[ModelCall],
    *,
    size: int,
    seed: str,
    workspace_ids: list[str] | None = None,
    model: str | None = None,
    stratify: bool = True,
    prior_excluded: list[dict] | None = None,
) -> SampleResult:
    sampler = Sampler(size=size, seed=seed, workspace_ids=workspace_ids, model=model,
                      stratify=stratify)
    sampler.excluded.extend(prior_excluded or [])
    for call in calls:
        sampler.add(call)
    return sampler.result()


PROFILE_TOOL_PREFIX = "tool_update_visitor"


def infer_task_type(request: dict) -> str:
    """从请求形态推断任务类型。

    精确任务名需要 skill_id→名称映射（外部配置），这里只用请求里能直接看出来的信号：
    带【子任务调用输入】的是子任务；可用工具全是画像写入的是画像收集。
    """

    if not task_input(request):
        return "mainagent"
    tools = available_tools(request)
    if tools and all(name.startswith(PROFILE_TOOL_PREFIX) for name in tools):
        return "profile-update"
    if tools and all("knowledge" in name for name in tools):
        return "knowledge-lookup"
    return "subagent"


def to_case(
    call: ModelCall, *, baseline_id: str = "baseline", task_type: str | None = None
) -> EvalCase:
    """把一次线上调用变成评审输入；候选侧的输出在重放之后再补。"""

    resolved = task_type or infer_task_type(call.request)
    return EvalCase(
        case_id=call.call_id,
        workspace_id=call.workspace_id,
        task_name=task_name(call.request) or resolved,
        task_type=resolved,
        current_turn=current_turn(call.request),
        task_input=task_input(call.request),
        prior_turns=prior_turns(call.request),
        business_rules=business_rules(call.request),
        history_tool_calls=history_tool_calls(call.request),
        profile_snapshot=profile_snapshot(call.request),
        session_facts=session_facts(call.request),
        available_tools=available_tools(call.request),
        tool_labels=tool_labels(call.request),
        outputs=[to_output(call.response, baseline_id)],
    )
