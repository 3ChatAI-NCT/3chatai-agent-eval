import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from test_jsonl_corpus import shared_payload
from test_turn_execution import FakePlatform, turn_task

from agent_eval import engines
from agent_eval.config import Settings
from golden_eval.adapters.target_contacts import CHAT_MESSAGES
from golden_eval.application.turn_execution import ProfileReadbackPolicy, execute_turn


@pytest.mark.parametrize("configured,expected", [(None, 180), (240, 240)])
def test_golden_execution_waits_for_slow_sync_reply(monkeypatch, configured, expected):
    platform = FakePlatform()
    chat_timeouts = []

    def urlopen(request, timeout):
        if request.full_url.endswith(CHAT_MESSAGES):
            chat_timeouts.append(timeout)
            # 模拟超过旧 45 秒上限、但在逐轮执行预算内完成的对话。
            if timeout < 60:
                raise TimeoutError("slow synchronous reply")
        headers = {key.lower(): value for key, value in request.header_items()}
        echoed = {}
        if request.data:
            body = platform.post(request.full_url, headers=headers,
                                 payload=json.loads(request.data), response_headers=echoed)
        else:
            params = {key: value[0] for key, value in
                      parse_qs(urlsplit(request.full_url).query).items()}
            body = platform.get(request.full_url, headers=headers, params=params)
        response = io.BytesIO(json.dumps(body).encode())
        response.headers = echoed
        return response

    def execute_without_wait(task, **kwargs):
        return execute_turn(task, **{**kwargs, "readback": ProfileReadbackPolicy(0, 0, 0)})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(engines, "credentials", lambda *_: (None, "test-token"))
    monkeypatch.setattr(engines, "execute_turn", execute_without_wait)
    settings = Settings(database_url="postgresql://unused/test", environment="development",
                        dev_identity=True, platform_base_url="https://platform.example.com",
                        **({"platform_http_timeout_seconds": configured} if configured else {}))
    job = {"id": "job_test", "experiment_id": "exp_test", "input": {
        "mode": "golden_regression", "turn": turn_task(
            bot_user_id="bot-1", im_bot_id="im-1").model_dump(mode="json"),
        "shared": shared_payload(), "config": {"execution_target": "platform", "space": {
            "target_workspace_id": "target", "source_workspace_id": "source"}},
    }}
    result, _ = engines.execute(job, settings, replay_request_id="turn-request")
    assert result["candidate"]
    assert result["replay_request_id_echoed"] == "turn-request"
    assert chat_timeouts == [expected]


def test_engine_rechecks_missing_source_route_without_changing_frozen_turn(monkeypatch):
    from copy import deepcopy

    from golden_eval.adapters.platform_source import PlatformCorpusSource

    task = turn_task()
    proof = {"session_id": task.session_id, "channel_type": "openApi",
             "visitor_found": True, "bot_user_id": None, "im_bot_id": None}
    seen = []
    monkeypatch.setattr(engines, "credentials", lambda *_: ("source-token", "target-token"))
    monkeypatch.setattr(PlatformCorpusSource, "routing_evidence", lambda self, sid: (
        seen.append((self._identity.tenant_id, self._token, sid)) or proof))

    def observe(turn, **kwargs):
        assert kwargs["routing_evidence"] == proof
        return execute_turn(turn, target=engines.FixtureTargetContact(),
            execution_target="fixture", readback=ProfileReadbackPolicy(0, 0, 0))

    monkeypatch.setattr(engines, "execute_turn", observe)
    job = {"id": "job_test", "experiment_id": "exp_test", "input": {
        "mode": "golden_regression", "turn": task.model_dump(mode="json"),
        "shared": shared_payload(), "config": {"execution_target": "platform", "space": {
            "target_workspace_id": "target", "source_workspace_id": "source"}},
    }}
    original = deepcopy(job)
    result, _ = engines.execute(job, Settings(database_url="postgresql://unused/test",
                                            environment="development", dev_identity=True))
    assert result["candidate"] and job == original
    assert seen == [("source", "source-token", task.session_id)]
