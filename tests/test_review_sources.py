import pytest
from test_platform_integration import _atomic_fixture_run, platform  # noqa: F401

from agent_eval.worker import run_one

pytestmark = pytest.mark.postgres


def test_assisted_review_does_not_complete_human_review(platform):  # noqa: F811
    client, store, settings, space_id = platform
    run_id = _atomic_fixture_run(client, store, space_id)
    while run_one(store, settings, "test"):
        pass
    base = f"/api/agent-eval/v1/experiments/{run_id}"
    job = next(j for j in client.get(base + "/results").json() if j["stage"] == "judge")
    url = base + f"/results/{job['id']}/review"
    body = {"label": "unavailable", "issue_kind": "judge_error", "note": "AI辅助，待业务确认",
            "outputs_sha256": job["result_sha256"], "actor_type": "ai_assisted"}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    result = client.get(base + "/report").json()
    assert result["reviewed"] == 0
    assert result["assisted_review"]["count"] == 1
    assert result["human_review"]["labels"] == {}
    assert client.post(base + "/review/complete").status_code == 409
    assert client.post(url, json={**body, "actor_type": "human", "revision": 1}).status_code == 200
    assert client.post(url, json={**body, "revision": 2}).status_code == 200
    result = client.get(base + "/report").json()
    assert result["reviewed"] == 1
    displayed = next(j for j in client.get(base + "/results").json() if j["id"] == job["id"])
    assert displayed["review"]["actor_type"] == "human"
    assert displayed["review_revision"] == 3
    assert client.post(base + "/review/complete").status_code == 200
