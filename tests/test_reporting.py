from datetime import UTC, datetime, timedelta

import pytest

from agent_eval.models import Mode
from agent_eval.reporting import report


def test_assisted_review_cannot_replace_human_calibration():
    from agent_eval.reporting import current_reviews

    now = datetime.now(UTC)
    job = {"id": "judge-1", "result_sha256": "b" * 64}
    human = {"id": "review-1", "job_id": job["id"], "actor_type": "human",
             "subject": "fde", "outputs_sha256": "b" * 64, "created_at": now}
    assisted = {**human, "id": "review-2", "actor_type": "ai_assisted",
                "created_at": now + timedelta(seconds=1)}
    assert current_reviews([job], [human, assisted])[job["id"]] == human
    assert current_reviews([job], [assisted]) == {}


def test_human_review_summary_uses_latest_review_and_current_evidence():
    now = datetime.now(UTC)
    execute = {
        "id": "execute-1", "case_id": "case-1", "repeat_index": 0, "stage": "execute",
        "status": "succeeded", "error_code": None, "result_sha256": "a" * 64,
        "result": {}, "input": {"item": {}}, "runtime_engine_sha256": "engine",
    }
    judge = {
        **execute, "id": "judge-1", "stage": "judge", "result_sha256": "b" * 64,
        "result": {"verdict": {"relative": "G", "agreement": True},
                   "worst": {"candidate": "none", "baseline": "none"},
                   "rule_checks": {"candidate": [], "baseline": []}, "reviews": []},
    }
    old = {"id": "review-1", "job_id": judge["id"], "subject": "fde-a", "revision": 3,
           "label": "G", "issue_kind": "none", "note": "", "outputs_sha256": "b" * 64,
           "created_at": now - timedelta(minutes=2)}
    current = {**old, "id": "review-2", "subject": "fde-b", "revision": 1,
               "label": "B", "issue_kind": "judge_error", "note": "证据与结论不符",
               "created_at": now}
    stale = {**current, "id": "review-3", "outputs_sha256": "c" * 64,
             "created_at": now - timedelta(minutes=1)}
    run = {
        "mode": Mode.ATOMIC, "status": "completed", "review_status": "in_progress",
        "manifest": {"config": {"repeats": 1, "execution_target": "fixture"},
                     "judge_prompt_version": "v1", "judge_prompt_sha256": "p" * 64,
                     "judge_model": "judge", "engine_sha256": "engine"},
    }
    result = report(run, {"content_sha256": "d" * 64}, [execute, judge],
                    [current, stale, old])

    assert result["reviewed"] == 1
    assert result["human_review"] == {
        "labels": {"B": 1}, "issue_kinds": {"judge_error": 1},
        "compared": 1, "agreed": 0,
        "disagreements": [{"case_id": "case-1", "repeat_index": 0}],
    }
    assert result["review_items"][0]["note"] == "证据与结论不符"


@pytest.mark.parametrize(("execution_status", "has_judgment"), [
    ("failed", True), ("failed", False), ("uncertain", False),
])
def test_golden_turn_that_failed_to_execute_keeps_its_session_from_passing(
    execution_status, has_judgment,
):
    from test_jsonl_corpus import session_payload

    from agent_eval import engines

    session = session_payload()
    first = session["turns"][0]
    session["turns"].append({**first, "turn_id": f"{session['session_id']}#T2", "seq": 2,
                             "expected": {**first["expected"], "profile_update": {}}})
    config = {"repeats": 1, "execution_target": "platform", "dataset_version_id": "ds-1",
              "space": {"id": "space-1", "source_workspace_id": "src",
                        "target_workspace_id": "target"}}

    def observation(turn, status):
        return {"observation_id": f"obs-{turn['seq']}", "run_id": "exp-1",
                "session_id": session["session_id"], "turn_id": turn["turn_id"],
                "seq": turn["seq"], "repeat_index": 1, "status": status,
                "stage_reached": "profile_settled" if status == "completed" else "created",
                "account_id": "eval-1", "execution_target": "platform"}

    def job(job_id, stage, turn, status, *, result, job_input=None):
        return {"id": job_id, "experiment_id": "exp-1", "case_id": turn["turn_id"],
                "repeat_index": 1, "stage": stage, "status": status, "error_code": None,
                "result_sha256": job_id * 8, "result": result, "runtime_engine_sha256": "engine",
                "input": job_input or {"mode": Mode.GOLDEN, "config": config}}

    passed = [{"metric_id": metric_id, "status": "pass", "reason": "符合金标"}
              for metric_id in ("2", "3", "4", "5", "6", "8", "9")]
    jobs = [
        job("e1", "execute", first, "succeeded", result={}),
        job("j1", "judge", first, "succeeded",
            result={"turns": [{"turn_id": first["turn_id"], "repeat_index": 1, "metrics": passed}]},
            job_input={"mode": Mode.GOLDEN, "config": config,
                       "observation": observation(first, "completed")}),
        job("e2", "execute", session["turns"][1], execution_status,
            result={"observation": observation(session["turns"][1], "execution_error")}),
    ]
    if not has_judgment:
        jobs = [job("e1", "execute", first, execution_status,
                    result={"observation": observation(first, "execution_error")}), jobs[-1]]
    prompt = engines.golden_prompt().model_dump(mode="json")
    run = {"mode": Mode.GOLDEN, "status": "completed", "review_status": "pending",
           "manifest": {"config": config, "judge_prompt_version": "golden",
                        "judge_prompt_sha256": "p" * 64, "judge_prompt": prompt,
                        "judge_model": "judge", "engine_sha256": "engine"}}

    result = report(run, {"content_sha256": "d" * 64, "content": {"items": [session]}},
                    jobs, [])
    quality = result["quality"]

    attempt = quality["sessions"][0]["attempts"][0]
    expected_statuses = (["judged", "missing_judgement"] if has_judgment
                         else ["missing_judgement", "missing_judgement"])
    assert [turn["status"] for turn in attempt["turns"]] == expected_statuses
    assert attempt["session_status"] == "unavailable"
    assert quality["missing_turn_count"] == (1 if has_judgment else 2)
    assert result["judged"] == (1 if has_judgment else 0)
    assert result["uncertain"] == (2 if execution_status == "uncertain" else 0)
