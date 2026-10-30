"""Unified API, PostgreSQL facts, and worker contracts against an isolated test database."""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_jsonl_corpus import session_payload, shared_payload

from agent_eval import capture, engines, vault
from agent_eval.api import create_app
from agent_eval.config import Settings
from agent_eval.objects import LocalObjects
from agent_eval.retention import sweep
from agent_eval.store import Conflict, Store
from agent_eval.worker import (
    run_annotation,
    run_batch_starts,
    run_capture,
    run_one,
    run_report,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def platform(tmp_path):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    settings = Settings(database_url=SecretStr(dsn), environment="development",
                        dev_identity=True, object_root=str(tmp_path),
                        mapping_from_model="agent-model",
                        model_gateway_url="https://gateway.example.test/v1")
    store = Store(dsn)
    store.open()
    space_id = f"test-{uuid.uuid4().hex}"
    store.upsert_space({"id": space_id, "name": "Fixture space",
                        "source_workspace_id": "source-ws",
                        "target_workspace_id": "target-ws"}, "local:developer")
    with TestClient(create_app(settings, store)) as client:
        yield client, store, settings, space_id
    with store.pool.connection() as connection:
        connection.execute(
            """DELETE FROM eval_blind_reviews WHERE experiment_id IN (
                 SELECT id FROM eval_experiments WHERE space_id=%s)""", (space_id,)
        )
        connection.execute(
            """DELETE FROM eval_reviews WHERE job_id IN (
                 SELECT j.id FROM eval_jobs j JOIN eval_experiments e
                   ON e.id=j.experiment_id WHERE e.space_id=%s)""", (space_id,)
        )
        connection.execute(
            """DELETE FROM eval_events WHERE experiment_id IN (
                 SELECT id FROM eval_experiments WHERE space_id=%s)""", (space_id,)
        )
        connection.execute(
            """DELETE FROM eval_turn_traces WHERE experiment_id IN (
                 SELECT id FROM eval_experiments WHERE space_id=%s)""", (space_id,)
        )
        connection.execute(
            """UPDATE eval_jobs SET source_job_id=NULL WHERE experiment_id IN (
                 SELECT id FROM eval_experiments WHERE space_id=%s)""", (space_id,)
        )
        connection.execute(
            """DELETE FROM eval_jobs WHERE experiment_id IN (
                 SELECT id FROM eval_experiments WHERE space_id=%s)""", (space_id,)
        )
        connection.execute(
            """DELETE FROM eval_report_exports WHERE experiment_id IN (
                 SELECT id FROM eval_experiments WHERE space_id=%s)""", (space_id,)
        )
        connection.execute(
            """DELETE FROM eval_annotation_jobs WHERE draft_id IN (
                 SELECT id FROM eval_dataset_drafts WHERE space_id=%s)""", (space_id,)
        )
        for table in ("eval_experiments", "eval_dataset_versions", "eval_dataset_drafts",
                      "eval_capture_jobs", "eval_source_snapshots", "eval_audit",
                      "eval_space_model_changes", "eval_spaces"):
            connection.execute(f"DELETE FROM {table} WHERE space_id=%s" if table != "eval_spaces"
                               else "DELETE FROM eval_spaces WHERE id=%s", (space_id,))
    store.close()


def _dataset(client, space_id, items=None):
    data = {"name": "Golden fixture", "mode": "golden_regression",
            "items": items or [session_payload()], "shared": shared_payload()}
    draft = client.post(f"/api/agent-eval/v1/spaces/{space_id}/drafts", json=data).json()
    assert client.post(f"/api/agent-eval/v1/drafts/{draft['id']}/publish",
                       headers={"If-Match": str(draft["revision"])}).status_code == 409
    reviewed = draft
    for item in data["items"]:
        reviewed = client.post(
            f"/api/agent-eval/v1/drafts/{draft['id']}/cases/{item['session_id']}/review",
            headers={"If-Match": str(reviewed["revision"])}).json()
    response = client.post(f"/api/agent-eval/v1/drafts/{draft['id']}/publish",
                           headers={"If-Match": str(reviewed["revision"])})
    assert response.status_code == 201, response.text
    return response.json()


def _experiment(client, space_id, dataset_id):
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Fixture execution", "dataset_version_id": dataset_id,
                               "execution_target": "fixture", "repeats": 1,
                           })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _continued_execution(platform):
    from copy import deepcopy

    client, store, settings, space_id = platform
    parent_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    while run_one(store, settings, "group-fixture"):
        pass
    parent = store.experiment(parent_id)
    original = next(j for j in store.jobs(parent_id) if j["stage"] == "execute")
    manifest = deepcopy(parent["manifest"])
    config = manifest["config"]
    config["name"] = "Continued execution"
    config["sample_scope"] = [original["case_id"]]
    payload = {**original["input"], "config": config}
    child_id = store.submit(store.space(space_id), config, uuid.uuid4().hex,
                            "local:developer", manifest,
                            [(original["case_id"], original["repeat_index"], payload)])
    return parent_id, child_id, original


def test_continued_execution_is_one_live_report_without_reusing_the_old_judge(platform):
    client, store, settings, _ = platform
    parent_id, child_id, original = _continued_execution(platform)
    original_rows = store.query("SELECT id,status,result_sha256 FROM eval_jobs "
                                "WHERE experiment_id=%s ORDER BY id", (parent_id,))
    original_planned = store.job_counts(parent_id)["planned"]
    store.link_execution(parent_id, child_id, "local:developer")
    store.link_execution(parent_id, child_id, "local:developer")  # 关联可安全重试
    listed = client.get("/api/agent-eval/v1/experiments").json()
    assert {row["id"] for row in listed} == {parent_id}
    view = client.get(f"/api/agent-eval/v1/experiments/{parent_id}").json()
    assert view["status"] == "queued"  # 原执行已结束，后续执行还没开跑
    assert view["progress"]["execution_records"] == 2
    assert len(view["execution_records"]) == 2
    selected = [j for j in store.report_jobs(parent_id) if j["case_id"] == original["case_id"]]
    assert len(selected) == 1 and selected[0]["experiment_id"] == child_id
    assert selected[0]["stage"] == "execute" and selected[0]["status"] == "queued"
    assert store.set_archived([parent_id], True, "local:developer") == 0
    assert run_one(store, settings, "group-execute")
    assert run_one(store, settings, "group-judge")
    selected = [j for j in store.report_jobs(parent_id) if j["case_id"] == original["case_id"]]
    assert {j["stage"] for j in selected} == {"execute", "judge"}
    assert all(j["experiment_id"] == child_id for j in selected)
    child_execution = next(j for j in selected if j["stage"] == "execute")
    evidence, judge = store.evidence_jobs(parent_id, child_execution["id"])
    assert evidence["id"] == child_execution["id"] and judge["input"]["observation"]
    report = client.get(f"/api/agent-eval/v1/experiments/{parent_id}/report").json()
    assert report["planned"] == store.job_counts(parent_id)["planned"]
    assert report["planned"] == original_planned
    assert store.query("SELECT id,status,result_sha256 FROM eval_jobs "
                       "WHERE experiment_id=%s ORDER BY id", (parent_id,)) == original_rows
    # 新执行失败也必须覆盖原成功，不能按结果挑选；同时不能借用原评分。
    with store.pool.connection() as c:
        c.execute("UPDATE eval_jobs SET status='uncertain',error_code='platform_http_504' "
                  "WHERE id=%s", (child_execution["id"],))
        c.execute("DELETE FROM eval_jobs WHERE experiment_id=%s AND stage='judge'", (child_id,))
    counts = store.job_counts(parent_id)
    assert counts["uncertain"] == 1
    assert not any(j["stage"] == "judge" and j["case_id"] == original["case_id"]
                   for j in store.report_jobs(parent_id))


@pytest.mark.parametrize("change", ["input", "target", "policy", "observation"])
def test_continued_execution_requires_the_same_frozen_inputs_and_configuration(platform, change):
    from psycopg.types.json import Jsonb

    _, store, _, _ = platform
    parent_id, child_id, _ = _continued_execution(platform)
    with store.pool.connection() as c:
        if change == "input":
            c.execute("UPDATE eval_jobs SET input=jsonb_set(input,'{turn,profile_before}',%s) "
                      "WHERE experiment_id=%s", (Jsonb({"unexpected": "different"}), child_id))
        elif change == "observation":
            c.execute("UPDATE eval_experiments SET manifest=jsonb_set(manifest,%s,%s) "
                      "WHERE id=%s", (["config", "profile_readback"],
                                      Jsonb({"stable_seconds": 12}), child_id))
        else:
            path = "target_config_sha256" if change == "target" else "judge_prompt_sha256"
            c.execute("UPDATE eval_experiments SET manifest=jsonb_set(manifest,%s,%s) "
                      "WHERE id=%s", ([path], Jsonb("different"), child_id))
    with pytest.raises(Conflict):
        store.link_execution(parent_id, child_id, "local:developer")


def test_cancelling_a_group_stops_pending_executions_in_all_members(platform):
    _, store, _, _ = platform
    parent_id, child_id, _ = _continued_execution(platform)
    store.link_execution(parent_id, child_id, "local:developer")
    store.cancel(parent_id, "local:developer")
    assert store.experiment(child_id)["status"] == "cancelled"
    assert all(j["status"] == "cancelled" for j in store.jobs(child_id))


def test_group_retries_only_the_selected_execution_judge(platform):
    _, store, settings, _ = platform
    parent_id, child_id, _ = _continued_execution(platform)
    store.link_execution(parent_id, child_id, "local:developer")
    assert run_one(store, settings, "continued-execute")
    judge = store.claim("continued-judge", 120)
    assert judge["stage"] == "judge" and judge["experiment_id"] == child_id
    store.finish(judge, error_code="model_timeout")
    assert store.experiment(parent_id)["status"] == "failed"
    outcome = store.retry_failed(parent_id, "retry-selected-child", "local:developer")
    assert outcome["requeued"] == 1
    assert store.experiment(parent_id)["status"] == "queued"
    assert run_one(store, settings, "retry-continued-judge")
    assert store.experiment(parent_id)["status"] == "completed"
    assert all(j["attempt"] == 1 for j in store.query(
        "SELECT attempt FROM eval_jobs WHERE experiment_id=%s AND stage='execute'", (child_id,)))


def test_candidate_key_is_encrypted_hidden_and_cleared_when_the_run_ends(platform):
    from cryptography.fernet import Fernet
    from test_candidate_model import CALL

    _client, store, settings, space_id = platform
    configured = settings.model_copy(update={
        "judge_model": "judge-test", "credential_key": SecretStr(Fernet.generate_key().decode())})
    dataset = store.publish_atomic(space_id, {"name": "Atomic sample", "mode": "atomic_replay",
                                              "items": [CALL], "shared": {}}, "local:developer")
    with TestClient(create_app(configured, store)) as client:
        response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                               headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                                   "name": "Candidate run", "dataset_version_id": dataset["id"],
                                   "candidate": {"base_url": "https://candidate.example.test/v1",
                                                 "model": "candidate-model",
                                                 "api_key": "candidate-secret"}})
        assert response.status_code == 201, response.text
        run_id = response.json()["id"]
        shown = client.get(f"/api/agent-eval/v1/experiments/{run_id}").text
        assert "candidate-secret" not in shown and "candidate_api_key" not in shown
    run = store.experiment(run_id)
    assert run["manifest"]["config"]["candidate_endpoint"] == "https://candidate.example.test/v1"
    assert "candidate-secret" not in json.dumps(run["manifest"])
    assert vault.unseal(configured, run["candidate_api_key"]) == "candidate-secret"
    assert all("candidate-secret" not in json.dumps(job["input"]) for job in store.jobs(run_id))
    store.cancel(run_id, "local:developer")
    assert store.experiment(run_id)["candidate_api_key"] is None


def _atomic_dataset(store, space_id):
    from test_candidate_model import CALL

    return store.publish_atomic(space_id, {"name": "Atomic sample", "mode": "atomic_replay",
                                           "items": [CALL], "shared": {}}, "local:developer")["id"]


def _atomic_fixture_run(client, store, space_id):
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Atomic fixture",
                               "dataset_version_id": _atomic_dataset(store, space_id),
                               "execution_target": "fixture"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_judge_timeouts_are_retried_before_the_result_is_marked_uncertain(platform, monkeypatch):
    client, store, settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    assert run_one(store, settings, "test")

    def timeout(*_args):
        raise httpx.ReadTimeout("gateway slow")

    monkeypatch.setattr(engines, "grade", timeout)
    for attempt in (1, 2):
        assert run_one(store, settings, "test")
        judge = next(job for job in store.jobs(run_id) if job["stage"] == "judge")
        assert (judge["status"], judge["attempt"]) == ("queued", attempt)
    assert run_one(store, settings, "test")
    judge = next(job for job in store.jobs(run_id) if job["stage"] == "judge")
    assert (judge["status"], judge["error_code"]) == ("uncertain", "external_delivery_unknown")


def test_rejected_candidate_call_keeps_the_http_status_as_the_reason(platform, monkeypatch):
    client, store, settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)

    def too_large(*_args):
        request = httpx.Request("POST", "https://candidate.example.test/v1/responses")
        raise httpx.HTTPStatusError("too large", request=request,
                                    response=httpx.Response(413, request=request))

    monkeypatch.setattr(engines, "execute", too_large)
    assert run_one(store, settings, "test")
    execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
    assert (execute["status"], execute["error_code"]) == ("failed", "http_413")


def test_retry_failed_atomic_case_keeps_the_same_report_and_rejudges_only_that_case(
    platform, monkeypatch,
):
    from test_candidate_model import CALL

    client, store, settings, space_id = platform
    other = {**CALL, "call_id": "req-2:attempt:1", "request_id": "req-2"}
    dataset = store.publish_atomic(space_id, {"name": "Atomic sample", "mode": "atomic_replay",
                                              "items": [CALL, other], "shared": {}},
                                   "local:developer")
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Atomic fixture", "dataset_version_id": dataset["id"],
                               "execution_target": "fixture"})
    run_id = response.json()["id"]
    real_execute = engines.execute
    failed_attempts = 0

    def reject_first(job, *args):
        nonlocal failed_attempts
        if job["case_id"] == CALL["call_id"] and failed_attempts < 2:
            failed_attempts += 1
            request = httpx.Request("POST", "https://candidate.example.test/v1/responses")
            raise httpx.HTTPStatusError("too large", request=request,
                                        response=httpx.Response(413, request=request))
        return real_execute(job, *args)

    monkeypatch.setattr(engines, "execute", reject_first)
    while run_one(store, settings, "test"):
        pass
    assert store.experiment(run_id)["status"] == "failed"
    detail = client.get(f"/api/agent-eval/v1/experiments/{run_id}").json()
    assert detail["display_status"] == "partial"
    assert detail["execution_failed"] == 1
    listed = client.get("/api/agent-eval/v1/experiments").json()
    assert next(item for item in listed if item["id"] == run_id)["display_status"] == "partial"
    partial_report = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()
    assert partial_report["planned"] == 2
    assert partial_report["judged"] == 1
    assert partial_report["atomic"]["judged"] == 0  # fixture 评审只验证链路，不产生结论
    assert store.experiment(run_id)["report_revision"] == 1
    objects = LocalObjects(settings.object_root)
    assert run_report(store, settings, objects, "test")
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report/artifact").json()[
        "report_revision"] == 1
    first = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").json()
    failed = next(job for job in first if job["stage"] == "execute" and
                  job["case_id"] == CALL["call_id"])
    reviewed = client.post(
        f"/api/agent-eval/v1/experiments/{run_id}/results/{failed['id']}/review",
        json={"label": "unavailable", "outputs_sha256": failed["result_sha256"]})
    assert reviewed.status_code == 200, reviewed.text

    path = f"/api/agent-eval/v1/experiments/{run_id}/retry-failed"
    key = uuid.uuid4().hex
    retried = client.post(path, headers={"Idempotency-Key": key}, json={})
    assert retried.status_code == 200 and retried.json()["requeued"] == 1
    assert client.post(path, headers={"Idempotency-Key": key}, json={}).status_code == 200
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()["artifact"] is None
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report/artifact").status_code == 409
    queued = store.jobs(run_id)
    assert [(job["case_id"], job["attempt"]) for job in queued
            if job["stage"] == "execute" and job["status"] == "queued"] == [
                (CALL["call_id"], 1)]
    while run_one(store, settings, "test"):
        pass
    assert store.experiment(run_id)["status"] == "failed"
    assert store.experiment(run_id)["report_revision"] == 2
    assert run_report(store, settings, objects, "test")
    second = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").json()
    failed_again = next(job for job in second if job["id"] == failed["id"])
    assert failed_again["attempt"] == 2
    assert failed_again["result_sha256"] != failed["result_sha256"]
    assert failed_again["review"] is None
    assert failed_again["review_revision"] == 1
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()["reviewed"] == 0

    retried = client.post(path, headers={"Idempotency-Key": uuid.uuid4().hex}, json={})
    assert retried.status_code == 200
    while run_one(store, settings, "test"):
        pass
    final = store.experiment(run_id)
    assert final["status"] == "completed" and final["report_revision"] == 3
    assert final["display_status"] == "completed"
    jobs = store.jobs(run_id)
    assert len([job for job in jobs if job["stage"] == "execute"]) == 2
    assert len([job for job in jobs if job["stage"] == "judge"]) == 2
    assert next(job for job in jobs if job["case_id"] == CALL["call_id"]
                and job["stage"] == "execute")["attempt"] == 3
    assert next(job for job in jobs if job["case_id"] == other["call_id"]
                and job["stage"] == "execute")["attempt"] == 1
    report = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()
    assert report["executed"] == report["judged"] == 2
    assert report["execution_failed"] == report["reviewed"] == 0
    assert run_report(store, settings, objects, "test")
    artifact = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report/artifact").json()
    assert artifact["report_revision"] == 3
    assert artifact["status"] == "completed"


def test_retrying_a_failed_judge_does_not_keep_the_old_unavailable_review(platform, monkeypatch):
    client, store, settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    assert run_one(store, settings, "test")
    original_grade = engines.grade

    def broken_judge(*_args):
        raise RuntimeError("judge failed")

    monkeypatch.setattr(engines, "grade", broken_judge)
    assert run_one(store, settings, "test")
    results = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").json()
    execution = next(job for job in results if job["stage"] == "execute")
    reviewed = client.post(
        f"/api/agent-eval/v1/experiments/{run_id}/results/{execution['id']}/review",
        json={"label": "unavailable", "outputs_sha256": execution["result_sha256"]})
    assert reviewed.status_code == 200
    retried = client.post(f"/api/agent-eval/v1/experiments/{run_id}/retry-failed",
                          headers={"Idempotency-Key": uuid.uuid4().hex}, json={})
    assert retried.status_code == 200 and retried.json()["requeued"] == 1
    monkeypatch.setattr(engines, "grade", original_grade)
    assert run_one(store, settings, "test")
    report = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()
    assert report["judged"] == 1 and report["reviewed"] == 0
    assert report["atomic"]["human"]["labelled"] == 0


def test_regrade_carries_a_definite_execution_failure_over(platform, monkeypatch):
    from test_candidate_model import CALL

    client, store, settings, space_id = platform
    other = {**CALL, "call_id": "req-2:attempt:1", "request_id": "req-2"}
    dataset = store.publish_atomic(space_id, {"name": "Atomic sample", "mode": "atomic_replay",
                                              "items": [CALL, other], "shared": {}},
                                   "local:developer")
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Atomic fixture", "dataset_version_id": dataset["id"],
                               "execution_target": "fixture"})
    source = response.json()["id"]
    real_execute = engines.execute

    def reject_first(job, *args):
        if job["case_id"] == CALL["call_id"]:
            request = httpx.Request("POST", "https://candidate.example.test/v1/responses")
            raise httpx.HTTPStatusError("too large", request=request,
                                        response=httpx.Response(413, request=request))
        return real_execute(job, *args)

    monkeypatch.setattr(engines, "execute", reject_first)
    while run_one(store, settings, "test"):
        pass
    assert store.experiment(source)["status"] == "failed"
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Regrade", "dataset_version_id": dataset["id"],
                               "execution_target": "fixture", "reuse_experiment_id": source})
    assert response.status_code == 201, response.text
    jobs = store.jobs(response.json()["id"])
    failed = [job for job in jobs if job["status"] == "failed"]
    assert [(job["case_id"], job["error_code"]) for job in failed] == [
        (CALL["call_id"], "http_413")]
    assert [job["case_id"] for job in jobs if job["stage"] == "judge"] == [other["call_id"]]


@pytest.mark.parametrize(("code", "definite"), [
    ("http_413", True), ("http_401", True), ("request_not_replayable", True),
    ("http_429", False), ("http_503", False), ("http_408", False), ("execution_failed", False),
    (None, False),
])
def test_only_definite_failures_can_be_carried_into_a_regrade(code, definite):
    from agent_eval.store import definite_failure

    assert definite_failure(code) is definite


def test_expired_jobs_of_a_cancelled_run_are_closed_not_requeued(platform):
    client, store, _settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    assert store.claim("test", 0)["stage"] == "execute"
    store.cancel(run_id, "local:developer")
    store.recover_expired()
    execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
    assert execute["status"] == "cancelled"


def test_interrupted_atomic_execution_is_requeued_up_to_the_attempt_limit(platform):
    client, store, _settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    for attempt in (1, 2):
        # 租约立即过期，模拟发布或崩溃时 Worker 在调用候选模型途中被杀
        assert store.claim("test", 0)["stage"] == "execute"
        store.recover_expired()
        execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
        assert (execute["status"], execute["attempt"]) == ("queued", attempt)
    store.claim("test", 0)
    store.recover_expired()
    execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
    assert (execute["status"], execute["error_code"]) == ("uncertain", "worker_lease_expired")


def test_atomic_regrade_reuses_the_candidate_output_without_executing_again(platform):
    client, store, settings, space_id = platform
    source = _atomic_fixture_run(client, store, space_id)
    assert run_one(store, settings, "test") and run_one(store, settings, "test")
    assert store.experiment(source)["status"] == "completed"
    dataset_id = store.experiment(source)["dataset_version_id"]
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Regrade", "dataset_version_id": dataset_id,
                               "execution_target": "fixture", "reuse_experiment_id": source})
    assert response.status_code == 201, response.text
    jobs = store.jobs(response.json()["id"])
    execute = next(job for job in jobs if job["stage"] == "execute")
    judge = next(job for job in jobs if job["stage"] == "judge")
    assert execute["status"] == "succeeded" and execute["source_job_id"]
    assert judge["status"] == "queued" and "response" in judge["input"]


def test_judge_settings_are_versioned_and_frozen_into_new_runs(platform):
    client, store, _settings, space_id = platform
    base = "/api/agent-eval/v1/settings/judge"
    try:
        current = client.get(base).json()
        config = {"model": "judge-a", "timeout_seconds": 120, "format_attempts": 2}
        saved = client.put(base, json={"config": config, "revision": current["revision"]})
        assert saved.status_code == 200, saved.text
        stale = client.put(base, json={"config": config, "revision": current["revision"]})
        assert stale.status_code == 409
        shown = client.get(base).json()
        assert shown["config"] == config and shown["history"][0]["config"] == config
        assert {prompt["mode"] for prompt in shown["prompts"]} == {"atomic_replay",
                                                                   "golden_regression"}
        assert client.get("/api/agent-eval/v1/capabilities").json()["judge_model"] == "judge-a"
        run_id = _atomic_fixture_run(client, store, space_id)
        manifest = store.experiment(run_id)["manifest"]
        assert manifest["judge"] == config and manifest["judge_model"] == "judge-a"
    finally:
        with store.pool.connection() as connection:
            connection.execute("DELETE FROM eval_setting_revisions WHERE key='judge'")


def test_candidate_presets_keep_keys_sealed_and_feed_new_runs(platform, monkeypatch):
    from cryptography.fernet import Fernet
    from test_candidate_model import CALL

    from agent_eval import gateway

    _client, store, settings, space_id = platform
    configured = settings.model_copy(update={
        "judge_model": "judge-test", "credential_key": SecretStr(Fernet.generate_key().decode())})
    base = "/api/agent-eval/v1/settings/candidates"
    runs = f"/api/agent-eval/v1/spaces/{space_id}/experiments"
    preset = {"name": f"北坡-{uuid.uuid4().hex[:6]}",
              "base_url": "https://candidate.example.test/v1/",
              "model": "candidate-model", "note": "评测专用 key"}
    dataset = store.publish_atomic(space_id, {"name": "Atomic sample", "mode": "atomic_replay",
                                              "items": [CALL], "shared": {}}, "local:developer")

    def start(client, **candidate):
        return client.post(runs, headers={"Idempotency-Key": uuid.uuid4().hex},
                           json={"name": "Run", "dataset_version_id": dataset["id"], **candidate})

    try:
        with TestClient(create_app(configured, store)) as client:
            assert client.put("/api/agent-eval/v1/settings/report", json={
                "config": {"blind_review": False}, "revision": 0}).status_code == 200
            assert client.post(base, json=preset).status_code == 422  # 新建必须带 key
            created = client.post(base, json={**preset, "api_key": "sk-preset-secret-1234"})
            assert created.status_code == 201, created.text
            preset_id = created.json()["id"]
            assert client.post(base, json={**preset, "api_key": "sk-other"}).status_code == 409
            listed = client.get(base).json()
            shown = next(item for item in listed if item["id"] == preset_id)
            assert "secret" not in json.dumps(listed) and shown["key_hint"] == "sk-…1234"
            assert shown["base_url"] == "https://candidate.example.test/v1"
            # 不填 key 更新：沿用原 key；旧修订号更新被拒
            updated = client.put(f"{base}/{preset_id}",
                                 json={**preset, "note": "改备注", "revision": 1})
            assert updated.json()["revision"] == 2
            assert client.put(f"{base}/{preset_id}",
                              json={**preset, "revision": 1}).status_code == 409
            sealed = store.candidate_preset(preset_id)["sealed_key"]
            assert vault.unseal(configured, sealed) == "sk-preset-secret-1234"

            run = start(client, candidate_preset_id=preset_id)
            assert run.status_code == 201, run.text
            row = store.experiment(run.json()["id"])
            assert row["candidate_api_key"] == sealed
            assert row["manifest"]["config"]["candidate_preset"]["id"] == preset_id

            monkeypatch.setattr(gateway, "chat_models",
                                lambda _settings: [{"id": "gpt-5.6-sol", "provider": "OPENAI"}])
            assert start(client, candidate_gateway_model="no-such-model").status_code == 422
            run = start(client, candidate_gateway_model="gpt-5.6-sol")
            assert run.status_code == 201, run.text
            row = store.experiment(run.json()["id"])
            assert row["candidate_api_key"] is None
            assert row["manifest"]["config"]["candidate_source"] == "gateway"
            report = client.get(f"/api/agent-eval/v1/experiments/{row['id']}/report").json()
            assert report["candidate"]["host"] == "3Chat 网关"

            assert client.delete(f"{base}/{preset_id}").status_code == 200
            assert all(item["id"] != preset_id for item in client.get(base).json())
    finally:
        with store.pool.connection() as connection:
            connection.execute("DELETE FROM eval_candidate_presets WHERE name=%s",
                               (preset["name"],))
            connection.execute("DELETE FROM eval_setting_revisions WHERE key='report'")


def test_archive_restore_and_delete_follow_their_guards(platform):
    client, store, settings, space_id = platform
    source = _atomic_fixture_run(client, store, space_id)
    while run_one(store, settings, "test"):
        pass
    regrade = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                          headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                              "name": "Regrade", "execution_target": "fixture", "trial": True,
                              "dataset_version_id": store.experiment(source)["dataset_version_id"],
                              "reuse_experiment_id": source}).json()["id"]
    while run_one(store, settings, "test"):
        pass
    listed = {row["id"]: row for row in client.get("/api/agent-eval/v1/experiments").json()}
    assert listed[regrade]["trial"] is True and listed[regrade]["deletable"] is False
    assert listed[source]["deletable"] is False  # 是重新判分的来源
    assert client.delete(f"/api/agent-eval/v1/experiments/{regrade}").status_code == 409
    assert client.delete(f"/api/agent-eval/v1/experiments/{source}").status_code == 409

    archived = client.post("/api/agent-eval/v1/experiments/archive",
                           json={"ids": [source, regrade], "archived": True}).json()
    assert archived == {"changed": 2}
    ids = [row["id"] for row in client.get("/api/agent-eval/v1/experiments").json()]
    assert source not in ids and regrade not in ids
    everything = client.get("/api/agent-eval/v1/experiments?include_archived=true").json()
    assert {source, regrade} <= {row["id"] for row in everything}
    assert next(row for row in everything if row["id"] == regrade)["deletable"] is True
    client.post("/api/agent-eval/v1/experiments/archive", json={"ids": [source], "archived": False})
    assert source in [row["id"] for row in client.get("/api/agent-eval/v1/experiments").json()]

    assert client.delete(f"/api/agent-eval/v1/experiments/{regrade}").status_code == 200
    assert not store.query("SELECT 1 FROM eval_jobs WHERE experiment_id=%s", (regrade,))
    assert client.get("/api/agent-eval/v1/experiments/" + source).json()["deletable"] is False
    client.post("/api/agent-eval/v1/experiments/archive", json={"ids": [source], "archived": True})
    assert client.get("/api/agent-eval/v1/experiments/" + source).json()["deletable"] is True


def test_finished_trials_are_archived_after_seven_days(platform):
    client, store, settings, space_id = platform
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Trial", "execution_target": "fixture", "trial": True,
                               "dataset_version_id": _atomic_dataset(store, space_id)})
    run_id = response.json()["id"]
    while run_one(store, settings, "test"):
        pass
    assert store.archive_expired_trials() == 0  # 还没满 7 天
    with store.pool.connection() as connection:
        connection.execute("UPDATE eval_experiments SET created_at=now()-interval '8 days' "
                           "WHERE id=%s", (run_id,))
    assert store.archive_expired_trials() == 1
    assert store.experiment(run_id)["archived_at"] is not None


def test_replay_batch_keeps_frozen_basis_and_starts_fresh_tasks(platform, monkeypatch):
    from copy import deepcopy

    from cryptography.fernet import Fernet
    from test_candidate_model import CALL

    from agent_eval import gateway

    _client, store, settings, space_id = platform
    configured = settings.model_copy(update={
        "judge_model": "original-judge",
        "credential_key": SecretStr(Fernet.generate_key().decode())})
    monkeypatch.setattr(gateway, "chat_models",
                        lambda _settings: [{"id": "old-model", "provider": "test"}])
    second_call = {**deepcopy(CALL), "call_id": "another-call"}
    monkeypatch.setattr(capture, "collect", lambda job, *_args: (
        {"calls": [CALL, second_call]}, {"count": 2}, "source-sha",
        {"name": job["name"], "mode": "atomic_replay",
         "items": [CALL, second_call], "shared": {}}))
    base = "/api/agent-eval/v1/batches"
    preset = None
    try:
        with TestClient(create_app(configured, store)) as client:
            source_id = client.post(base, headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                "name": "Original batch", "space_ids": [space_id], "size": 2, "trial": True,
                "start": "2026-09-22T00:00:00+08:00", "end": "2026-09-22T01:00:00+08:00",
                "candidate_gateway_model": "old-model"}).json()["id"]
            assert run_capture(store, configured, LocalObjects(configured.object_root), "test")
            assert run_batch_starts(store, configured)
            endpoint = f"{base}/{source_id}/replay"
            assert client.get(endpoint).status_code == 409  # 整批结束前不能复用
            source_item = store.batch(source_id)[1][0]
            source = store.experiment(source_item["experiment_id"])
            store.cancel(source["id"], "local:developer")
            preview = client.get(endpoint).json()
            assert preview["items"][0]["samples"] == 2  # 包含旧评测中没有成功执行的样本
            assert preview["items"][0]["judge_model"] == "original-judge"
            preset = client.post("/api/agent-eval/v1/settings/candidates", json={
                "name": f"Replay-{uuid.uuid4().hex[:6]}",
                "base_url": "https://candidate.example.test/v1",
                "model": "qwen3.8-max-0902", "api_key": "sk-replay-secret"}).json()["id"]
            # 全局设置与内置提示词变了，也必须沿用旧评测冻结的评分依据。
            configured.judge_model = "new-global-judge"
            monkeypatch.setattr(engines.pairwise, "prompt", lambda: {
                "version": "new-version", "sha256": "new-sha", "text": "new prompt"})
            payload = {"name": "Qwen replay", "candidate_preset_id": preset}
            headers = {"Idempotency-Key": uuid.uuid4().hex}
            original_counts = store.one("SELECT count(*) AS n FROM eval_capture_jobs")["n"]
            response = client.post(endpoint, headers=headers, json=payload)
            assert response.status_code == 201, response.text
            replay_id = response.json()["id"]
            assert client.post(endpoint, headers=headers, json=payload).json()["id"] == replay_id
            assert client.post(endpoint, headers=headers,
                               json={**payload, "name": "Other"}).status_code == 409
            batch, items = store.batch(replay_id)
            assert batch["spec"]["source_batch_id"] == source_id
            assert "candidate_gateway_model" not in batch["spec"]
            assert items[0]["capture_id"] == source_item["capture_id"]
            replay = store.experiment(items[0]["experiment_id"])
            old, new = source["manifest"], replay["manifest"]
            for field in ("dataset_sha256", "judge", "judge_model", "atomic_prompt",
                          "judge_prompt_version", "judge_prompt_sha256", "blind_review"):
                assert new.get(field) == old.get(field)
            assert replay["dataset_version_id"] == source["dataset_version_id"]
            assert new["config"]["trial"] is True
            assert new["config"]["candidate_model"] == "qwen3.8-max-0902"
            assert vault.unseal(configured, replay["candidate_api_key"]) == "sk-replay-secret"
            jobs = store.jobs(replay["id"])
            assert len(jobs) == 2 and all(job["stage"] == "execute" for job in jobs)
            assert all(job["status"] == "queued" and job["result"] is None for job in jobs)
            assert store.one("SELECT count(*) AS n FROM eval_capture_jobs")["n"] == original_counts
            assert "sk-replay-secret" not in json.dumps(client.get(f"{base}/{replay_id}").json())
            # 提交中途失败必须回滚批次、评测和任务，避免创建了半批却已开始计费。
            submit = store.submit

            def fail_after_submit(*args, **kwargs):
                submit(*args, **kwargs)
                raise Conflict("test rollback")

            monkeypatch.setattr(store, "submit", fail_after_submit)
            failed_key = uuid.uuid4().hex
            assert client.post(endpoint, headers={"Idempotency-Key": failed_key},
                               json=payload).status_code == 409
            assert not store.query("SELECT id FROM eval_batches WHERE idempotency_key=%s",
                                   (failed_key,))
            monkeypatch.setattr(store, "submit", submit)
            with store.pool.connection() as c:
                c.execute("UPDATE eval_dataset_versions SET content_purged_at=now() WHERE id=%s",
                          (source["dataset_version_id"],))
            assert client.get(endpoint).status_code == 410
            assert client.post(endpoint, headers={"Idempotency-Key": uuid.uuid4().hex},
                               json=payload).status_code == 410
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM eval_batch_items WHERE space_id=%s", (space_id,))
            c.execute("DELETE FROM eval_batches WHERE created_by='local:developer'")
            if preset:
                c.execute("DELETE FROM eval_candidate_presets WHERE id=%s", (preset,))


def test_batch_captures_then_starts_each_space_once(platform, monkeypatch):
    from cryptography.fernet import Fernet
    from test_candidate_model import CALL

    from agent_eval import gateway

    _client, store, settings, space_id = platform
    configured = settings.model_copy(update={
        "judge_model": "judge-test", "credential_key": SecretStr(Fernet.generate_key().decode())})
    monkeypatch.setattr(gateway, "chat_models",
                        lambda _settings: [{"id": "gpt-5.6-sol", "provider": "OPENAI"}])
    monkeypatch.setattr(capture, "collect", lambda job, *_args: (
        {"calls": [CALL]}, {"count": 1}, "source-sha",
        {"name": job["name"], "mode": "atomic_replay", "items": [CALL], "shared": {}}))
    window = {"start": "2026-09-22T00:00:00+08:00", "end": "2026-09-22T01:00:00+08:00"}
    base = "/api/agent-eval/v1/batches"
    presets = []
    try:
        with TestClient(create_app(configured, store)) as client:
            def create(**candidate):
                return client.post(base, headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                    "name": "放量", "space_ids": [space_id], "size": 1, "trial": True,
                    **window, **candidate})

            assert create(candidate_gateway_model="no-such-model").status_code == 422
            batch_id = create(candidate_gateway_model="gpt-5.6-sol").json()["id"]
            assert run_capture(store, configured, LocalObjects(configured.object_root), "test")
            assert run_batch_starts(store, configured)
            assert not run_batch_starts(store, configured)  # 已开跑的条目不再处理
            detail = client.get(f"{base}/{batch_id}").json()
            item = detail["items"][0]
            assert item["capture_status"] == "succeeded" and item["experiment_id"]
            run = store.experiment(item["experiment_id"])
            assert run["manifest"]["config"]["candidate_source"] == "gateway"
            assert run["manifest"]["config"]["trial"] is True
            assert run["name"] == "放量 · Fixture space"
            started = batch_id

            # 开跑前预设被删除：条目记下原因，而不是让 Worker 反复重试
            preset = client.post("/api/agent-eval/v1/settings/candidates", json={
                "name": f"预设-{uuid.uuid4().hex[:6]}", "base_url": "https://candidate.example.test/v1",
                "model": "candidate-model", "api_key": "sk-preset-secret-1234"}).json()["id"]
            presets.append(preset)
            batch_id = create(candidate_preset_id=preset).json()["id"]
            client.delete(f"/api/agent-eval/v1/settings/candidates/{preset}")
            assert run_capture(store, configured, LocalObjects(configured.object_root), "test")
            run_batch_starts(store, configured)
            item = client.get(f"{base}/{batch_id}").json()["items"][0]
            assert item["experiment_id"] is None and "不存在" in item["error_message"]
            assert item["blind_status"] is None  # 没开跑的条目不算盲评
            listed = next(row for row in client.get(base).json() if row["id"] == batch_id)
            assert (listed["failed"], listed["blind"]) == (1, 0)

            # 归档与删除：还有空间在运行的批次不处理；归档连同评测一起，删除前必须先归档
            archive = f"{base}/archive"
            assert client.post(archive, json={"ids": [started], "archived": True}).json() == {
                "changed": 0}
            store.cancel(run["id"], "local:developer")
            assert client.post(archive, json={"ids": [started, batch_id],
                                              "archived": True}).json() == {"changed": 2}
            assert store.experiment(run["id"])["archived_at"] is not None
            assert started not in [row["id"] for row in client.get(base).json()]
            assert started in [row["id"] for row in
                               client.get(base, params={"include_archived": True}).json()]
            assert client.post(archive, json={"ids": [started], "archived": False}).json() == {
                "changed": 1}
            assert store.experiment(run["id"])["archived_at"] is None
            assert client.delete(f"{base}/{started}").status_code == 409
            client.post(archive, json={"ids": [started], "archived": True})
            assert client.delete(f"{base}/{started}").status_code == 200
            assert client.get(f"/api/agent-eval/v1/experiments/{run['id']}").status_code == 404
            assert client.delete(f"{base}/{batch_id}").status_code == 200
            assert client.get(f"{base}/{batch_id}").status_code == 404
    finally:
        with store.pool.connection() as connection:
            connection.execute("DELETE FROM eval_batch_items WHERE space_id=%s", (space_id,))
            connection.execute("DELETE FROM eval_batches WHERE created_by='local:developer'")
            connection.execute("DELETE FROM eval_candidate_presets WHERE id=ANY(%s)", (presets,))


def test_cross_summary_pools_atomic_runs_and_rejects_golden(platform):
    client, store, settings, space_id = platform
    first = _atomic_fixture_run(client, store, space_id)
    second = _atomic_fixture_run(client, store, space_id)
    while run_one(store, settings, "test"):
        pass
    body = client.get(f"/api/agent-eval/v1/summary?ids={first},{second}").json()
    assert [row["id"] for row in body["runs"]] == [first, second]
    assert body["runs"][0]["space"]["name"] == "Fixture space"
    assert body["runs"][0]["planned"] == 1 and "relatives" in body["total"]
    golden = _experiment(client, space_id, _dataset(client, space_id)["id"])
    assert client.get(f"/api/agent-eval/v1/summary?ids={golden}").status_code == 422


def test_blind_review_settings_are_versioned_and_ignore_the_retired_toggle(platform):
    client, store, _settings, _space_id = platform
    base = "/api/agent-eval/v1/settings/report"
    defaults = {"blind_review": True, "random_percent": 10, "min_random": 5, "risk_percent": 5}
    try:
        with store.pool.connection() as connection:
            connection.execute(
                """INSERT INTO eval_setting_revisions(key,revision,value,subject)
                   VALUES ('report',1,'{"blind_review_default": true}','local:developer')""")
        current = client.get(base).json()
        assert current["config"] == defaults and current["revision"] == 1
        assert client.get("/api/agent-eval/v1/capabilities").json()["blind_review"] == defaults
        changed = {**defaults, "random_percent": 20}
        assert client.put(base, json={"config": changed, "revision": 1}).status_code == 200
        assert client.put(base, json={"config": defaults, "revision": 1}).status_code == 409
        assert client.put(base, json={"config": {**defaults, "random_percent": 0},
                                      "revision": 2}).status_code == 422
        assert client.get(base).json()["config"] == changed
    finally:
        with store.pool.connection() as connection:
            connection.execute("DELETE FROM eval_setting_revisions WHERE key='report'")


IMAGE = "https://test-media-bucket.oss-cn-beijing.aliyuncs.com/demo/receipt.jpg"


def test_blind_review_hides_ai_and_identity_until_the_sample_is_revealed(platform, monkeypatch):
    from test_candidate_model import CALL

    from agent_eval import gateway

    _client, store, settings, space_id = platform
    configured = settings.model_copy(update={"judge_model": "judge-test"})
    # 客户消息带图片：盲评页保留原图链接和业务正文，仍隐藏盲评模型身份
    request = {**CALL["request"],
               "input": [{"role": "user", "content": f"周六有课吗\n[客户发送了图片] {IMAGE}"}]}
    calls = [{**CALL, "request": request, "call_id": f"req-{index}:attempt:1",
              "request_id": f"req-{index}"} for index in range(12)]
    dataset = store.publish_atomic(space_id, {"name": "Atomic sample", "mode": "atomic_replay",
                                              "items": calls, "shared": {}}, "local:developer")
    monkeypatch.setattr(gateway, "chat_models",
                        lambda _settings: [{"id": "gpt-5.6-sol", "provider": "OPENAI"}])
    real_execute = engines.execute
    monkeypatch.setattr(engines, "execute", lambda job, *args: real_execute({**job, "input": {
        **job["input"], "config": {**job["input"]["config"], "execution_target": "fixture"}}},
        *args))
    # 前 6 条 AI 判候选更优，其余判基线更优
    monkeypatch.setattr(engines, "grade", lambda job, *_args: {
        "verdict": {"relative": "G" if int(job["case_id"][4:].split(":")[0]) < 6 else "B",
                    "agreement": True, "reason": "测试"},
        "worst": {"candidate": "none", "baseline": "none"}, "reviews": []})
    base = "/api/agent-eval/v1/experiments"
    with TestClient(create_app(configured, store)) as client:
        created = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                              headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                                  "name": "Blind run", "dataset_version_id": dataset["id"],
                                  "candidate_gateway_model": "gpt-5.6-sol"})
        assert created.status_code == 201, created.text
        run_id = created.json()["id"]
        assert store.experiment(run_id)["manifest"]["blind_review"] == {
            "random_percent": 10, "min_random": 5, "risk_percent": 5}
        assert client.get(f"{base}/{run_id}/blind-review").json() == {"status": "blind_waiting"}
        while run_one(store, configured, "test"):
            pass

        detail = client.get(f"{base}/{run_id}").json()
        assert detail["blind_status"] == "blind_open"
        assert "gpt-5.6-sol" not in json.dumps(detail) and "judge-test" not in json.dumps(detail)
        with monkeypatch.context() as patch:
            patch.setattr(store, "jobs", lambda _id: pytest.fail("盲评进度读取了完整任务"))
            patch.setattr(store, "dataset", lambda _id: pytest.fail("盲评进度读取了完整数据集"))
            report = client.get(f"{base}/{run_id}/report").json()
        assert report["judged"] == 12 and "atomic" not in report and "candidate" not in report
        assert report["blind"] == {"status": "blind_open"}
        assert set(report) == {"mode", "status", "review_status", "planned", "executed",
                               "execution_failed", "uncertain", "judged", "judge_failed",
                               "case_count", "fixture", "blind", "artifact"}
        assert client.get(f"{base}/{run_id}/results").status_code == 403
        assert client.get(f"{base}/{run_id}/report/artifact").status_code == 403
        execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
        assert set(client.get(f"{base}/{run_id}/results/{execute['id']}/evidence")
                   .json()) == {"request"}
        summary = client.get(f"/api/agent-eval/v1/summary?ids={run_id}").json()
        assert summary["runs"] == [{"id": run_id, "name": "Blind run", "status": "completed",
                                    "space": summary["runs"][0]["space"], "blind": True}]
        assert summary["total"]["judged"] == 0 and "盲评尚未揭晓" in summary["warnings"][0]
        assert client.get("/api/agent-eval/v1/workbench").json()["pending_reports"][0][
            "id"] == run_id

        with monkeypatch.context() as patch:
            patch.setattr(store, "jobs", lambda _id: pytest.fail("盲评抽样读取了完整任务"))
            sample = client.get(f"{base}/{run_id}/blind-review").json()
        items = sample["items"]
        # 12 条：随机 max(5, ceil(10%)) = 5 条，风险名额 ceil(5%) = 1 条给基线更优的一条
        assert sample["status"] == "blind_open" and len(items) == 6 and sample["reviewed"] == 0
        assert all(item["case"]["current_images"] == [IMAGE] for item in items)
        shown = json.dumps(items)
        for leaked in ("side_id", "upstream_model", "verdict", "baseline", "candidate",
                       "elapsed_ms", "gpt-5.6-sol"):
            assert leaked not in shown
        with monkeypatch.context() as patch:
            patch.setattr(store, "jobs", lambda _id: pytest.fail("盲评重开读取了完整任务"))
            assert client.get(f"{base}/{run_id}/blind-review").json()["items"] == items
        assert client.post(f"{base}/{run_id}/blind-review/reveal", json={}).status_code == 409

        row = store.blind_review(run_id)
        for item in items:
            placed = next(entry for entry in row["sample"] if entry["job_id"] == item["job_id"])
            # 人工每条都选候选那一侧
            choice = "A" if placed["a"] == "candidate" else "B"
            with monkeypatch.context() as patch:
                patch.setattr(store, "jobs", lambda _id: pytest.fail("单条复核读取了完整任务"))
                saved = client.post(f"{base}/{run_id}/blind-review/items/{item['job_id']}", json={
                    "choice": choice, "outputs_sha256": item["result_sha256"]})
            assert saved.status_code == 200, saved.text
        again = client.get(f"{base}/{run_id}/blind-review").json()
        assert again["reviewed"] == 6
        assert all(item["review"]["choice"] in ("A", "B") for item in again["items"])
        target = next(job for job in store.jobs(run_id) if job["stage"] == "judge")
        assert client.post(f"{base}/{run_id}/results/{target['id']}/review", json={
            "label": "G", "outputs_sha256": target["result_sha256"]}).status_code == 403

        assert client.post(f"{base}/{run_id}/blind-review/reveal", json={}).status_code == 200
        assert client.get(f"{base}/{run_id}/blind-review").json() == {"status": "revealed"}
        assert client.get(f"{base}/{run_id}/results").status_code == 200
        with monkeypatch.context() as patch:
            patch.setattr(store, "dataset", lambda _id: pytest.fail("原子报告读取了完整数据集"))
            revealed = client.get(f"{base}/{run_id}/report").json()
        assert revealed["review_status"] == "completed"
        stats = revealed["blind"]
        random_ai = [entry for entry in row["sample"] if entry["kind"] == "random"]
        assert stats["status"] == "revealed" and stats["random"]["sampled"] == len(random_ai) == 5
        assert stats["random"]["compared"] == 5 and stats["risk"]["sampled"] == 1
        assert stats["risk"]["agreed"] == 0  # 风险样本 AI 判基线更优，人工选了候选
        assert revealed["candidate"]["model"] == "gpt-5.6-sol"
        assert client.post(f"{base}/{run_id}/blind-review/items/{items[0]['job_id']}", json={
            "choice": "S", "outputs_sha256": items[0]["result_sha256"],
            "revision": 1}).status_code == 409
        # 揭晓后在报告页修订：锁定的盲评结论不变，修订单独计数
        risk_item = next(entry for entry in row["sample"] if entry["kind"] == "risk")
        risk_target = next(job for job in store.jobs(run_id) if job["stage"] == "judge" and
                           job["case_id"] == next(job for job in store.jobs(run_id)
                                                  if job["id"] == risk_item["job_id"])["case_id"])
        assert client.post(f"{base}/{run_id}/results/{risk_target['id']}/review", json={
            "label": "B", "outputs_sha256": risk_target["result_sha256"],
            "revision": 1}).status_code == 200
        after = client.get(f"{base}/{run_id}/report").json()
        assert after["blind"]["risk"]["revised"] == 1 and after["blind"]["risk"]["agreed"] == 0
        assert after["review_status"] == "in_progress"
        assert client.post(f"{base}/{run_id}/review/complete").status_code == 200


def test_evidence_returns_raw_request_and_both_responses(platform):
    client, store, settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    assert run_one(store, settings, "test")
    execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
    path = f"/api/agent-eval/v1/experiments/{run_id}/results/{execute['id']}/evidence"
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request"]["input"] and body["baseline"]["output"]
    assert body["candidate"] == body["baseline"]  # fixture 原样返回线上输出


def test_golden_review_publish_execute_and_report(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    run_id = _experiment(client, space_id, dataset["id"])
    execute = store.claim("test", 120)
    result, judge_input = engines.execute(execute, settings)
    assert store.finish(execute, result=result, judge_input=judge_input)
    judge = store.claim("test", 120)
    assert judge["stage"] == "judge"
    assert store.finish(judge, result=engines.grade(judge, settings))
    report = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report")
    assert report.status_code == 200, report.text
    assert report.json()["planned"] == 1
    assert report.json()["judged"] == 1
    assert report.json()["fixture"] is True
    assert "quality" not in report.json()
    results = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").json()
    assert len(results) == 2
    assert all("input" not in item for item in results)


def test_failed_golden_turn_keeps_its_observation(platform, monkeypatch):
    client, store, settings, space_id = platform
    run_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    real_execute_turn = engines.execute_turn

    def broken_after_create(task, **kwargs):
        observation = real_execute_turn(task, **kwargs)
        return observation.model_copy(update={
            "status": "execution_error", "stage_reached": "created",
            "response_messages": [], "error_code": "platform_http_500"})

    monkeypatch.setattr(engines, "execute_turn", broken_after_create)
    assert run_one(store, settings, "test")
    jobs = store.jobs(run_id)
    assert [job["stage"] for job in jobs] == ["execute"]  # 失败的 Turn 不进 Judge
    assert (jobs[0]["status"], jobs[0]["error_code"]) == ("failed", "platform_http_500")
    shown = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").json()[0]
    assert shown["result"]["observation"]["stage_reached"] == "created"
    assert shown["result"]["context"]
    assert "account_id" not in shown["result"]["observation"]
    evidence = client.get(
        f"/api/agent-eval/v1/experiments/{run_id}/results/{jobs[0]['id']}/evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["observation"]["error_code"] == "platform_http_500"


def test_golden_evidence_returns_the_masked_observation(platform):
    client, store, settings, space_id = platform
    run_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    assert run_one(store, settings, "test")
    execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
    path = f"/api/agent-eval/v1/experiments/{run_id}/results/{execute['id']}/evidence"
    observation = client.get(path).json()["observation"]
    assert observation["stage_reached"] == "response_observed"
    assert observation["profile_readback"]["settled"] is False  # Fixture 不轮询。
    assert observation["response_messages"] and "agent_logs" in observation
    assert "account_id" not in observation and "raw_agent_logs" not in observation


def test_golden_run_can_be_scoped_to_selected_turns(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id, [session_payload("GD-FIXTURE-0001"),
                                           session_payload("GD-FIXTURE-0002")])
    turns = [turn["turn_id"] for turn in dataset_turns(store, dataset["id"])]
    assert len(turns) == 2
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Scoped", "dataset_version_id": dataset["id"],
                               "execution_target": "fixture", "sample_scope": turns[1:2]})
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    assert [job["case_id"] for job in store.jobs(run_id)] == turns[1:2]
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()[
        "scope"] == "selected_turns"
    unknown = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                          headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                              "name": "Scoped", "dataset_version_id": dataset["id"],
                              "execution_target": "fixture", "sample_scope": ["missing#T9"]})
    assert unknown.status_code == 422


def dataset_turns(store, dataset_id):
    return [turn for session in store.dataset(dataset_id)["content"]["items"]
            for turn in session["turns"]]


@pytest.mark.parametrize("echoed,uncertain", [("same", False), (None, True)])
def test_platform_turn_registers_replay_request_id_before_sending(
    platform, monkeypatch, echoed, uncertain,
):
    monkeypatch.setattr("agent_eval.workspace_policy.prepare", lambda *_: {"context": {}})
    client, store, settings, space_id = platform
    run_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    with store.pool.connection() as c:
        c.execute("""UPDATE eval_jobs SET input=jsonb_set(input,'{config,execution_target}',
                     '"platform"') WHERE experiment_id=%s""", (run_id,))
    seen = {}

    def send(job, _settings, _key, request_id):
        seen["registered"] = store.one(
            "SELECT request_id,settled_at FROM eval_turn_traces WHERE job_id=%s", (job["id"],))
        result = {"replay_request_id": request_id,
                  "replay_request_id_echoed": request_id if echoed == "same" else echoed}
        if uncertain:
            raise engines.ExecutionError("platform_http_network", uncertain=True, result=result)
        return result, None

    monkeypatch.setattr(engines, "execute", send)
    assert run_one(store, settings, "test")
    request_id = seen["registered"]["request_id"]
    assert len(request_id) == 32 and seen["registered"]["settled_at"] is None
    trace = store.one("SELECT * FROM eval_turn_traces WHERE experiment_id=%s", (run_id,))
    assert (trace["request_id"], trace["workspace_id"], trace["status"]) == (
        request_id, "target-ws", "pending")
    assert trace["settled_at"] is not None
    assert trace["echo_verified"] is (True if echoed == "same" else None)
    job = store.jobs(run_id)[0]
    assert job["status"] == ("uncertain" if uncertain else "succeeded")
    assert job["result"]["replay_request_id"] == request_id


def _platform_run(client, store, space_id):
    run_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    with store.pool.connection() as c:
        c.execute("""UPDATE eval_jobs SET input=jsonb_set(input,'{config,execution_target}',
                     '"platform"') WHERE experiment_id=%s""", (run_id,))
    return run_id


def _gateway_record(request_id, model, seconds_ago):
    from test_traces import record

    when = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return record(request_id=request_id, model=model, when=when,
                  ended=when.strftime("%H:%M:%S.000"))


def test_turn_trace_is_collected_frozen_served_and_purged(platform, monkeypatch):
    from agent_eval import worker
    from atomic_eval.sls import JsonlRecordSource

    monkeypatch.setattr("agent_eval.workspace_policy.prepare", lambda *_: {"context": {}})
    client, store, settings, space_id = platform
    run_id = _platform_run(client, store, space_id)
    logs = [{"type": "modelCall"}, {"type": "modelCall"}, {"type": "tool", "name": "lookup"}]

    def send(job, _settings, _key, request_id):
        observation = {"observation_id": "obs-1", "run_id": run_id,
                       "session_id": "GD-FIXTURE-0001", "turn_id": job["case_id"], "seq": 1,
                       "repeat_index": 1, "status": "completed",
                       "stage_reached": "profile_settled", "account_id": "eval-1",
                       "execution_target": "platform", "agent_logs": logs}
        return ({"replay_request_id": request_id, "replay_request_id_echoed": request_id},
                {**job["input"], "observation": observation})

    monkeypatch.setattr(engines, "execute", send)
    assert run_one(store, settings, "test")
    trace = store.one("SELECT * FROM eval_turn_traces WHERE experiment_id=%s", (run_id,))
    assert (trace["expected_calls"], trace["echo_verified"]) == (2, True)
    assert trace["due_at"] is not None and trace["deadline_at"] > trace["due_at"]
    records = [_gateway_record(trace["request_id"], "main", 20),
               _gateway_record(trace["request_id"], "profile", 10)]
    message = [{"type": "input_text", "text": "13800138000 jane@example.com"},
               {"type": "input_image", "image_url": "https://assets.example.com/test.png"}]
    records[0]["msg"] = records[0]["msg"].replace(
        json.dumps({"model": "main", "input": "hi"}),
        json.dumps({"model": "main", "input": message, "api_key": "server-secret"}),
    )
    monkeypatch.setattr(worker, "SlsRecordSource", lambda: JsonlRecordSource(records))
    objects = LocalObjects(settings.object_root)

    def poll():
        with store.pool.connection() as c:
            c.execute("UPDATE eval_turn_traces SET due_at=now()-interval '1 second' "
                      "WHERE experiment_id=%s", (run_id,))
        assert worker.run_trace(store, settings, objects)
        return store.one("SELECT * FROM eval_turn_traces WHERE experiment_id=%s", (run_id,))

    first = poll()
    assert (first["status"], first["call_count"], first["poll_count"]) == ("pending", 2, 1)
    frozen = poll()
    assert (frozen["status"], frozen["call_count"]) == ("complete", 2)
    assert [span["model"] for span in frozen["summary"]] == ["main", "profile"]
    assert "request" not in frozen["summary"][0]
    execute = next(job for job in store.jobs(run_id) if job["stage"] == "execute")
    path = f"/api/agent-eval/v1/experiments/{run_id}/results/{execute['id']}/trace"
    served = client.get(path).json()
    assert served["status"] == "complete"
    assert [span["request"]["model"] for span in served["spans"]] == ["main", "profile"]
    assert served["spans"][0]["request"]["input"] == message
    assert "api_key" not in served["spans"][0]["request"]
    report = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()
    assert report["trace"]["statuses"] == {"complete": 1} and report["trace"]["avg_calls"] == 2
    with store.pool.connection() as c:
        c.execute("UPDATE eval_turn_traces SET collected_at=now()-interval '61 days' "
                  "WHERE experiment_id=%s", (run_id,))
    assert sweep(store, objects, apply=True)["traces_purged"] >= 1
    purged = client.get(path).json()
    assert "spans" not in purged and purged["object_purged_at"] and purged["summary"]


def test_trace_of_a_turn_that_never_sent_the_chat_is_closed_without_polling(
    platform, monkeypatch,
):
    monkeypatch.setattr("agent_eval.workspace_policy.prepare", lambda *_: {"context": {}})
    client, store, settings, space_id = platform
    run_id = _platform_run(client, store, space_id)

    def broken(job, _settings, _key, request_id):
        raise engines.ExecutionError("contact_lifecycle_error", result={
            "replay_request_id": request_id, "observation": {"stage_reached": "created"}})

    monkeypatch.setattr(engines, "execute", broken)
    assert run_one(store, settings, "test")
    trace = store.one("SELECT * FROM eval_turn_traces WHERE experiment_id=%s", (run_id,))
    assert (trace["status"], trace["error_code"], trace["due_at"]) == (
        "not_found", "chat_not_sent", None)


def test_traces_left_by_a_dead_worker_are_settled_on_recovery(platform):
    client, store, settings, space_id = platform
    run_id = _platform_run(client, store, space_id)
    job = store.claim("test", 120)
    store.register_trace(job, "target-ws")
    with store.pool.connection() as c:
        c.execute("UPDATE eval_jobs SET status='uncertain',finished_at=now() WHERE id=%s",
                  (job["id"],))
    assert store.recover_traces(60, 600) >= 1
    trace = store.one("SELECT * FROM eval_turn_traces WHERE experiment_id=%s", (run_id,))
    assert trace["settled_at"] and trace["due_at"] and trace["status"] == "pending"


def test_trace_readiness_reports_body_logging_or_unknown(platform, monkeypatch):
    from test_traces import Counting

    from agent_eval import api
    from atomic_eval.config import ConfigError

    client, _store, _settings, space_id = platform
    path = f"/api/agent-eval/v1/spaces/{space_id}/trace-readiness"
    monkeypatch.setattr(api, "SlsRecordSource", lambda: Counting({24: 5}, {}))
    assert client.get(path).json()["status"] == "disabled"

    def missing():
        raise ConfigError("SLS 缺少环境变量: SLS_ENDPOINT")

    monkeypatch.setattr(api, "SlsRecordSource", missing)
    assert client.get(path).json() == {"status": "unknown",
                                       "detail": "SLS 缺少环境变量: SLS_ENDPOINT"}


def test_trace_readiness_skips_spaces_shared_with_the_judge_tenant(platform, monkeypatch):
    """Judge 经网关调用时记在内部租户名下；与之同租户的 EVAL 空间，日志计数混有 Judge 流量。"""

    from test_traces import Counting

    from agent_eval import api

    _client, store, settings, space_id = platform
    shared = settings.model_copy(update={"internal_tenant_id": "target-ws"})
    monkeypatch.setattr(api, "SlsRecordSource", lambda: Counting({24: 1600}, {}))
    with TestClient(create_app(shared, store)) as client:
        result = client.get(f"/api/agent-eval/v1/spaces/{space_id}/trace-readiness").json()
    assert result["status"] == "unknown" and "Judge" in result["detail"]


def test_fixture_turn_does_not_register_a_trace(platform):
    client, store, settings, space_id = platform
    run_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    assert run_one(store, settings, "test")
    assert store.query("SELECT 1 FROM eval_turn_traces WHERE experiment_id=%s", (run_id,)) == []


def test_reviews_cannot_cross_experiments(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id)
    first = _experiment(client, space_id, dataset["id"])
    second = _experiment(client, space_id, dataset["id"])
    job = store.jobs(first)[0]
    assert client.post(f"/api/agent-eval/v1/experiments/{second}/results/{job['id']}/review",
                       json={"label": "unavailable", "outputs_sha256": "0" * 64}).status_code == 404


def test_atomic_review_revision_reopens_report_until_archived_again(platform):
    client, store, settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    while run_one(store, settings, "test"):
        pass
    target = next(job for job in client.get(
        f"/api/agent-eval/v1/experiments/{run_id}/results").json() if job["stage"] == "judge")
    url = f"/api/agent-eval/v1/experiments/{run_id}/results/{target['id']}/review"
    first = client.post(url, json={"label": "unavailable", "note": "Fixture 无质量结论",
                                   "outputs_sha256": target["result_sha256"]})
    assert first.status_code == 200, first.text
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()[
        "human_review"]["labels"] == {"unavailable": 1}
    complete_url = f"/api/agent-eval/v1/experiments/{run_id}/review/complete"
    assert client.post(complete_url).status_code == 200
    objects = LocalObjects(settings.object_root)
    while run_report(store, settings, objects, "test"):
        pass
    revised = client.post(url, json={"label": "unavailable", "note": "修订依据",
                                     "outputs_sha256": target["result_sha256"], "revision": 1})
    assert revised.status_code == 200, revised.text
    live = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").json()
    assert live["review_status"] == "in_progress"
    assert live["review_items"][0]["note"] == "修订依据"
    assert client.post(complete_url).status_code == 200
    while run_report(store, settings, objects, "test"):
        pass
    archived = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report/artifact").json()
    assert archived["review_items"][0]["note"] == "修订依据"


def test_authenticated_writes_require_configured_browser_origin(platform):
    _client, store, settings, space_id = platform
    protected = settings.model_copy(update={"dev_identity": False,
                                            "internal_tenant_id": "internal",
                                            "member_staff_ids": {"fde-1"},
                                            "public_origin": "https://eval.example.test"})
    with TestClient(create_app(protected, store)) as client:
        headers = {"tenant_id": "internal", "staff_id": "fde-1"}
        path = f"/api/agent-eval/v1/spaces/{space_id}/drafts"
        assert client.post(path, json={}, headers=headers).status_code == 403
        assert client.post(path, json={}, headers={
            **headers, "Origin": "https://attacker.example.test"}).status_code == 403
        assert client.post(path, json={}, headers={
            **headers, "Origin": "https://eval.example.test"}).status_code == 422


def test_annotation_job_freezes_prompts_and_requires_new_human_review(platform, monkeypatch):
    _client, store, settings, space_id = platform
    data = {"name": "AI assistance", "mode": "golden_regression",
            "items": [session_payload()], "shared": shared_payload()}
    draft = store.create_draft(space_id, data, "local:developer")
    approved = store.review_draft_case(draft["id"], "GD-FIXTURE-0001",
                                       draft["revision"], "local:developer")
    configured = settings.model_copy(update={"annotation_model": "judge-test"})
    with TestClient(create_app(configured, store)) as client:
        path = f"/api/agent-eval/v1/drafts/{draft['id']}/cases/GD-FIXTURE-0001/auto-draft"
        response = client.post(path, headers={"Idempotency-Key": uuid.uuid4().hex})
        assert response.status_code == 201, response.text
        assert "profile_prompt" not in response.json()
        assert "rubric_prompt" not in response.json()
        queued = store.annotation_jobs(draft["id"])[0]
        assert queued["profile_prompt"]["content_sha256"]
        assert queued["rubric_prompt"]["content_sha256"]

        import agent_eval.annotation as annotation

        original = approved["content"]["items"][0]
        changed = dict(original)
        changed["meta"] = {**changed["meta"], "design_intent": "Judge generated hint"}
        monkeypatch.setattr(annotation, "generate", lambda *_args: changed)
        assert run_annotation(store, configured, "test")
        result = store.draft(draft["id"])
        assert result["content"]["items"][0]["meta"]["design_intent"] == "Judge generated hint"
        assert result["review_state"] == {}
        assert store.annotation_jobs(draft["id"])[0]["status"] == "succeeded"


def test_bulk_annotations_skip_success_and_active_jobs_and_retry_failures(platform):
    _client, store, settings, space_id = platform
    cases = [session_payload(f"GD-BULK-{index}") for index in range(4)]
    draft = store.create_draft(space_id, {"name": "Bulk assistance", "mode": "golden_regression",
                              "items": cases, "shared": shared_payload()}, "local:developer")
    from agent_eval.annotation import frozen_prompts

    profile, rubric = frozen_prompts()
    for case, failure in ((cases[0], False), (cases[1], True)):
        store.create_annotation(draft["id"], case["session_id"], profile, rubric, "test-model",
                                uuid.uuid4().hex, "local:developer")
        job = store.claim_annotation("test", 120)
        assert store.finish_annotation(job, session=None if failure else case,
                                       error_code="annotation_failed" if failure else None)
    store.create_annotation(draft["id"], cases[2]["session_id"], profile, rubric,
                            "test-model", uuid.uuid4().hex, "local:developer")
    configured = settings.model_copy(update={"annotation_model": "test-model"})
    with TestClient(create_app(configured, store)) as client:
        path = f"/api/agent-eval/v1/drafts/{draft['id']}/auto-draft"
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        response = client.post(path, headers=headers)
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["skipped_success"] == [cases[0]["session_id"]]
        assert result["skipped_active"] == [cases[2]["session_id"]]
        assert {j["session_id"] for j in result["jobs"]} == {
            cases[1]["session_id"], cases[3]["session_id"],
        }
        assert all(j["status"] == "queued" for j in result["jobs"])
        assert "profile_prompt" not in result["jobs"][0]
        assert client.post(path, headers=headers).json() == result
        again = client.post(path, headers={"Idempotency-Key": uuid.uuid4().hex}).json()
        assert again["jobs"] == []
        assert len(again["skipped_active"]) == 3
        assert len(store.annotation_jobs(draft["id"])) == 5
        for _ in range(3):
            job = store.claim_annotation("test", 120)
            case = next(c for c in cases if c["session_id"] == job["session_id"])
            assert store.finish_annotation(job, session=case)
        finished = client.post(path, headers={"Idempotency-Key": uuid.uuid4().hex}).json()
        assert len(finished["skipped_success"]) == 4
        assert finished["jobs"] == []


def test_draft_images_preserve_urls_and_authenticated_previews(platform, monkeypatch):
    from agent_eval import media

    client, store, settings, space_id = platform
    case = session_payload()
    attachment = {"name": "diagram.png", "content_type": "image/png",
                  "url": "https://assets.example.com/diagram.png"}
    for message in (case["turns"][0]["input"]["customer_messages"][0],
                    case["turns"][0]["expected"]["messages"][0]):
        message.update(type="image", attachments=[attachment])
    case["turns"][0]["rubric"] = (
        "【标注说明】AI 预填：依据线上回复、客户消息与金标规则生成，待人工抽检后修改。"
    )
    draft = store.create_draft(space_id, {"name": "Images", "mode": "golden_regression",
                              "items": [case], "shared": shared_payload()}, "local:developer")
    projected = client.get(f"/api/agent-eval/v1/drafts/{draft['id']}").json()
    assert attachment["url"] in json.dumps(projected)
    turn = projected["content"]["items"][0]["turns"][0]
    assert "规则预填" in turn["rubric"]
    seen = []

    def load_image(item, allowed_hosts):
        seen.append(item["url"])
        return b"image-bytes", "image/png"

    monkeypatch.setattr(media, "load_image", load_image)
    urls = [message["attachments"][0]["preview_url"] for message in (
        turn["input"]["customer_messages"][0], turn["expected"]["messages"][0],
    )]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == b"image-bytes"
        assert response.headers["content-type"] == "image/png"
        assert response.headers["cache-control"] == "private, no-store"
        assert client.get(url[:-1] + "99").status_code == 404
    assert seen == [attachment["url"], attachment["url"]]
    assert "preview_url" not in json.dumps(store.draft(draft["id"])["content"])
    protected = settings.model_copy(update={"dev_identity": False,
                                            "internal_tenant_id": "internal",
                                            "member_staff_ids": {"fde-1"}})
    with TestClient(create_app(protected, store)) as anonymous:
        assert anonymous.get(urls[0]).status_code == 403


def test_golden_report_images_use_frozen_result_and_reference(platform, monkeypatch):
    from agent_eval import media

    client, store, settings, space_id = platform
    case = session_payload()
    attachment = {"name": "diagram.png", "content_type": "image/png",
                  "url": "https://assets.example.com/diagram.png"}
    contact = "13800138000 jane@example.com"
    for message in (case["turns"][0]["input"]["customer_messages"][0],
                    case["turns"][0]["expected"]["messages"][0]):
        message.update(type="image", content=contact, attachments=[attachment])
    dataset = _dataset(client, space_id, [case])
    shown_dataset = client.get(f"/api/agent-eval/v1/datasets/{dataset['id']}").text
    assert contact in shown_dataset and attachment["url"] in shown_dataset
    run_id = _experiment(client, space_id, dataset["id"])
    while run_one(store, settings, "test-images"):
        pass
    job = next(j for j in store.jobs(run_id) if j["stage"] == "execute")
    base = f"/api/agent-eval/v1/experiments/{run_id}/results/{job['id']}/media"
    monkeypatch.setattr(media, "load_image", lambda item, allowed_hosts: (
        (b"frozen-image", "image/png") if item["url"] == attachment["url"] else None))
    for side in ("context", "reference"):
        response = client.get(f"{base}/{side}/0/0")
        assert response.status_code == 200, response.text
        assert response.content == b"frozen-image"
        assert response.headers["cache-control"] == "private, no-store"
        assert client.get(f"{base}/{side}/99/0").status_code == 404
        assert client.get(f"{base}/{side}/0/-1").status_code == 404
    assert client.get(f"{base}/candidate/0/0").status_code == 404
    assert client.get(base.replace(run_id, "wrong-run") + "/context/0/0").status_code == 404
    projected = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").text
    assert attachment["url"] in projected and contact in projected
    objects = LocalObjects(settings.object_root)
    while run_report(store, settings, objects, "test-images"):
        pass
    exported = client.get(f"/api/agent-eval/v1/experiments/{run_id}/report/artifact")
    assert exported.status_code == 200
    assert attachment["url"] in exported.text and contact in exported.text
    frozen = store.dataset(dataset["id"])["content"]["items"][0]
    assert frozen == case
    protected = settings.model_copy(update={"dev_identity": False,
                                            "internal_tenant_id": "internal",
                                            "member_staff_ids": {"fde-1"}})
    with TestClient(create_app(protected, store)) as anonymous:
        assert anonymous.get(f"{base}/reference/0/0").status_code == 403
    with store.pool.connection() as c:
        c.execute("UPDATE eval_experiments SET evidence_purged_at=now() WHERE id=%s", (run_id,))
    assert client.get(f"{base}/reference/0/0").status_code == 410


NO_CALLS = "所选时间窗口内没有可等价回放的模型调用"


@pytest.mark.parametrize(("error", "shown"), [
    (ValueError(NO_CALLS), NO_CALLS),
    (RuntimeError("upstream body with customer data"), None),
])
def test_failed_capture_shows_only_platform_messages(platform, monkeypatch, error, shown):
    client, store, settings, space_id = platform
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/captures/atomic",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Atomic window", "start": "2026-09-22T00:00:00+08:00",
                               "end": "2026-09-22T01:00:00+08:00", "size": 1,
                           })
    assert response.status_code == 201, response.text

    def fail(*_args):
        raise error

    monkeypatch.setattr(capture, "collect", fail)
    assert run_capture(store, settings, LocalObjects(settings.object_root), "test")
    found = client.get(f"/api/agent-eval/v1/spaces/{space_id}/captures").json()[0]
    assert found["status"] == "failed"
    assert found["error_message"] == shown


def test_repeatedly_interrupted_capture_fails_instead_of_looping(platform):
    client, store, _settings, space_id = platform
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/captures/atomic",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Atomic window", "start": "2026-09-22T00:00:00+08:00",
                               "end": "2026-09-22T01:00:00+08:00", "size": 1,
                           })
    assert response.status_code == 201, response.text
    for _ in range(3):
        # 租约立即过期，模拟 worker 在采集中途被杀
        assert store.claim_capture("test", 0)
        store.recover_captures()
    assert store.claim_capture("test", 0) is None
    found = client.get(f"/api/agent-eval/v1/captures/{response.json()['id']}").json()
    assert found["status"] == "failed"
    assert found["error_message"] == "采集多次中断未能完成，请缩短时间窗口后重试"


def test_capture_fact_and_object_checksum(platform):
    client, store, settings, space_id = platform
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/captures/atomic",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Atomic window", "start": "2026-09-22T00:00:00+08:00",
                               "end": "2026-09-22T01:00:00+08:00", "size": 1,
                           })
    assert response.status_code == 201, response.text
    job = store.claim_capture("test", 120)
    objects = LocalObjects(settings.object_root)
    key = f"captures/{job['id']}.json.gz"
    sha = objects.put(key, {"manifest": {"count": 0}, "calls": []})
    assert store.finish_capture(job, object_key=key, content_sha256=sha,
                                source_hash="source-sha", summary={"count": 0})
    found = client.get(f"/api/agent-eval/v1/captures/{job['id']}").json()
    assert found["status"] == "succeeded"
    assert "object_key" not in found
    listed = client.get(f"/api/agent-eval/v1/spaces/{space_id}/captures").json()[0]
    assert listed["request"] == {"start": "2026-09-22T00:00:00+08:00",
                                 "end": "2026-09-22T01:00:00+08:00", "size": 1}
    assert listed["outcome"] == {"count": 0}
    assert "spec" not in listed
    assert objects.get(key, sha)["calls"] == []
    assert len(client.get(f"/api/agent-eval/v1/spaces/{space_id}/snapshots").json()) == 1
    dataset = store.publish_atomic(space_id, {
        "name": "Frozen source", "mode": "atomic_replay", "items": [{}],
        "shared": {}, "source_snapshot_id": found["snapshot_id"],
    }, "local:developer")
    with store.pool.connection() as connection:
        connection.execute(
            "UPDATE eval_source_snapshots SET created_at=now()-interval '31 days' WHERE id=%s",
            (found["snapshot_id"],),
        )
    assert sweep(store, objects)["snapshots"] == 1
    assert sweep(store, objects, apply=True)["snapshots_purged"] == 1
    assert store.snapshot(found["snapshot_id"])["object_purged_at"] is not None
    assert store.dataset(dataset["id"])["content_purged_at"] is None


def test_published_dataset_expires_after_730_days_without_live_dependents(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    with store.pool.connection() as connection:
        connection.execute(
            "UPDATE eval_dataset_versions SET created_at=now()-interval '731 days' WHERE id=%s",
            (dataset["id"],),
        )
    objects = LocalObjects(settings.object_root)
    assert sweep(store, objects)["datasets"] == 1
    assert sweep(store, objects, apply=True)["datasets_purged"] == 1
    assert store.dataset(dataset["id"])["content"] == {"purged": True}
    assert client.get(f"/api/agent-eval/v1/datasets/{dataset['id']}").status_code == 410


def test_manual_profile_correction_recalculates_later_turns_and_requires_review(platform):
    _client, store, _settings, space_id = platform
    session = session_payload()
    session["turns"].append({
        "turn_id": "GD-FIXTURE-0001#T2", "session_id": session["session_id"], "seq": 2,
        "input": {"customer_messages": [{"seq": 1, "type": "text", "content": "Now Shanghai."}]},
        "expected": {
            "messages": [{"seq": 1, "type": "text", "content": "Understood."}],
            "profile_update": {"city": {"value": "Shanghai", "updated_from": "Chengdu"}},
            "profile_snapshot": {"city": "Shanghai"}, "tool_calls": [],
        },
        "rubric": "Acknowledge the new city.",
    })
    session["expected"]["memory_after"]["profile_snapshot"] = {"city": "Shanghai"}
    draft = store.create_draft(space_id, {
        "name": "Corrected gold", "mode": "golden_regression",
        "items": [session], "shared": shared_payload(),
    }, "local:developer")
    with pytest.raises(Conflict, match="画像字段 city 的格式无效"):
        store.edit_draft_case(draft["id"], session["session_id"], draft["revision"], {
            "turn_index": 0, "rubric": "Acknowledge the city.",
            "expected_messages": [None], "expected_profile_update": {"city": []},
        }, "local:developer")
    first = store.edit_draft_case(draft["id"], session["session_id"], draft["revision"], {
        "turn_index": 0, "rubric": "Acknowledge the city.",
        "expected_messages": [None],
        "expected_profile_update": {"city": {"value": "Hangzhou", "source": "pulled"}},
    }, "local:developer")
    corrected = first["content"]["items"][0]
    assert corrected["turns"][0]["expected"]["profile_update"]["city"]["source"] == "manual"
    assert corrected["turns"][0]["expected"]["profile_snapshot"] == {"city": "Hangzhou"}
    assert corrected["turns"][1]["expected"]["profile_update"]["city"]["updated_from"] == "Hangzhou"
    assert corrected["turns"][1]["expected"]["profile_snapshot"] == {"city": "Shanghai"}
    assert corrected["expected"]["memory_after"]["profile_snapshot"] == {"city": "Shanghai"}
    assert "【画像】" in corrected["turns"][0]["rubric"]
    with pytest.raises(Conflict, match="案例尚未审核"):
        store.publish_draft(first["id"], first["revision"], "local:developer")
    reviewed = store.review_draft_case(first["id"], session["session_id"],
                                       first["revision"], "local:developer")
    published = store.publish_draft(reviewed["id"], reviewed["revision"],
                                    "local:developer")
    assert published["content"]["items"][0] == corrected


def test_regrade_reuses_only_unchanged_execution_inputs_and_archives_report(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    source_id = _experiment(client, space_id, dataset["id"])
    execute = store.claim("test", 120)
    output, judge_input = engines.execute(execute, settings)
    store.finish(execute, result=output, judge_input=judge_input,
                 engine_sha256=engines.engine_fingerprint())
    judge = store.claim("test", 120)
    store.finish(judge, result=engines.grade(judge, settings),
                 engine_sha256=engines.engine_fingerprint())

    derived = store.derive_draft(dataset["id"], "local:developer")
    edited = store.edit_draft_case(derived["id"], "GD-FIXTURE-0001", derived["revision"],
                                   {"turn_index": 0, "rubric": "Acknowledge the city clearly.",
                                    "expected_messages": [None],
                                    "expected_profile_update": None}, "local:developer")
    reviewed = store.review_draft_case(edited["id"], "GD-FIXTURE-0001",
                                       edited["revision"], "local:developer")
    new_dataset = store.publish_draft(reviewed["id"], reviewed["revision"],
                                      "local:developer")
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Regrade", "dataset_version_id": new_dataset["id"],
                               "execution_target": "fixture", "reuse_experiment_id": source_id,
                           })
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    copied = [job for job in store.jobs(run_id) if job["stage"] == "execute"][0]
    assert copied["source_job_id"] == execute["id"]
    assert copied["status"] == "succeeded"
    assert store.experiment(source_id)["dataset_version_id"] == dataset["id"]
    next_judge = store.claim("test", 120)
    store.finish(next_judge, result=engines.grade(next_judge, settings),
                 engine_sha256=engines.engine_fingerprint())
    objects = LocalObjects(settings.object_root)
    for _ in range(10):
        if store.report_exports(run_id)[0]["status"] == "succeeded":
            break
        assert run_report(store, settings, objects, "test")
    exports = store.report_exports(run_id)
    assert exports[0]["status"] == "succeeded"
    report = objects.get(exports[0]["object_key"], exports[0]["content_sha256"])
    assert report["source_experiment_id"] == source_id
    assert report["cases"][0]["execution_source_job_id"] == execute["id"]
    assert report["cases"][0]["execution"]["candidate"]

    changed = store.derive_draft(dataset["id"], "local:developer")
    altered = dict(changed["content"])
    altered["items"][0]["turns"][0]["input"]["customer_messages"][0]["content"] = "New input"
    changed = store.replace_draft(changed["id"], altered, changed["revision"],
                                  "local:developer")
    changed = store.review_draft_case(changed["id"], "GD-FIXTURE-0001",
                                      changed["revision"], "local:developer")
    changed_dataset = store.publish_draft(changed["id"], changed["revision"],
                                          "local:developer")
    rejected = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Must rerun", "dataset_version_id": changed_dataset["id"],
                               "execution_target": "fixture", "reuse_experiment_id": source_id,
                           })
    assert rejected.status_code == 409
    assert "重新执行" in rejected.json()["detail"]


def test_first_judge_evidence_freeze_is_immutable(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    run_id = _experiment(client, space_id, dataset["id"])
    assert run_one(store, settings, "test")
    judge = store.claim("test", 120)
    assert judge["stage"] == "judge"
    first = {"version": "turn-evidence-v1", "tool_results": [], "status": "unavailable"}
    assert store.freeze_judge_evidence(judge, first) == first
    assert store.freeze_judge_evidence(judge, {"status": "changed"}) == first
    stored = next(job for job in store.jobs(run_id) if job["id"] == judge["id"])
    assert stored["input"]["judge_evidence"] == first
    stale = {**judge, "lease_token": uuid.uuid4().hex}
    with pytest.raises(Conflict):
        store.freeze_judge_evidence(stale, {"status": "must not write"})


def test_worker_freezes_v12_evidence_before_judge_and_reuses_it_on_retry(platform, monkeypatch):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    run_id = _experiment(client, space_id, dataset["id"])
    assert run_one(store, settings, "test")
    with store.pool.connection() as c:
        c.execute("""UPDATE eval_experiments SET manifest=jsonb_set(manifest,
                  '{judge_prompt_version}','"full-evaluation-v12"') WHERE id=%s""", (run_id,))
        c.execute("""UPDATE eval_jobs SET input=jsonb_set(input,
                  '{config,execution_target}','"platform"')
                  WHERE experiment_id=%s AND stage='judge'""", (run_id,))
    seen = []

    def grade(job, *_):
        seen.append(job["input"]["judge_evidence"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("temporary test timeout")
        return {"turns": []}

    monkeypatch.setattr(engines, "grade", grade)
    assert run_one(store, settings, "test")
    assert run_one(store, settings, "test")
    assert seen[0] == seen[1]
    assert seen[0]["status"] == "unavailable"
    assert seen[0]["image_pixels_available"] is False
    assert seen[0]["diagnostics"] == ["trace_not_available_at_judging"]


def test_repeated_regrade_keeps_original_execution_trace(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    source = _experiment(client, space_id, dataset["id"])
    assert run_one(store, settings, "test")
    assert run_one(store, settings, "test")
    root_job = next(job for job in store.jobs(source) if job["stage"] == "execute")
    request_id = store.register_trace(root_job, "target-ws")
    previous = source
    previous_job = root_job
    for index in range(2):
        response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
            headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                "name": f"Regrade {index}", "dataset_version_id": dataset["id"],
                "execution_target": "fixture", "reuse_experiment_id": previous})
        assert response.status_code == 201, response.text
        current = response.json()["id"]
        copied = next(job for job in store.jobs(current) if job["stage"] == "execute")
        assert copied["source_job_id"] == previous_job["id"]
        rows = store.experiment_traces(current, copied["id"])
        assert len(rows) == 1
        assert rows[0]["request_id"] == request_id
        assert rows[0]["job_id"] == root_job["id"]
        assert rows[0]["result_job_id"] == copied["id"]
        assert run_one(store, settings, "test")
        previous, previous_job = current, copied


def test_retention_waits_for_review_and_final_report(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    run_id = _experiment(client, space_id, dataset["id"])
    execution = store.claim("test", 120)
    output, judge_input = engines.execute(execution, settings)
    store.finish(execution, result=output, judge_input=judge_input)
    judge = store.claim("test", 120)
    store.finish(judge, result=engines.grade(judge, settings))
    with store.pool.connection() as connection:
        connection.execute(
            "UPDATE eval_experiments SET created_at=now()-interval '31 days' WHERE id=%s",
            (run_id,),
        )
    objects = LocalObjects(settings.object_root)
    assert sweep(store, objects)["evidence"] == 0
    results = client.get(f"/api/agent-eval/v1/experiments/{run_id}/results").json()
    result = next(row for row in results if row["stage"] == "judge")
    review = client.post(f"/api/agent-eval/v1/experiments/{run_id}/results/{result['id']}/review",
                         json={"label": "unavailable", "outputs_sha256": result["result_sha256"],
                               "revision": 0, "note": "Fixture only"})
    assert review.status_code == 200, review.text
    complete = client.post(f"/api/agent-eval/v1/experiments/{run_id}/review/complete")
    assert complete.status_code == 200, complete.text
    assert sweep(store, objects)["evidence"] == 0
    for _ in range(10):
        if store.report_exports(run_id)[0]["status"] == "succeeded":
            break
        assert run_report(store, settings, objects, "test")
    assert sweep(store, objects, apply=True)["evidence_purged"] == 1
    assert all(job["result"] is None and job["input"] == {"purged": True}
               for job in store.jobs(run_id))
    assert client.get(f"/api/agent-eval/v1/experiments/{run_id}/report").status_code == 200


def test_workspace_policy_freeze_is_shared_and_lease_checked(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    original = dataset["content_sha256"]
    run_id = _experiment(client, space_id, dataset["id"])
    execute = store.claim("test", 120)
    first = {"workspace_id": "target-ws", "context": {"guidance": "first"}}
    assert store.freeze_workspace_policy(execute, first) == first
    output, judge_input = engines.execute(execute, settings)
    assert store.finish(execute, result=output, judge_input=judge_input)
    judge = store.claim("test", 120)
    assert store.freeze_workspace_policy(judge, {"context": {"guidance": "changed"}}) == first
    assert store.experiment(run_id)["manifest"]["workspace_policy"] == first
    assert store.dataset(dataset["id"])["content_sha256"] == original
    with pytest.raises(Conflict):
        store.freeze_workspace_policy({**judge, "lease_token": uuid.uuid4().hex}, first)


def test_turn_rubric_is_shared_across_repeats_and_lease_checked(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
        headers={"Idempotency-Key": uuid.uuid4().hex}, json={"name": "Frozen criteria",
            "dataset_version_id": dataset["id"], "execution_target": "fixture", "repeats": 2})
    assert response.status_code == 201
    run_id = response.json()["id"]
    first = {"rubric": "First independent criterion", "rubric_sha256": "first"}
    frozen = []
    for _ in range(4):
        job = store.claim("test", 120)
        if job["stage"] == "execute":
            output, judge_input = engines.execute(job, settings)
            assert store.finish(job, result=output, judge_input=judge_input)
        else:
            frozen.append(store.freeze_turn_rubric(
                job, first if not frozen else {"rubric": "changed"}))
            with pytest.raises(Conflict):
                store.freeze_turn_rubric({**job, "lease_token": uuid.uuid4().hex}, first)
            with pytest.raises(Conflict):
                store.freeze_turn_rubric({**job, "case_id": "different-turn"}, first)
            manifest = store.experiment(run_id)["manifest"]
            assert store.finish(job, result=engines.grade(job, settings, manifest))
    assert frozen == [first, first]
    assert store.dataset(dataset["id"])["content_sha256"] == dataset["content_sha256"]


def test_golden_judge_retry_keeps_execution_and_frozen_rules(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    run_id = _experiment(client, space_id, dataset["id"])
    execution = store.claim("test", 120)
    result, judge_input = engines.execute(execution, settings)
    assert store.finish(execution, result=result, judge_input=judge_input)
    job = store.claim("test", 120)
    frozen = store.freeze_turn_rubric(job, {"rubric": "Frozen requirements"})
    assert store.finish(job, error_code="judge_result_invalid")
    original_execution = store.jobs(run_id)[0]
    path = f"/api/agent-eval/v1/experiments/{run_id}/retry-failed"
    key = uuid.uuid4().hex
    response = client.post(path, headers={"Idempotency-Key": key}, json={})
    assert response.status_code == 200, response.text
    assert response.json()["requeued"] == 1
    assert client.post(path, headers={"Idempotency-Key": key}, json={}).status_code == 200
    assert next(j for j in store.jobs(run_id) if j["stage"] == "execute") == original_execution
    retry = store.claim("test", 120)
    assert retry["stage"] == "judge"
    assert retry["input"]["observation"] == judge_input["observation"]
    assert store.freeze_turn_rubric(retry, {"rubric": "Do not replace"}) == frozen
    manifest = store.experiment(run_id)["manifest"]
    assert store.finish(retry, result=engines.grade(retry, settings, manifest))
    assert store.experiment(run_id)["status"] == "completed"


def test_golden_retry_cannot_repeat_a_chat_execution(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id)
    run_id = _experiment(client, space_id, dataset["id"])
    execution = store.claim("test", 120)
    assert store.finish(execution, error_code="execution_failed")
    response = client.post(f"/api/agent-eval/v1/experiments/{run_id}/retry-failed",
        headers={"Idempotency-Key": uuid.uuid4().hex}, json={})
    assert response.status_code == 409
    assert next(j for j in store.jobs(run_id) if j["stage"] == "execute")["status"] == "failed"


def test_golden_batch_freezes_unconfirmed_references_and_repeats(platform, monkeypatch):
    client, store, settings, space_id = platform
    configured = settings.model_copy(update={"judge_model": "judge-test"})
    attempts = []

    def collect(*_):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError("Read-only sampling temporarily failed")
        return {}, {"count": 1}, "sha", None

    monkeypatch.setattr(capture, "collect", collect)
    source = {"name": "Reference", "mode": "golden_regression", "shared": shared_payload(),
              "items": [session_payload()]}

    def build(_raw, snapshot_id, request):
        from copy import deepcopy
        data = deepcopy(source)
        data.update(name=request["name"], source_snapshot_id=snapshot_id)
        return data, {}

    monkeypatch.setattr(capture, "draft_from_snapshot", build)
    base = "/api/agent-eval/v1/batches"
    try:
        with TestClient(create_app(configured, store)) as client:
            body = {"name": "Golden batch", "space_ids": [space_id], "mode": "golden_regression",
                    "size": 50, "min_turns": 3, "repeats": 3,
                    "start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
            headers = {"Idempotency-Key": uuid.uuid4().hex}
            assert client.post(base, json={**body, "size": 101}, headers=headers).status_code == 422
            assert client.post(base, json={**body, "candidate_gateway_model": "x"},
                               headers=headers).status_code == 422
            result = client.post(base, json=body, headers=headers)
            assert result.status_code == 201, result.text
            batch_id = result.json()["id"]
            assert client.post(base, json=body, headers=headers).json()["id"] == batch_id
            assert client.post(base, json={**body, "size": 49}, headers=headers).status_code == 409
            item = store.batch(batch_id)[1][0]
            job = store.capture(item["capture_id"])
            assert job["mode"] == "golden_regression"
            assert job["spec"]["policy"]["quota_per_workspace"] == 50
            assert job["spec"]["policy"]["min_turns"] == 3
            assert run_capture(store, configured, LocalObjects(configured.object_root), "test")
            assert store.capture(item["capture_id"])["status"] == "failed"
            assert client.post(f"{base}/{batch_id}/retry").json()["changed"] == 1
            assert client.post(f"{base}/{batch_id}/retry").json()["changed"] == 0
            assert run_capture(store, configured, LocalObjects(configured.object_root), "test")
            assert run_batch_starts(store, configured)
            item = store.batch(batch_id)[1][0]
            assert not item["error_message"], item
            run = store.experiment(item["experiment_id"])
            dataset = store.dataset(item["dataset_version_id"])
            assert not dataset["review_state"]
            assert dataset["content"]["shared"]["context"]["reference_status"] == "generated"
            assert run["manifest"]["config"]["repeats"] == 3
            jobs = store.jobs(run["id"])
            assert len(jobs) == len(source["items"][0]["turns"]) * 3
            assert {job["repeat_index"] for job in jobs} == {1, 2, 3}
            assert store.freeze_reference_capture(item["capture_id"], dataset["content"],
                                                  "local:developer") == dataset["id"]
            assert not run_batch_starts(store, configured)
            detail = client.get(f"{base}/{batch_id}").json()["items"][0]
            assert detail["capture_outcome"]["count"] == 1
            assert detail["planned_turns"] == len(jobs)
            assert client.post(f"{base}/{batch_id}/retry").json()["changed"] == 0
            store.cancel(run["id"], "local:developer")
    finally:
        with store.pool.connection() as c:
            c.execute("DELETE FROM eval_batch_items WHERE space_id=%s", (space_id,))
            c.execute("DELETE FROM eval_batches WHERE name='Golden batch'")


def test_data_archive_hides_lists_but_preserves_versions_and_can_restore(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id)["id"]
    original = store.dataset(dataset)["content_sha256"]
    draft = store.derive_draft(dataset, "local:developer")
    base = "/api/agent-eval/v1"
    for kind, row_id in [("datasets", dataset), ("drafts", draft["id"])]:
        path = f"{base}/data/{kind}/archive"
        assert client.post(path, json={"ids": [row_id]}).json()["changed"] == 1
        assert row_id not in {row["id"] for row in client.get(
            f"{base}/spaces/{space_id}/{kind}").json()}
        assert row_id in {row["id"] for row in client.get(
            f"{base}/spaces/{space_id}/{kind}?include_archived=true").json()}
        assert client.get(f"{base}/{kind}/{row_id}").status_code == 200
        if kind == "drafts":
            assert row_id not in {row["id"] for row in client.get(
                f"{base}/workbench").json()["gold_drafts"]}
        assert client.post(path, json={"ids": [row_id], "archived": False}).json()["changed"] == 1
    assert store.dataset(dataset)["content_sha256"] == original
    job = store.create_capture(space_id, "golden_regression", "Running capture", {},
                               uuid.uuid4().hex, "local:developer")
    assert client.post(f"{base}/data/captures/archive", json={"ids": [job["id"]]}).json()[
        "changed"] == 0


def test_golden_platform_failure_stops_requests_without_retrying_uncertain_deliveries(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id, [session_payload(f"guard-{i}") for i in range(3)])
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Failure guard", "dataset_version_id": dataset["id"],
                               "execution_target": "fixture", "repeats": 3})
    assert response.status_code == 201
    rid = response.json()["id"]
    successful = store.claim("failure-guard", 300)
    assert store.finish(successful, result={})
    for index in range(5):
        job = store.claim("failure-guard", 300)
        assert job["experiment_id"] == rid
        assert store.finish(job, error_code="platform_http_500", uncertain=True)
        assert store.experiment(rid)["status"] == ("uncertain" if index == 4 else "running")
    jobs = store.jobs(rid)
    assert sum(j["status"] == "uncertain" for j in jobs) == 5
    assert sum(j["status"] == "cancelled" for j in jobs) == len(jobs) - 6
    assert all(j["attempt"] == 1 for j in jobs if j["status"] == "uncertain")
    assert store.experiment(rid)["manifest"]["execution_stop"]["error_count"] == 5
    assert store.claim("failure-guard", 300) is None


def test_scheduler_rotates_runs_and_scores_finished_answers_before_more_execution(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id)
    first = _experiment(client, space_id, dataset["id"])
    second = _experiment(client, space_id, dataset["id"])
    job1 = store.claim("scheduler", 300)
    job2 = store.claim("scheduler", 300)
    assert job1["experiment_id"] == first and job2["experiment_id"] == second
    assert store.finish(job1, result={}, judge_input=job1["input"])
    judge = store.claim("scheduler", 300)
    assert judge["experiment_id"] == first and judge["stage"] == "judge"
    assert store.finish(judge, result={})
    assert store.finish(job2, error_code="test-stopped")
    store.cancel(first, "local:developer")
    store.cancel(second, "local:developer")


def test_success_breaks_platform_failure_streak(platform):
    client, store, _settings, space_id = platform
    dataset = _dataset(client, space_id, [session_payload(f"streak-{i}") for i in range(3)])
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                           headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                               "name": "Intermittent failures", "dataset_version_id": dataset["id"],
                               "execution_target": "fixture", "repeats": 3})
    rid = response.json()["id"]
    for index in range(9):
        job = store.claim("failure-streak", 300)
        if index == 4:
            store.finish(job, result={})
        else:
            store.finish(job, error_code="platform_http_500", uncertain=True)
        assert "execution_stop" not in store.experiment(rid)["manifest"]
    assert all(j["status"] != "cancelled" for j in store.jobs(rid))


def test_golden_report_queries_exclude_repeated_inputs_without_changing_results(platform):
    from agent_eval.reporting import report

    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    rid = _experiment(client, space_id, dataset["id"])
    while run_one(store, settings, "compact-report"):
        pass
    with store.pool.connection() as c:
        c.execute("""UPDATE eval_jobs SET input=input ||
                     jsonb_build_object('unused_legacy_blob',repeat('x',1048576))
                     WHERE experiment_id=%s""", (rid,))
    full = store.jobs(rid)
    compact = store.report_jobs(rid)
    assert len(json.dumps(full, default=str)) > len(json.dumps(compact, default=str)) + 1048576
    for row in compact:
        assert set(row["input"]) <= {"mode", "config", "observation"}
        assert "item" not in row["input"] and "shared" not in row["input"]
    run = store.experiment(rid)
    stored_dataset = store.dataset(dataset["id"])
    assert report(run, stored_dataset, compact, [], include_cases=True) == report(
        run, stored_dataset, full, [], include_cases=True)
    response = client.get(f"/api/agent-eval/v1/experiments/{rid}/results")
    assert response.status_code == 200
    assert {row["id"] for row in response.json()} == {row["id"] for row in full}
    assert "unused_legacy_blob" in store.jobs(rid)[0]["input"]


def test_report_query_keeps_atomic_inputs_for_existing_response_annotations(platform):
    client, store, _settings, space_id = platform
    rid = _atomic_fixture_run(client, store, space_id)
    assert store.report_jobs(rid) == store.jobs(rid)


def _unpack_evidence(response):
    import hashlib
    from io import BytesIO
    from zipfile import ZipFile

    assert response.status_code == 200, response.text if response.status_code != 200 else ''
    assert response.headers['content-type'] == 'application/zip'
    assert response.headers['cache-control'] == 'private, no-store'
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files['manifest.json'])
    for name, info in manifest['files'].items():
        assert hashlib.sha256(files[name]).hexdigest() == info['sha256']
        assert len(files[name]) == info['bytes']
        if name.endswith('.jsonl'):
            assert len(files[name].splitlines()) == info['records']
    return files, manifest


def test_evidence_bundle_exports_raw_values_and_all_saved_calls_without_writes(
    platform, monkeypatch,
):
    client, store, settings, space_id = platform
    contact = '13800138000 jane@example.com https://example.com/order?id=9'
    image = 'https://assets.example.com/photo.png?Signature=media-signature&Expires=1900000000'
    case = session_payload()
    message = case['turns'][0]['input']['customer_messages'][0]
    message.update(content=contact, type='image', attachments=[
        {'name': 'photo.png', 'content_type': 'image/png', 'url': image}])
    dataset = _dataset(client, space_id, [case])
    run_id = _experiment(client, space_id, dataset['id'])
    monkeypatch.setattr(engines, 'trace_workspace', lambda _: 'target-ws')
    while run_one(store, settings, 'bundle-test'):
        pass
    objects = LocalObjects(settings.object_root)
    trace = store.one('SELECT * FROM eval_turn_traces WHERE experiment_id=%s', (run_id,))
    span = {'span_id': 'saved-call-1', 'has_body': True,
            'request': {'input': contact, 'image_url': image, 'api_key': 'hidden-key'},
            'response': {'output': contact}}
    key = f'traces/{run_id}/bundle-test.json.gz'
    sha = objects.put(key, {'spans': [span]})
    with store.pool.connection() as c:
        c.execute("UPDATE eval_turn_traces SET status='complete',call_count=1,object_key=%s,"
                  "content_sha256=%s WHERE id=%s", (key, sha, trace['id']))
    while run_report(store, settings, objects, 'bundle-test'):
        pass
    before = store.query('SELECT id,status,attempt,result_sha256 FROM eval_jobs '
                         'WHERE experiment_id=%s ORDER BY id', (run_id,))
    exports = store.report_exports(run_id)
    monkeypatch.setattr(store, 'jobs', lambda *_: pytest.fail('no bulk full-input loading'))
    monkeypatch.setattr(store, 'report_jobs', lambda *_: pytest.fail('use frozen report archive'))
    files, manifest = _unpack_evidence(client.get(
        f'/api/agent-eval/v1/experiments/{run_id}/evidence-bundle'))
    assert manifest['logical_executions'] == 1 and manifest['completeness'] == 'complete'
    assert contact in files['cases.jsonl'].decode() and image in files['cases.jsonl'].decode()
    calls = [json.loads(line) for line in files['model_calls.jsonl'].splitlines()]
    assert len(calls) == 1 and calls[0]['span_id'] == 'saved-call-1'
    assert calls[0]['request'] == {'input': contact, 'image_url': image}
    assert b'hidden-key' not in b''.join(files.values())
    assert image in files['attachments.jsonl'].decode()
    assert store.report_exports(run_id) == exports
    assert store.query('SELECT id,status,attempt,result_sha256 FROM eval_jobs '
                       'WHERE experiment_id=%s ORDER BY id', (run_id,)) == before
    assert store.dataset(dataset['id'])['content']['items'] == [case]
    with store.pool.connection() as c:
        c.execute("UPDATE eval_turn_traces SET content_sha256='invalid' WHERE id=%s",
                  (trace['id'],))
    damaged, partial = _unpack_evidence(client.get(
        f'/api/agent-eval/v1/experiments/{run_id}/evidence-bundle'))
    assert partial['completeness'] == 'partial'
    assert partial['missing_by_reason']['object_unreadable'] == 1
    assert damaged['model_calls.jsonl'] == b''


def test_evidence_bundle_keeps_original_and_continued_executions_separate(platform):
    client, store, settings, _ = platform
    parent_id, child_id, original = _continued_execution(platform)
    store.link_execution(parent_id, child_id, 'local:developer')
    while run_one(store, settings, 'bundle-continuation'):
        pass
    while run_report(store, settings, LocalObjects(settings.object_root), 'bundle-continuation'):
        pass
    files, manifest = _unpack_evidence(client.get(
        f'/api/agent-eval/v1/experiments/{parent_id}/evidence-bundle'))
    cases = [json.loads(line) for line in files['cases.jsonl'].splitlines()]
    assert len(cases) == manifest['logical_executions'] == 1
    assert cases[0]['execution']['experiment_id'] == child_id
    assert cases[0]['judgment']['experiment_id'] == child_id
    history = [json.loads(line) for line in files['executions.jsonl'].splitlines()]
    old = next(j for j in history if j['id'] == original['id'])
    assert not old['selected_for_report'] and old['result'] == original['result']
    assert len(history) == 4  # 两次执行与各自的评分，逻辑槽仍为一个


def test_atomic_evidence_bundle_keeps_full_frozen_baseline_request(platform):
    from copy import deepcopy

    from test_candidate_model import CALL

    client, store, settings, space_id = platform
    call = deepcopy(CALL)
    text = '13800138000 jane@example.com'
    call['request']['input'][0]['content'] = text
    version = store.publish_atomic(space_id, {'name': 'Bundle atomic', 'mode': 'atomic_replay',
                                            'items': [call], 'shared': {}}, 'local:developer')
    run_id = client.post(f'/api/agent-eval/v1/spaces/{space_id}/experiments',
                        headers={'Idempotency-Key': uuid.uuid4().hex}, json={
                            'name': 'Bundle atomic', 'dataset_version_id': version['id'],
                            'execution_target': 'fixture'}).json()['id']
    while run_one(store, settings, 'bundle-atomic'):
        pass
    files, manifest = _unpack_evidence(client.get(
        f'/api/agent-eval/v1/experiments/{run_id}/evidence-bundle'))
    calls = [json.loads(line) for line in files['model_calls.jsonl'].splitlines()]
    assert len(calls) == 1 and calls[0]['kind'] == 'source_baseline'
    assert calls[0]['request'] == call['request'] and calls[0]['response'] == call['response']
    assert manifest['completeness'] == 'partial'  # 尚未生成报告归档，明确缺口而非假装完整
    assert manifest['missing_by_reason'] == {'report_archive_unavailable': 1}
    assert text in files['cases.jsonl'].decode()


def test_evidence_bundle_guards_and_purged_evidence_are_explicit(platform):
    client, store, settings, space_id = platform
    run_id = _experiment(client, space_id, _dataset(client, space_id)['id'])
    url = f'/api/agent-eval/v1/experiments/{run_id}/evidence-bundle'
    assert client.get(url).status_code == 409
    while run_one(store, settings, 'bundle-guards'):
        pass
    while run_report(store, settings, LocalObjects(settings.object_root), 'bundle-guards'):
        pass
    protected = settings.model_copy(update={'dev_identity': False})
    with TestClient(create_app(protected, store)) as anonymous:
        assert anonymous.get(url).status_code == 403
    with store.pool.connection() as c:
        c.execute("UPDATE eval_experiments SET manifest=manifest || "
                  "'{\"blind_review\":{}}'::jsonb WHERE id=%s", (run_id,))
    assert client.get(url).status_code == 403
    with store.pool.connection() as c:
        c.execute("UPDATE eval_experiments SET manifest=manifest-'blind_review',"
                  "evidence_purged_at=now() WHERE id=%s", (run_id,))
    files, manifest = _unpack_evidence(client.get(url))
    assert manifest['completeness'] == 'partial'
    missing = [json.loads(line) for line in files['missing_evidence.jsonl'].splitlines()]
    assert any(m['reason'] == 'execution_evidence_purged' for m in missing)
    case = json.loads(files['cases.jsonl'].splitlines()[0])
    assert case['execution']['input'] is None and case['judgment']['input'] is None


def test_evidence_bundle_follows_reused_job_sources(platform):
    client, store, settings, space_id = platform
    version = _dataset(client, space_id)
    source = _experiment(client, space_id, version['id'])
    while run_one(store, settings, 'bundle-source'):
        pass
    original = next(j for j in store.jobs(source) if j['stage'] == 'execute')
    response = client.post(f'/api/agent-eval/v1/spaces/{space_id}/experiments',
                           headers={'Idempotency-Key': uuid.uuid4().hex}, json={
                               'name': 'Reused bundle', 'dataset_version_id': version['id'],
                               'execution_target': 'fixture', 'reuse_experiment_id': source})
    assert response.status_code == 201, response.text
    reused = response.json()['id']
    while run_one(store, settings, 'bundle-source'):
        pass
    files, manifest = _unpack_evidence(client.get(
        f'/api/agent-eval/v1/experiments/{reused}/evidence-bundle'))
    case = json.loads(files['cases.jsonl'].splitlines()[0])
    assert manifest['logical_executions'] == 1
    assert case['execution']['source_job_id'] == original['id']
    history = [json.loads(line) for line in files['executions.jsonl'].splitlines()]
    archived = next(j for j in history if j['id'] == original['id'])
    assert not archived['selected_for_report'] and archived['result'] == original['result']


def test_new_platform_replay_freezes_stable_readback_policy(platform):
    client, store, settings, space_id = platform
    data = store.dataset(_dataset(client, space_id)["id"])["content"]
    config = {"execution_target": "platform", "candidate_model": "", "candidate_endpoint": "",
              "repeats": 1}
    manifest, tasks = engines.plan(data, config, settings, store.space(space_id),
                                   {"model": "judge-test"})
    expected = {"timeout_seconds": 90, "poll_seconds": 3,
                "min_wait_seconds": 30, "stable_seconds": 12}
    assert manifest["config"]["profile_readback"] == expected
    assert all(task[2]["config"]["profile_readback"] == expected for task in tasks)
    legacy = {**tasks[0][2], "config": {}}
    explicit = {**legacy, "config": {"profile_readback": {
        "timeout_seconds": 90, "poll_seconds": 3, "min_wait_seconds": 12}}}
    assert engines.execution_basis(legacy) == engines.execution_basis(explicit)
    assert engines.execution_basis(legacy) != engines.execution_basis(tasks[0][2])


def test_regrade_preserves_legacy_profile_observation_policy(platform):
    client, store, settings, space_id = platform
    dataset = _dataset(client, space_id)
    source = _experiment(client, space_id, dataset["id"])
    while run_one(store, settings, "legacy-readback-fixture"):
        pass
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
        headers={"Idempotency-Key": uuid.uuid4().hex}, json={
            "name": "Regrade old observation", "dataset_version_id": dataset["id"],
            "execution_target": "fixture", "repeats": 1, "reuse_experiment_id": source})
    assert response.status_code == 201, response.text
    current = store.experiment(response.json()["id"])
    assert current["manifest"]["config"]["profile_readback"] == {
        "timeout_seconds": 90, "poll_seconds": 3, "min_wait_seconds": 12}
    assert "profile_readback" not in store.experiment(source)["manifest"]["config"]


@pytest.mark.parametrize("stable", [False, True])
def test_platform_execution_uses_its_frozen_observation_policy(platform, monkeypatch, stable):
    from copy import deepcopy

    client, store, settings, space_id = platform
    run_id = _experiment(client, space_id, _dataset(client, space_id)["id"])
    job = deepcopy(next(j for j in store.jobs(run_id) if j["stage"] == "execute"))
    job["input"]["config"]["execution_target"] = "platform"
    job["input"]["turn"]["bot_user_id"] = "bot-test"
    if stable:
        job["input"]["config"]["profile_readback"] = {
            "timeout_seconds": 90, "poll_seconds": 3,
            "min_wait_seconds": 30, "stable_seconds": 12}
    monkeypatch.setattr(engines, "credentials", lambda *_: ("source-test", "target-test"))

    class PolicyCaptured(Exception):
        pass

    def capture(*_, readback, **__):
        assert readback.timeout_seconds == 90
        assert readback.min_wait_seconds == (30 if stable else 12)
        assert readback.stable_seconds == (12 if stable else None)
        raise PolicyCaptured

    monkeypatch.setattr(engines, "execute_turn", capture)
    with pytest.raises(PolicyCaptured):
        engines.execute(job, settings)


class FakeMappingGateway:
    """模拟网关按租户的模型映射：查询返回当前映射，更新按 fromModel 覆盖。"""

    def __init__(self, mappings=None):
        self.mappings = mappings or {}
        self.updates = []

    def post(self, url, json, **_kwargs):
        request = httpx.Request("POST", url)
        if url.endswith("/query"):
            return httpx.Response(200, request=request, json=[
                {"tenantId": tenant, "mappings": self.mappings[tenant]}
                for tenant in json["tenantIds"] if tenant in self.mappings])
        self.updates.append(json["updates"])
        for update in json["updates"]:
            kept = [item for item in self.mappings.get(update["tenantId"], [])
                    if item["fromModel"] != update["fromModel"]]
            self.mappings[update["tenantId"]] = kept + [
                {"fromModel": update["fromModel"], "toModel": update["toModel"]}]
        return httpx.Response(200, request=request, json={})


def _space_model(client, space_id):
    body = client.get("/api/agent-eval/v1/space-models").json()
    return next(item for item in body["items"] if item["space_id"] == space_id)


def test_space_model_is_switched_reset_and_recorded(platform, monkeypatch):
    from agent_eval import model_mapping

    client, _store, _settings, space_id = platform
    fake = FakeMappingGateway({"target-ws": [{"fromModel": "weaver", "toModel": "weaver/xxx"}]})
    monkeypatch.setattr(model_mapping.httpx, "post", fake.post)
    monkeypatch.setattr(model_mapping.gateway, "chat_models",
                        lambda _settings: [{"id": "qwen3.7-plus", "provider": ""}])
    assert _space_model(client, space_id) == {"space_id": space_id, "model": None,
                                              "other_mappings": 1, "locked": False,
                                              "shared": False, "protected": False}

    path = "/api/agent-eval/v1/space-models"
    changed = client.put(path, json={"space_ids": [space_id], "model": "qwen3.7-plus"}).json()
    assert changed["changes"][0] | {"space_id": None} == {
        "space_id": None, "tenant_id": "target-ws", "from_model": "agent-model",
        "previous_model": None, "model": "qwen3.7-plus"}
    assert changed["mismatched"] == [] and changed["unknown_model"] is False
    assert _space_model(client, space_id)["model"] == "qwen3.7-plus"
    assert {"fromModel": "weaver", "toModel": "weaver/xxx"} in fake.mappings["target-ws"]

    def broken(_settings):
        raise AttributeError("unexpected model list shape")

    monkeypatch.setattr(model_mapping.gateway, "chat_models", broken)
    odd = client.put(path, json={"space_ids": [space_id], "model": "glm-5"})
    assert odd.status_code == 200 and odd.json()["unknown_model"] is None

    reset = client.put(path, json={"space_ids": [space_id], "model": None}).json()
    assert reset["changes"][0]["model"] is None and reset["unknown_model"] is None
    assert fake.updates[-1][0]["toModel"] == "agent-model"
    assert _space_model(client, space_id)["model"] is None

    history = client.get(f"/api/agent-eval/v1/spaces/{space_id}/model-changes").json()
    assert [(row["previous_model"], row["model"]) for row in history] == [
        ("glm-5", None), ("qwen3.7-plus", "glm-5"), (None, "qwen3.7-plus")]
    assert history[0]["subject"] == "local:developer"


def test_space_model_is_locked_while_a_turn_replay_is_pending(platform, monkeypatch):
    from agent_eval import model_mapping

    client, _store, _settings, space_id = platform
    fake = FakeMappingGateway()
    monkeypatch.setattr(model_mapping.httpx, "post", fake.post)
    _experiment(client, space_id, _dataset(client, space_id)["id"])
    assert _space_model(client, space_id)["locked"] is True
    response = client.put("/api/agent-eval/v1/space-models",
                          json={"space_ids": [space_id], "model": "qwen3.7-plus"})
    assert response.status_code == 409 and "Fixture space" in response.json()["detail"]
    assert fake.updates == []


def test_space_shared_with_the_judge_tenant_switches_unless_the_judge_uses_that_model(
        platform, monkeypatch):
    """映射只替换 Agent 请求的模型；平台自己的 Judge 或草稿模型正好是它时才禁止切换。"""

    from agent_eval import model_mapping

    _client, store, settings, space_id = platform
    fake = FakeMappingGateway()
    monkeypatch.setattr(model_mapping.httpx, "post", fake.post)
    monkeypatch.setattr(model_mapping.gateway, "chat_models", lambda _settings: [])
    body = {"space_ids": [space_id], "model": "qwen3.7-plus"}
    shared = settings.model_copy(update={"internal_tenant_id": "target-ws",
                                         "judge_model": "gpt-6-luna"})
    with TestClient(create_app(shared, store)) as client:
        assert _space_model(client, space_id) | {"model": None} == {
            "space_id": space_id, "model": None, "other_mappings": 0, "locked": False,
            "shared": True, "protected": False}
        assert client.put("/api/agent-eval/v1/space-models", json=body).status_code == 200
    assert fake.mappings["target-ws"][0]["toModel"] == "qwen3.7-plus"

    risky = shared.model_copy(update={"annotation_model": "agent-model"})
    with TestClient(create_app(risky, store)) as client:
        assert _space_model(client, space_id)["protected"] is True
        response = client.put("/api/agent-eval/v1/space-models", json=body)
    assert response.status_code == 422 and "Judge" in response.json()["detail"]
    assert len(fake.updates) == 1
    assert len(store.space_model_changes(space_id)) == 1


def test_unreachable_mapping_gateway_changes_nothing(platform, monkeypatch):
    from agent_eval import model_mapping

    client, store, _settings, space_id = platform

    def down(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(model_mapping.httpx, "post", down)
    assert client.get("/api/agent-eval/v1/space-models").status_code == 502
    response = client.put("/api/agent-eval/v1/space-models",
                          json={"space_ids": [space_id], "model": "qwen3.7-plus"})
    assert response.status_code == 502 and "无法连接" in response.json()["detail"]
    assert store.space_model_changes(space_id) == []
    missing = client.put("/api/agent-eval/v1/space-models",
                         json={"space_ids": ["missing-space"], "model": "qwen3.7-plus"})
    assert missing.status_code == 404


def test_new_turn_replay_freezes_the_space_model(platform, monkeypatch):
    from agent_eval import model_mapping

    _client, store, settings, space_id = platform
    fake = FakeMappingGateway({"target-ws": [
        {"fromModel": "agent-model", "toModel": "qwen3.7-plus"}]})
    monkeypatch.setattr(model_mapping.httpx, "post", fake.post)
    judged = settings.model_copy(update={"judge_model": "judge-model"})
    with TestClient(create_app(judged, store)) as client:
        dataset_id = _dataset(client, space_id)["id"]
        response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/experiments",
                               headers={"Idempotency-Key": uuid.uuid4().hex}, json={
                                   "name": "Platform execution",
                                   "dataset_version_id": dataset_id,
                                   "execution_target": "platform"})
        assert response.status_code == 201, response.text
        run = client.get(f"/api/agent-eval/v1/experiments/{response.json()['id']}").json()
        fixture = store.experiment(_experiment(client, space_id, dataset_id))
    assert run["manifest"]["target_model"] == {"from_model": "agent-model",
                                               "model": "qwen3.7-plus"}
    assert "target_model" not in fixture["manifest"]


def test_probe_calls_the_spaces_eval_tenant_and_returns_the_result(platform, monkeypatch):
    from agent_eval import model_mapping

    client, _store, _settings, space_id = platform
    called = []

    def call(_settings, tenant):
        called.append(tenant)
        return {"ok": True, "model": "qwen3.7-plus", "reply": "pong", "input_tokens": 7,
                "output_tokens": 5, "detail": "", "latency_ms": 1200}

    monkeypatch.setattr(model_mapping, "call", call)
    response = client.post(f"/api/agent-eval/v1/spaces/{space_id}/model-probe")
    assert response.status_code == 200, response.text
    assert called == ["target-ws"] and response.json()["model"] == "qwen3.7-plus"
    assert client.post("/api/agent-eval/v1/spaces/missing-space/model-probe").status_code == 404


def test_annotation_validation_detail_survives_worker_database_and_api(platform, monkeypatch):
    from agent_eval import annotation
    from golden_eval.application.profile_enrichment import ProfileEnrichmentError

    client, store, settings, space_id = platform
    draft = store.create_draft(space_id, {
        "name": "Validation detail", "mode": "golden_regression",
        "items": [session_payload()], "shared": shared_payload()}, "local:developer")
    profile, rubric = annotation.frozen_prompts()
    store.create_annotation(draft["id"], "GD-FIXTURE-0001", profile, rubric,
                            "test-model", uuid.uuid4().hex, "local:developer")
    error = annotation.AnnotationError(
        "profile", ProfileEnrichmentError("模型返回 1 个 Turn 的结果，工单是 6 个，无法一一对应"))

    def fail(*_args):
        raise error

    monkeypatch.setattr(annotation, "generate", fail)
    assert run_annotation(store, settings, "test")
    row = store.annotation_jobs(draft["id"])[0]
    assert row["error_detail"] == error.detail
    response = client.get(f"/api/agent-eval/v1/drafts/{draft['id']}/annotations")
    assert response.status_code == 200
    view = response.json()[0]
    assert view["error_code"] == "profile_validation_failed"
    assert view["error_detail"]["message"] == str(error)
    assert "profile_prompt" not in view
