"""盲评抽样、A/B 映射与揭晓后统计的纯函数。"""

import random
from datetime import UTC, datetime

from agent_eval import blind

CONFIG = {"random_percent": 10, "min_random": 5, "risk_percent": 5}


def jobs(relatives: list[str], worst: dict[int, str] | None = None) -> list[dict]:
    rows = []
    for index, relative in enumerate(relatives):
        case = f"case-{index}"
        rows.append({"id": f"exec-{index}", "stage": "execute", "case_id": case,
                     "repeat_index": 1, "status": "succeeded"})
        rows.append({"id": f"judge-{index}", "stage": "judge", "case_id": case,
                     "repeat_index": 1, "status": "succeeded", "error_code": None,
                     "result_sha256": f"{index:064x}",
                     "result": {"verdict": {"relative": relative},
                                "worst": {"candidate": (worst or {}).get(index, "none")}}})
    return rows


def test_sample_size_follows_percent_with_a_floor():
    assert len(blind.draw(jobs(["S"] * 200), CONFIG)) == 20
    assert len(blind.draw(jobs(["S"] * 30), CONFIG)) == 5  # 10% 只有 3 条，按下限取 5
    assert len(blind.draw(jobs(["S"] * 3), CONFIG)) == 3


def test_only_judged_cases_are_sampled_and_risk_cases_come_from_the_rest():
    rows = jobs(["S"] * 40 + ["D", "B", "G"], worst={41: "major"})
    rows[1]["status"] = "failed"  # case-0 评审失败，不能盲评
    sample = blind.draw(rows, CONFIG, random.Random(7))
    picked = [item["job_id"] for item in sample]
    assert "exec-0" not in picked and len(picked) == len(set(picked))
    chosen = {item["job_id"] for item in sample if item["kind"] == "random"}
    risky = {item["job_id"] for item in sample if item["kind"] == "risk"}
    # 42 条可评：随机 5 条；风险名额 ceil(5%)=3，但只有分歧与重要问题两条够格，且不与随机重复
    assert len(chosen) == 5
    assert risky == {"exec-40", "exec-41"} - chosen
    assert {item["a"] for item in sample} <= {"candidate", "baseline"}


def test_choice_and_label_round_trip_for_both_orders():
    for a in ("candidate", "baseline"):
        for choice in ("A", "B", "S", "U"):
            assert blind.to_choice(blind.to_label(choice, a), a) == choice
    assert blind.to_label("A", "candidate") == "G"
    assert blind.to_label("A", "baseline") == "B"


def test_kappa_discounts_chance_agreement():
    assert blind.kappa([("G", "G"), ("B", "B")]) == 1.0
    assert blind.kappa([("S", "S"), ("S", "S")]) is None  # 双方都只给一种结论，无法区分偶然
    assert blind.kappa([]) is None
    assert blind.kappa([("G", "G"), ("G", "B"), ("B", "G"), ("B", "B")]) == 0.0


def test_stats_use_the_locked_blind_labels_and_count_later_revisions():
    rows = jobs(["G", "B", "S"])
    sample = [{"job_id": "exec-0", "kind": "random", "a": "candidate"},
              {"job_id": "exec-1", "kind": "random", "a": "baseline"},
              {"job_id": "exec-2", "kind": "risk", "a": "candidate"}]
    at = datetime(2026, 9, 29, tzinfo=UTC)

    def review(target: int, label: str, revision: int) -> dict:
        return {"id": revision * 10 + target, "job_id": f"judge-{target}", "label": label,
                "subject": "fde", "revision": revision, "created_at": at,
                "outputs_sha256": f"{target:064x}", "issue_kind": "none", "note": ""}

    blind_reviews = [review(0, "G", 1), review(1, "G", 1)]
    locked = blind.snapshot(sample, rows, blind_reviews)
    assert set(locked) == {"exec-0", "exec-1"}
    row = {"sample": sample, "snapshot": locked, "skip_reason": "急用", "revealed_by": "fde",
           "revealed_at": at}
    revised = [*blind_reviews, review(1, "B", 2)]
    stats = blind.stats(row, rows, revised)
    assert stats["status"] == "skipped"
    assert stats["random"] | {"interval": None} == {
        "sampled": 2, "reviewed": 2, "revised": 1, "compared": 2, "agreed": 1,
        "interval": None, "kappa": 0.0,
        "disagreements": [{"case_id": "case-1", "repeat_index": 1, "ai": "B", "human": "G"}]}
    assert stats["risk"]["sampled"] == 1 and stats["risk"]["reviewed"] == 0
