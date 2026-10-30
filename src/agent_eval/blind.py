"""人工盲评：评测结束后按比例抽样，揭晓前所有人看不到 AI 结论与模型身份。

随机样本用来估计 AI 与人工的一致率；风险样本（AI 判为分歧、指出重要问题或候选更差的案例）
只用来核对 AI 的判断，单独统计。两类混在一起、按随机顺序呈现，评审人看不出每条为什么被抽中；
每条样本的 A/B 顺序另行随机，与 AI 评审时的顺序无关。
"""

import math
import random
from collections import Counter

from .atomic_report import wilson
from .reporting import current_reviews

DECIDED = ("G", "S", "B")
# 揭晓前的状态；此时报告、逐条结果与原始证据都不能暴露 AI 结论与双方身份
PENDING = ("blind_waiting", "blind_open")


def judged(jobs: list[dict]) -> dict[tuple, dict]:
    return {(job["case_id"], job["repeat_index"]): job for job in jobs
            if job["stage"] == "judge" and job["status"] == "succeeded"}


def risk(result: dict) -> int | None:
    """风险优先级，越小越优先；不属于风险样本时返回 None。"""

    relative = (result.get("verdict") or {}).get("relative")
    worst = result.get("worst") or {}
    if relative == "D":
        return 0
    if any(worst.get(side) in ("major", "critical") for side in ("candidate", "baseline")):
        return 1
    if relative == "U":
        return 2
    if relative == "B":
        return 3
    return None


def draw(jobs: list[dict], config: dict, rng: random.Random | None = None) -> list[dict]:
    """只从双方都有完整输出、AI 盲评成功的样本里抽；随机样本的抽取不看 AI 结论。"""

    rng = rng or random.SystemRandom()
    scored = judged(jobs)
    pool = [job for job in jobs if job["stage"] == "execute"
            and (job["case_id"], job["repeat_index"]) in scored]
    total = len(pool)
    size = min(total, max(config["min_random"], math.ceil(total * config["random_percent"] / 100)))
    chosen = rng.sample(pool, size)
    taken = {job["id"] for job in chosen}
    risky = sorted(((rank, rng.random(), job) for job in pool if job["id"] not in taken
                    for rank in [risk(scored[(job["case_id"], job["repeat_index"])]["result"])]
                    if rank is not None), key=lambda entry: entry[:2])
    extra = [job for _, _, job in risky[:math.ceil(total * config["risk_percent"] / 100)]]
    items = ([{"job_id": job["id"], "kind": "random"} for job in chosen]
             + [{"job_id": job["id"], "kind": "risk"} for job in extra])
    rng.shuffle(items)
    for item in items:
        item["a"] = rng.choice(("candidate", "baseline"))
    return items


def to_label(choice: str, a: str) -> str:
    """评审人选的 A/B 换成相对候选的结论：G 候选更优、B 基线更优。"""

    if choice in ("S", "U"):
        return choice
    side = a if choice == "A" else ("baseline" if a == "candidate" else "candidate")
    return "G" if side == "candidate" else "B"


def to_choice(label: str, a: str) -> str:
    if label in ("G", "B"):
        side = "candidate" if label == "G" else "baseline"
        return "A" if side == a else "B"
    return "U" if label == "unavailable" else label


def targets(sample: list[dict], jobs: list[dict]) -> dict[str, dict]:
    """样本的执行任务 ID → 复核目标（成功的评审任务）；补跑中的样本暂时没有目标。"""

    by_id = {job["id"]: job for job in jobs}
    scored = judged(jobs)
    found = {}
    for item in sample:
        execute = by_id.get(item["job_id"])
        target = execute and scored.get((execute["case_id"], execute["repeat_index"]))
        if target:
            found[item["job_id"]] = target
    return found


def snapshot(sample: list[dict], jobs: list[dict], reviews: list[dict]) -> dict[str, dict]:
    """揭晓时锁定每条样本当时的人工结论，之后的修订不改变盲评统计。"""

    active = current_reviews(jobs, reviews)
    return {job_id: {key: active[target["id"]][key] for key in ("label", "subject", "revision")}
            for job_id, target in targets(sample, jobs).items() if target["id"] in active}


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa：扣除两方各自结论分布下的偶然一致。"""

    total = len(pairs)
    if not total:
        return None
    observed = sum(ai == human for ai, human in pairs) / total
    ai, human = Counter(ai for ai, _ in pairs), Counter(human for _, human in pairs)
    expected = sum(ai[key] * human[key] for key in ai) / (total * total)
    return None if expected == 1 else round((observed - expected) / (1 - expected), 3)


def stats(row: dict, jobs: list[dict], reviews: list[dict]) -> dict:
    """揭晓后的统计：随机样本与风险样本分开，一致率只算双方都给出明确结论的样本。"""

    by_id = {job["id"]: job for job in jobs}
    found = targets(row["sample"], jobs)
    active = current_reviews(jobs, reviews)
    locked = row["snapshot"] or {}
    groups = {kind: {"sampled": 0, "reviewed": 0, "pairs": [], "revised": 0, "disagreements": []}
              for kind in ("random", "risk")}
    for item in row["sample"]:
        group = groups[item["kind"]]
        group["sampled"] += 1
        blind = locked.get(item["job_id"])
        target = found.get(item["job_id"])
        if not blind or not target:
            continue
        group["reviewed"] += 1
        latest = active.get(target["id"])
        if latest and latest["label"] != blind["label"]:
            group["revised"] += 1
        ai = (target["result"].get("verdict") or {}).get("relative")
        if ai in DECIDED and blind["label"] in DECIDED:
            group["pairs"].append((ai, blind["label"]))
            if ai != blind["label"]:
                execute = by_id[item["job_id"]]
                group["disagreements"].append({
                    "case_id": execute["case_id"], "repeat_index": execute["repeat_index"],
                    "ai": ai, "human": blind["label"]})
    for group in groups.values():
        pairs = group.pop("pairs")
        agreed = sum(ai == human for ai, human in pairs)
        group |= {"compared": len(pairs), "agreed": agreed,
                  "interval": wilson(agreed, len(pairs)), "kappa": kappa(pairs)}
    return {"status": "skipped" if row["skip_reason"] else "revealed",
            "skip_reason": row["skip_reason"], "revealed_by": row["revealed_by"],
            "revealed_at": row["revealed_at"].isoformat(), **groups}
