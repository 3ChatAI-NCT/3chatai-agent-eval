"""Present related executions as one evaluation without rewriting their evidence."""

# 先选定每个逻辑槽位的执行，再取同一次执行的评分；等待中的新执行也不能沿用旧评分。
# 排序只看显式关联和创建顺序，不根据结果是否通过选择。
GROUP_CTES = """
group_runs AS (
    SELECT p.id AS logical_id,e.id AS run_id,e.created_at,(e.id<>p.id) AS continuation
    FROM eval_experiments p JOIN eval_experiments e ON e.id=p.id OR (
        e.manifest#>>'{execution_continuation,parent_experiment_id}'=p.id
        AND e.space_id=p.space_id AND e.dataset_version_id=p.dataset_version_id
        AND e.mode=p.mode)
    WHERE p.id=ANY(%s::text[])
), selected_executions AS (
    SELECT DISTINCT ON (g.logical_id,j.case_id,j.repeat_index)
        g.logical_id,j.experiment_id,j.case_id,j.repeat_index
    FROM group_runs g JOIN eval_jobs j ON j.experiment_id=g.run_id
    WHERE j.stage='execute'
    ORDER BY g.logical_id,j.case_id,j.repeat_index,g.continuation DESC,
        g.created_at DESC,g.run_id DESC
), logical_jobs AS (
    SELECT s.logical_id,j.* FROM selected_executions s JOIN eval_jobs j
        ON j.experiment_id=s.experiment_id AND j.case_id=s.case_id
        AND j.repeat_index=s.repeat_index
)
"""

PROGRESS_SQL = f"""WITH {GROUP_CTES}
SELECT logical_id,
    count(*) FILTER (WHERE stage='execute') AS planned,
    count(*) FILTER (WHERE stage='execute' AND status='succeeded') AS executed,
    count(*) FILTER (WHERE stage='execute' AND status='running') AS executing,
    count(*) FILTER (WHERE stage='execute' AND status='queued') AS waiting,
    count(*) FILTER (WHERE stage='execute' AND status='failed') AS execution_failed,
    count(*) FILTER (WHERE stage='execute' AND status='uncertain') AS uncertain,
    count(*) FILTER (WHERE stage='execute' AND status='cancelled') AS excluded,
    count(*) FILTER (WHERE stage='judge' AND status='succeeded') AS judged,
    count(*) FILTER (WHERE stage='judge' AND status='running') AS judging,
    count(*) FILTER (WHERE stage='judge' AND status='queued') AS judge_waiting,
    count(*) FILTER (WHERE stage='judge' AND status IN ('failed','uncertain')) AS judge_failed,
    (SELECT count(*) FROM group_runs g WHERE g.logical_id=j.logical_id) AS execution_records
FROM logical_jobs j GROUP BY logical_id"""


def with_progress(run: dict, progress: dict | None) -> dict:
    if progress is None:
        return run
    counts = {key: value for key, value in progress.items() if key != "logical_id"}
    result = {**run, "progress": counts, "execution_failed": counts["execution_failed"]}
    if counts["executing"] or counts["judging"]:
        status = "running"
    elif counts["waiting"] or counts["judge_waiting"]:
        status = "queued"
    elif counts["uncertain"]:
        status = "uncertain"
    elif counts["execution_failed"] or counts["judge_failed"]:
        status = "failed"
    elif counts["excluded"] == counts["planned"]:
        status = "cancelled"
    else:
        status = "completed"
    # 单次执行仍沿用原有状态语义；组合评测的状态以全部有效槽位为准。
    if counts["execution_records"] > 1:
        result["status"] = status
        result["display_status"] = (
            "partial" if status in {"failed", "completed"} and counts["judged"]
            and (counts["execution_failed"] or counts["judge_failed"] or counts["excluded"])
            else status)
    return result
