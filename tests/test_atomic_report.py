from agent_eval.atomic_report import summary, wilson


def judgment(case_id, relative, *, agreement=True, task="customer_reply", worst=None):
    return {"case_id": case_id, "status": "succeeded", "result": {
        "verdict": {"relative": relative, "agreement": agreement},
        "task_type": task, "worst": worst or {"candidate": "none", "baseline": "none"},
        "rule_checks": {"candidate": [], "baseline": []},
        "reviews": [{"rejected_issues": 1}]}}


def execution(case_id, *, baseline_ms, candidate_ms=None):
    result = {"performance": {"baseline": {"elapsed_ms": baseline_ms, "input_tokens": 100,
                                           "output_tokens": 10},
                              "candidate": {"elapsed_ms": candidate_ms}}} if candidate_ms else {}
    return {"case_id": case_id, "status": "succeeded", "result": result,
            "input": {"item": {"elapsed_ms": baseline_ms, "prompt_tokens": 100,
                               "completion_tokens": 10}}}


def test_wilson_interval_is_wide_for_small_samples():
    assert wilson(0, 0) is None
    low, high = wilson(2, 20)
    assert 0.02 < low < 0.03 and 0.30 < high < 0.32


def test_summary_counts_verdicts_agreement_severity_and_human_calibration():
    judgments = [
        judgment("a", "G", worst={"candidate": "none", "baseline": "major"}),
        judgment("b", "S", agreement=None),
        judgment("c", "D", agreement=False, task="task_routing"),
        {"case_id": "d", "status": "uncertain", "result": None},
    ]
    executions = [execution("a", baseline_ms=1000, candidate_ms=2000),
                  execution("b", baseline_ms=40_000)]
    result = summary(executions, judgments, {"a": "G", "b": "B"})
    assert result["judged"] == 3
    assert result["relatives"] == {"G": 1, "S": 1, "B": 0, "D": 1, "U": 0}
    assert result["agreement"] == {"agreed": 1, "compared": 2, "identical": 1}
    assert result["serious"]["baseline"]["major"] == 1
    assert result["by_task"]["主 Agent"] == {"total": 3, "G": 1, "S": 1, "D": 1,
                                             "baseline_serious": 1}
    assert result["rejected_issues"] == 3
    assert result["performance"]["baseline"]["slow"] == 1
    assert result["performance"]["candidate"]["samples"] == 1
    assert result["human"] == {"labelled": 2, "agreed": 1}
