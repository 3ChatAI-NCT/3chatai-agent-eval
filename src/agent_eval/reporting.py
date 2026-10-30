"""Aggregate persisted execution and judge facts without asking an LLM for totals."""

from collections import Counter
from urllib.parse import urlsplit

from golden_eval.application.judge_report import build_report
from golden_eval.domain.models import GoldenSession, JudgeTurnResult, PromptVersion, TurnObservation

from . import atomic_report, engines
from .models import Mode, digest


def latest(reviews: list[dict]) -> dict:
    """每条结果只取最新一版人工复核。"""

    found = {}
    for review in sorted(reviews, key=lambda item: (item["created_at"], item["id"])):
        key = review["job_id"]
        found[key] = review
    return found


def current_reviews(jobs: list[dict], reviews: list[dict], *, actor_type: str = "human") -> dict:
    by_id = {job["id"]: job for job in jobs}
    eligible = [review for review in reviews if review.get("actor_type", "human") == actor_type]
    return {job_id: review for job_id, review in latest(eligible).items()
            if job_id in by_id and review["outputs_sha256"] == (
                by_id[job_id]["result_sha256"] or digest({
                    "status": by_id[job_id]["status"],
                    "error_code": by_id[job_id]["error_code"],
                })
            )}


def human_labels(jobs: list[dict], reviews: list[dict]) -> dict[str, str]:
    scored = {(job["case_id"], job["repeat_index"]): job for job in jobs
              if job["stage"] == "judge" and job["status"] == "succeeded"}
    active = current_reviews(jobs, reviews)
    return {job["case_id"]: active[target["id"]]["label"]
            for job in jobs if job["stage"] == "execute"
            for target in [scored.get((job["case_id"], job["repeat_index"]), job)]
            if target["id"] in active}


def report(run: dict, dataset: dict, jobs: list[dict], reviews: list[dict],
           *, include_cases: bool = False) -> dict:
    config = run["manifest"]["config"]
    executions = [job for job in jobs if job["stage"] == "execute"]
    judgments = [job for job in jobs if job["stage"] == "judge"]
    scored = {(job["case_id"], job["repeat_index"]): job for job in judgments}
    by_case = Counter()
    for job in executions:
        by_case[job["case_id"]] += 1
    active_targets = {scored.get((job["case_id"], job["repeat_index"]), job)["id"]
                      for job in executions}
    latest_reviews = {job_id: review for job_id, review in current_reviews(jobs, reviews).items()
                      if job_id in active_targets}
    assisted = {job_id: review for job_id, review in current_reviews(
        jobs, reviews, actor_type="ai_assisted").items() if job_id in active_targets}
    by_id = {job["id"]: job for job in jobs}
    reviewed = 0
    labels = Counter()
    issue_kinds = Counter()
    compared = agreed = 0
    disagreements = []
    for job in executions:
        evidence = scored.get((job["case_id"], job["repeat_index"]), job)
        review = latest_reviews.get(evidence["id"])
        if review and review["outputs_sha256"] == (
            evidence["result_sha256"] or digest({
                "status": evidence["status"], "error_code": evidence["error_code"]
            })
        ):
            reviewed += 1
            labels[review["label"]] += 1
            issue_kinds[review["issue_kind"]] += 1
            if run["mode"] == Mode.ATOMIC and evidence["stage"] == "judge":
                ai_label = (evidence.get("result") or {}).get("verdict", {}).get("relative")
                if ai_label in ("G", "S", "B") and review["label"] in ("G", "S", "B"):
                    compared += 1
                    if ai_label == review["label"]:
                        agreed += 1
                    else:
                        disagreements.append({"case_id": job["case_id"],
                                              "repeat_index": job["repeat_index"]})
    payload = {
        "mode": run["mode"],
        "status": run["status"],
        "review_status": run["review_status"],
        "scope": "selected_turns" if run["manifest"]["config"].get("sample_scope")
                 else "published_dataset",
        "planned": len(executions),
        "executed": sum(job["status"] == "succeeded" for job in executions),
        "execution_failed": sum(job["status"] == "failed" for job in executions),
        "uncertain": sum(job["status"] == "uncertain" for job in executions),
        "judged": sum(job["status"] == "succeeded" for job in judgments),
        "judge_failed": sum(job["status"] in ("failed", "uncertain") for job in judgments),
        "reviewed": reviewed,
        "review_required": len(executions),
        "human_review": {"labels": dict(labels), "issue_kinds": dict(issue_kinds),
                         "compared": compared, "agreed": agreed,
                         "disagreements": disagreements},
        "assisted_review": {"count": len(assisted),
                            "labels": dict(Counter(r["label"] for r in assisted.values())),
                            "issue_kinds": dict(Counter(
                                r["issue_kind"] for r in assisted.values()))},
        "case_count": len(by_case),
        "repeats": run["manifest"]["config"]["repeats"],
        "fixture": run["manifest"]["config"]["execution_target"] == "fixture",
        "dataset_sha256": dataset["content_sha256"],
        "judge_prompt_version": run["manifest"]["judge_prompt_version"],
        "judge_prompt_sha256": run["manifest"]["judge_prompt_sha256"],
        "judge_model": run["manifest"]["judge_model"],
        "candidate": ({"model": config["candidate_model"],
                       "host": ("3Chat 网关" if config.get("candidate_source") == "gateway"
                                else urlsplit(config["candidate_endpoint"]).hostname),
                       "preset": (config.get("candidate_preset") or {}).get("name")}
                      if config.get("candidate_endpoint") else None),
        "source_experiment_id": run.get("source_experiment_id"),
        "progress": run.get("progress"),
        "review_items": sorted(({
            "case_id": by_id[item["job_id"]]["case_id"],
            "repeat_index": by_id[item["job_id"]]["repeat_index"],
            "stage": by_id[item["job_id"]]["stage"],
            "label": item["label"], "issue_kind": item["issue_kind"],
            "note": item["note"], "subject": item["subject"],
            "actor_type": item.get("actor_type", "human"),
            "revision": item["revision"], "created_at": item["created_at"].isoformat(),
        } for item in [*latest_reviews.values(), *assisted.values()]),
        key=lambda item: (item["case_id"], item["repeat_index"])),
    }
    versions = {stage: sorted({job["runtime_engine_sha256"] for job in jobs
                               if job["stage"] == stage and job["runtime_engine_sha256"]})
                for stage in ("execute", "judge")}
    payload["runtime_engine_versions"] = versions
    payload["execution_warnings"] = []
    if include_cases:
        payload["cases"] = [{
            "case_id": job["case_id"], "repeat_index": job["repeat_index"],
            "execution_status": job["status"], "execution_error_code": job["error_code"],
            "execution_result_sha256": job["result_sha256"],
            "execution_source_job_id": job.get("source_job_id"),
            "execution": job["result"],
            "judge_status": scored.get((job["case_id"], job["repeat_index"]), {}).get("status"),
            "judge_result": scored.get((job["case_id"], job["repeat_index"]), {}).get("result"),
        } for job in executions]
    if any(len(versions[stage]) > 1 for stage in versions):
        payload["execution_warnings"].append("同一阶段由不同引擎版本完成，请分组核对")
    if not run.get("source_experiment_id") and any(
        version != run["manifest"]["engine_sha256"]
        for versions_in_stage in versions.values() for version in versions_in_stage
    ):
        payload["execution_warnings"].append("执行期间引擎代码发生变化，请核对逐条版本")
    if run["mode"] == Mode.ATOMIC:
        payload["atomic"] = atomic_report.summary(executions, judgments,
                                                  human_labels(jobs, reviews))
    else:
        # 执行失败的 Turn 不进 Judge，但要以“未判定”计入会话，不能让会话因此显得通过
        observations = [TurnObservation.model_validate(job["input"]["observation"])
                        for job in judgments] + [
            TurnObservation.model_validate(job["result"]["observation"])
            for job in executions if job["status"] != "succeeded"
            and (job["result"] or {}).get("observation")]
        judged_results = [JudgeTurnResult.model_validate(item)
                          for job in judgments if job["status"] == "succeeded"
                          and "turns" in job["result"]
                          for item in job["result"]["turns"]]
        if observations and not payload["fixture"]:
            sessions = {item["session_id"]: GoldenSession.model_validate(item)
                        for item in dataset["content"]["items"]}
            first = judgments[0] if judgments else executions[0]
            prompt = PromptVersion.model_validate(run["manifest"]["judge_prompt"])
            logical_run = {**first, "experiment_id": run.get("id", first.get("experiment_id")),
                           "input": {**first["input"], "config": config}}
            aggregate = build_report(
                run=engines.golden_run(logical_run), sessions=sessions,
                observations=observations, results=judged_results, prompt=prompt,
            )
            payload["quality"] = aggregate.model_dump(mode="json")
        payload["human_labels"] = dict(Counter(
            review["label"] for review in latest_reviews.values()
            if review["label"] in ("pass", "fail", "unavailable")
        ))
    return payload


def _space(space: dict) -> dict:
    return {"id": space["id"], "name": space["name"], "source_name": space.get("source_name", "")}


def cross_summary(loaded, hidden: list[tuple[dict, dict]] = ()) -> dict:
    """多条原子回放评测的逐条汇总与合计。

    loaded 逐个产出 (run, jobs, reviews, space)，处理完一条就只留下轻量字段，
    避免多条评测的完整请求同时驻留内存。候选或评审方式不一致时合计仍给出，但附带提示。
    hidden 是盲评尚未揭晓的 (run, space)：只列出名称，不给结论也不计入合计。
    """

    rows, executions, judgments, human = [], [], [], {}
    for run, jobs, reviews, space in loaded:
        config = run["manifest"]["config"]
        own_executions = [atomic_report.compact_execution(job) for job in jobs
                          if job["stage"] == "execute"]
        # 评审任务的 input 带着完整请求与候选响应，汇总只需要结论
        own_judgments = [{key: job[key] for key in ("case_id", "status", "result")}
                         for job in jobs if job["stage"] == "judge"]
        labels = human_labels(jobs, reviews)
        rows.append({
            "id": run["id"], "name": run["name"], "status": run["status"],
            "space": _space(space),
            "candidate": config.get("candidate_model"),
            "judge_prompt_version": run["manifest"]["judge_prompt_version"],
            "planned": len(own_executions),
            "executed": sum(job["status"] == "succeeded" for job in own_executions),
            "atomic": atomic_report.summary(own_executions, own_judgments, labels),
        })
        executions += own_executions
        judgments += own_judgments
        human |= labels
    warnings = []
    if hidden:
        warnings.append(f"{len(hidden)} 条评测盲评尚未揭晓，未计入合计")
    if len({row["candidate"] for row in rows}) > 1:
        warnings.append("所选评测的候选模型不同，合计只作参考")
    if len({row["judge_prompt_version"] for row in rows}) > 1:
        warnings.append("所选评测的评审提示词版本不同，合计只作参考")
    rows += [{"id": run["id"], "name": run["name"], "status": run["status"],
              "space": _space(space), "blind": True} for run, space in hidden]
    return {"runs": rows, "total": atomic_report.summary(executions, judgments, human),
            "warnings": warnings}
