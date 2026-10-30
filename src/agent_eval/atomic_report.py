"""原子回放报告：结论分布与置信区间、评审一致率、问题严重度、任务分组与性能，全部由代码聚合。"""

import math
from collections import Counter, defaultdict

from atomic_eval.responses import task_name

from . import response_format

RELATIVES = ("G", "S", "B", "D", "U")
SIDES = ("candidate", "baseline")
SLOW_MS = 30_000
MAIN_AGENT = "主 Agent"


def skill(item: dict) -> str:
    """技能名取自请求中的子任务卡片；没有卡片的是主 Agent 对客调用。"""

    return task_name(item.get("request") or {}) or MAIN_AGENT


def compact_execution(job: dict) -> dict:
    """汇总只用到的字段：跨评测汇总时不必把每条完整请求留在内存里。"""

    item = job["input"]["item"]
    return {"case_id": job["case_id"], "status": job["status"], "skill": skill(item),
            "format_checks": response_format.execution_checks(job),
            "result": {"performance": (job["result"] or {}).get("performance")},
            "input": {"item": {key: item.get(key)
                               for key in ("elapsed_ms", "prompt_tokens", "completion_tokens")}}}


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    """二项比例的 Wilson 95% 区间；小样本下比正态近似可靠。"""

    if not total:
        return None
    p = successes / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)])


def _performance(rows: list[dict]) -> dict:
    elapsed = [row["elapsed_ms"] for row in rows if row.get("elapsed_ms") is not None]
    tokens = {key: [row[key] for row in rows if row.get(key) is not None]
              for key in ("input_tokens", "output_tokens")}
    return {"samples": len(elapsed), "p50_ms": _percentile(elapsed, 0.5),
            "p95_ms": _percentile(elapsed, 0.95),
            "slow": sum(value > SLOW_MS for value in elapsed),
            **{f"avg_{key}": round(sum(values) / len(values)) if values else None
               for key, values in tokens.items()}}


def summary(executions: list[dict], judgments: list[dict], human: dict[str, str]) -> dict:
    """human 为 case_id → 人工结论（G/S/B/U），用于计算人工与 AI 的一致率。"""

    judged = {job["case_id"]: job["result"] for job in judgments
              if job["status"] == "succeeded" and job["result"].get("verdict")}
    skills = {job["case_id"]: job.get("skill") or skill(job["input"]["item"])
              for job in executions}
    relatives = Counter(result["verdict"]["relative"] for result in judged.values())
    total = len(judged)
    agreement = [result["verdict"].get("agreement") for result in judged.values()]
    decided = [value for value in agreement if value is not None]
    by_task: dict[str, Counter] = defaultdict(Counter)
    for case_id, result in judged.items():
        task = by_task[skills.get(case_id, MAIN_AGENT)]
        task["total"] += 1
        task[result["verdict"]["relative"]] += 1
        for side in SIDES:
            if result.get("worst", {}).get(side) in ("major", "critical"):
                task[f"{side}_serious"] += 1
    perf = {side: [] for side in SIDES}
    for job in executions:
        if job["status"] != "succeeded":
            continue
        item = job["input"]["item"]
        recorded = (job["result"] or {}).get("performance") or {}
        perf["baseline"].append(recorded.get("baseline") or {
            "elapsed_ms": item.get("elapsed_ms"), "input_tokens": item.get("prompt_tokens"),
            "output_tokens": item.get("completion_tokens")})
        perf["candidate"].append(recorded.get("candidate") or {})
    labelled = [(human[case_id], result["verdict"]["relative"])
                for case_id, result in judged.items() if human.get(case_id) in ("G", "S", "B")
                and result["verdict"]["relative"] in ("G", "S", "B")]
    return {
        "response_format": response_format.summarize(executions),
        "judged": total,
        "relatives": {key: relatives[key] for key in RELATIVES},
        "intervals": {key: wilson(relatives[key], total) for key in RELATIVES},
        "agreement": {"agreed": sum(decided), "compared": len(decided),
                      "identical": agreement.count(None)},
        "serious": {side: {level: sum(result.get("worst", {}).get(side) == level
                                      for result in judged.values())
                           for level in ("critical", "major", "minor")} for side in SIDES},
        "rule_checks": {side: sum(bool(result.get("rule_checks", {}).get(side))
                                  for result in judged.values()) for side in SIDES},
        "rejected_issues": sum(review.get("rejected_issues", 0) for result in judged.values()
                               for review in result.get("reviews", [])),
        "by_task": {task: dict(counts) for task, counts in sorted(by_task.items())},
        "performance": {side: _performance(rows) for side, rows in perf.items()},
        "human": {"labelled": len(labelled),
                  "agreed": sum(label == relative for label, relative in labelled)},
    }
