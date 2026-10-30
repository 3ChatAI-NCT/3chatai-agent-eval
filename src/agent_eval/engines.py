"""两种执行模式共享调度，保留各自已验证的领域与评分合同。"""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import monotonic

import httpx
from pydantic import SecretStr

from atomic_eval import pairwise
from atomic_eval.config import ModelEndpoint
from atomic_eval.models import ModelCall
from atomic_eval.pairwise import JudgeError
from atomic_eval.replay import HttpResponsesClient, blocked_reason, swap_model
from atomic_eval.responses import to_output
from atomic_eval.sampling import to_case
from golden_eval.adapters.jsonl_corpus import JsonlCorpusStore
from golden_eval.adapters.platform_source import PlatformCorpusSource
from golden_eval.adapters.target_contacts import FixtureTargetContact, PlatformTargetContact
from golden_eval.application.judge_model import judge_batch
from golden_eval.application.prompt_registry import load_builtin_prompts
from golden_eval.application.turn_execution import (
    ROUTE_PLACEHOLDERS,
    ProfileReadbackPolicy,
    execute_turn,
)
from golden_eval.application.turn_projection import build_turn_tasks
from golden_eval.domain.models import (
    CreateRunRequest,
    GoldenSession,
    GoldenShared,
    PromptKind,
    RunIdentity,
    RunSummary,
    TurnObservation,
    TurnTask,
)
from golden_eval.integrations.platform_http import PlatformHTTPError, UrllibTransport
from golden_eval.integrations.structured_model import (
    OpenAICompatibleStructuredModel,
    StructuredModelConfig,
)

from . import gateway, vault
from .models import Mode, digest
from .registry import credentials

TARGET_KEYS = ("space", "candidate_endpoint", "candidate_model")


class ExecutionError(RuntimeError):
    def __init__(self, code, *, uncertain=False, result=None):
        self.code = code
        self.uncertain = uncertain
        self.result = result  # 失败时已有的执行证据，随失败状态一起保存
        super().__init__(code)


@lru_cache(maxsize=1)
def engine_fingerprint():
    root = Path(__file__).resolve().parents[2]
    paths = [p for package in ("agent_eval", "atomic_eval", "golden_eval")
             for p in (root / "src" / package).rglob("*") if p.suffix in (".py", ".md")]
    paths += list((root / "prompts").rglob("*.md"))
    material = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(paths)}
    return digest(material)


def validate_dataset(data):
    if data["mode"] == Mode.ATOMIC:
        calls = [ModelCall.model_validate(item) for item in data["items"]]
        keys = [call.call_id for call in calls]
        for call in calls:
            if not call.request or not call.response:
                raise ValueError("原子样本必须包含完整 request 和 baseline response")
            if blocked_reason(call.request):
                raise ValueError(f"原子样本不可等价回放: {call.call_id}")
            to_case(call)
    else:
        shared = GoldenShared.model_validate(data["shared"])
        sessions = [GoldenSession.model_validate(item) for item in data["items"]]
        keys = [s.session_id for s in sessions]
        turn_keys = [turn.turn_id for s in sessions for turn in s.turns]
        if len(set(turn_keys)) != len(turn_keys):
            raise ValueError("turn_id 必须全局唯一")
        for session in sessions:
            JsonlCorpusStore._validate_shared_contract(shared, session)
            if not session.turns or any(not t.rubric.strip() for t in session.turns):
                raise ValueError("金标会话必须包含可评估 turn 和 rubric")
    if len(keys) != len(set(keys)):
        raise ValueError("样本 ID 重复")


def plan(data, request, settings, space, judge):
    """judge 为创建时生效的全局 Judge 配置，随 manifest 冻结。"""

    validate_dataset(data)
    mode = data["mode"]
    fixture = request["execution_target"] == "fixture"
    if fixture and settings.environment != "development":
        raise ValueError("Fixture 只允许在开发环境执行")
    if not fixture and not judge["model"]:
        raise ValueError("请先在设置页配置 Judge 模型")
    if mode == Mode.ATOMIC:
        if not fixture and not (request["candidate_model"] and request["candidate_endpoint"]):
            raise ValueError("请选择候选模型：网关模型、已保存的接口，或临时填写接口地址与 key")
    else:
        if request["candidate_model"] or request["candidate_endpoint"]:
            raise ValueError("逐轮回放使用 EVAL 空间当前 Agent 配置，无需填写候选模型")
        if request["repeats"] > 5:
            raise ValueError("当前金标合同最多支持 5 次执行")
        request["space"] = {key: space[key] for key in
                            ("id", "source_workspace_id", "target_workspace_id")}
        if not fixture:
            request.setdefault("profile_readback", {
                "timeout_seconds": 90, "poll_seconds": 3,
                "min_wait_seconds": 30, "stable_seconds": 12})
    selected = request.get("sample_scope")
    if selected is not None:
        if mode != Mode.GOLDEN or not selected or len(selected) != len(set(selected)):
            raise ValueError("仅金标评测可指定非空且不重复的轮次范围")
        known = {turn["turn_id"] for item in data["items"] for turn in item["turns"]}
        if not set(selected) <= known:
            raise ValueError("指定轮次不存在于数据集")
    tasks = []
    for repeat_index in range(1, request["repeats"] + 1):
        for item in data["items"]:
            payload = {"mode": mode, "item": item, "shared": data["shared"],
                       "config": request}
            if mode == Mode.ATOMIC:
                tasks.append((item["call_id"], repeat_index, payload))
            else:
                session = GoldenSession.model_validate(item)
                routing = data["shared"]["dataset"].get("source_config", {}).get(
                    "session_routing", {}
                ).get(session.session_id, {})
                for turn in build_turn_tasks(run_id="pending", session=session,
                        repeat_index=repeat_index, account_prefix="eval",
                        bot_user_id=routing.get("bot_user_id", ""),
                        im_bot_id=routing.get("im_bot_id", "")):
                    if selected is not None and turn.turn_id not in selected:
                        continue
                    tasks.append((turn.turn_id, repeat_index,
                                  {**payload, "turn": turn.model_dump(mode="json")}))
    if mode == Mode.ATOMIC:
        frozen = pairwise.prompt()
        prompt_fields = {"atomic_prompt": frozen, "judge_prompt_version": frozen["version"],
                         "judge_prompt_sha256": frozen["sha256"]}
    else:
        builtins = load_builtin_prompts(Path(__file__).resolve().parents[2] / "prompts")
        prompt = builtins[PromptKind.JUDGE]
        prompt_fields = {"judge_prompt_version": prompt.version_label,
                         "judge_prompt_sha256": prompt.body_sha256,
                         "judge_prompt": prompt.version(is_active=True).model_dump(mode="json"),
                         "rubric_prompt": builtins[PromptKind.RUBRIC_DRAFT].version(
                             is_active=True).model_dump(mode="json")}
    manifest = {"mode": mode, "config": request, "engine_sha256": engine_fingerprint(),
                "judge_model": judge["model"], "judge": judge,
                "judge_policy": "symmetric-all-v1",
                **prompt_fields,
                "target_config_sha256": digest({key: request.get(key) for key in TARGET_KEYS}),
                "dataset_sha256": digest(data), "schema_version": "experiment-v1"}
    return manifest, tasks


def execution_basis(payload: dict) -> str:
    """Fields that can affect a Golden turn before judging starts."""

    turn = payload["turn"]
    return digest({
        "messages": turn["messages"], "profile_before": turn["profile_before"],
        "bot_user_id": turn["bot_user_id"], "im_bot_id": turn["im_bot_id"],
        "session_id": turn["session_id"], "turn_id": turn["turn_id"],
        "seq": turn["seq"], "repeat_index": turn["repeat_index"],
        "profile_keys": sorted(field["key"] for field in
                               payload["shared"]["context"]["profile_schema"]),
        "profile_readback": {key: float(value) if value is not None else None
            for key, value in asdict(ProfileReadbackPolicy(
                **payload.get("config", {}).get("profile_readback", {}))).items()},
    })


def frozen_judge(manifest):
    """评测创建时冻结的 Judge 配置；早期评测只记录了模型名，其余取当时的固定值。"""

    return manifest.get("judge") or {"model": manifest["judge_model"], "timeout_seconds": 180,
                                     "format_attempts": 3}


class HttpJudge:
    def __init__(self, settings, judge):
        self.model = judge["model"]
        self.timeout = judge["timeout_seconds"]
        self.settings = settings

    def complete(self, system_prompt, payload):
        response = httpx.post(self.settings.model_gateway_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + gateway.api_key(self.settings)},
            json=gateway.judge_body(self.model, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]),
            timeout=self.timeout, follow_redirects=False)
        # 超时与 HTTP 错误原样抛出：429 与 5xx 由 Worker 重试，4xx（模型名、key 等配置问题）
        # 按状态码记录；只有响应体不符合协议才算输出问题，交给格式重试
        response.raise_for_status()
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise JudgeError("judge_response_malformed") from exc


def probe_candidate(base_url: str, model: str, key: str) -> dict:
    endpoint = ModelEndpoint(base_url=base_url, model=model, api_key=SecretStr(key))
    started = monotonic()
    try:
        response = HttpResponsesClient(endpoint, timeout=30).create(
            {"model": model, "input": "ping", "max_output_tokens": 16})
        ok = response.get("status") in (None, "completed", "incomplete")
        detail = "" if ok else f"接口返回状态 {response.get('status')}"
    except (httpx.HTTPError, ValueError) as exc:
        ok, detail = False, gateway.failure_reason(exc)
    return {"ok": ok, "latency_ms": round((monotonic() - started) * 1000), "detail": detail}


def golden_prompt():
    builtin = load_builtin_prompts(Path(__file__).resolve().parents[2] / "prompts")
    return builtin[PromptKind.JUDGE].version(is_active=True)


def golden_run(job):
    config = job["input"]["config"]
    now = datetime.now(UTC)
    return RunSummary(run_id=job["experiment_id"], kind="evaluate", status="running",
        created_at=now, updated_at=now,
        request=CreateRunRequest(kind="evaluate", dataset_id=config["dataset_version_id"],
            target_connection_id=config["space"]["target_workspace_id"],
            execution_target=config["execution_target"], repeats=config["repeats"]))


def trace_workspace(job):
    """需要登记模型调用链路时返回 EVAL 空间 ID：只有真实平台上的逐轮回放执行会调用被测 Agent。"""

    data = job["input"]
    if (job["stage"] != "execute" or data["mode"] != Mode.GOLDEN
            or data["config"]["execution_target"] == "fixture"):
        return None
    return data["config"]["space"]["target_workspace_id"]


def execute(job, settings, candidate_key=None, replay_request_id=None):
    data = job["input"]
    config = data["config"]
    fixture = config["execution_target"] == "fixture"
    if data["mode"] == Mode.ATOMIC:
        call = ModelCall.model_validate(data["item"])
        if blocked_reason(call.request):
            raise ExecutionError("request_not_replayable")
        body, changed = swap_model(call.request, config["candidate_model"])
        if fixture:
            # Fixture 原样返回 baseline，仅验证管线，报告明确标记，不代表模型效果。
            response = call.response
        else:
            if config.get("candidate_source") == "gateway":
                # 网关已接入的模型用平台自己的网关 key 调用，与生产请求走同一条链路
                key = gateway.api_key(settings)
            elif candidate_key:
                key = vault.unseal(settings, candidate_key)
            else:
                raise ValueError("候选模型 API Key 已清除或未保存，请重新创建评测")
            endpoint = ModelEndpoint(base_url=config["candidate_endpoint"],
                                     model=config["candidate_model"], api_key=SecretStr(key))
            started = monotonic()
            response = HttpResponsesClient(endpoint).create(body)
            elapsed = round((monotonic() - started) * 1000)
        candidate = to_output(response, "candidate")
        case = to_case(call)
        usage = response.get("usage") or {}
        result = {"baseline": case.outputs[0].model_dump(mode="json"),
                  "candidate": candidate.model_dump(mode="json"),
                  "context": case.current_turn, "request_sha256": digest(body),
                  "changed_fields": changed, "fixture": fixture,
                  # 线上耗时来自生产网关日志，候选耗时在 Worker 侧测得，网络环境不同
                  "performance": {
                      "baseline": {"elapsed_ms": call.elapsed_ms,
                                   "input_tokens": call.prompt_tokens,
                                   "output_tokens": call.completion_tokens},
                      "candidate": {"elapsed_ms": None if fixture else elapsed,
                                    "input_tokens": usage.get("input_tokens"),
                                    "output_tokens": usage.get("output_tokens")}}}
        return result, {**data, "response": response}
    turn = TurnTask.model_validate(data["turn"])
    turn = turn.model_copy(update={"run_id": job["experiment_id"],
                                  "account_id": "eval-" + job["id"]})
    routing_evidence = None
    if fixture:
        target = FixtureTargetContact()
    else:
        source_token, token = credentials(settings, config["space"])
        if all(value.strip().lower() in ROUTE_PLACEHOLDERS
               for value in (turn.bot_user_id, turn.im_bot_id)):
            try:
                routing_evidence = PlatformCorpusSource(base_url=settings.platform_base_url,
                    identity=RunIdentity(tenant_id=config["space"]["source_workspace_id"],
                                         source="connection_settings"),
                    token=source_token, persist_attachments=False).routing_evidence(turn.session_id)
            except (PlatformHTTPError, ValueError) as exc:
                raise ExecutionError("source_routing_unavailable") from exc
        target = PlatformTargetContact(base_url=settings.platform_base_url,
            identity=RunIdentity(tenant_id=config["space"]["target_workspace_id"], staff_id="",
                                 source="connection_settings"),
            token=token, transport=UrllibTransport(
                timeout_seconds=settings.platform_http_timeout_seconds))
    observation = execute_turn(turn, target=target, execution_target=config["execution_target"],
        profile_keys=[f["key"] for f in data["shared"]["context"]["profile_schema"]],
        readback=ProfileReadbackPolicy(0, 0, 0) if fixture else ProfileReadbackPolicy(
            **config.get("profile_readback", {})),
        replay_request_id=replay_request_id, routing_evidence=routing_evidence)
    result = {"baseline": [m.model_dump(mode="json") for m in turn.expected.messages],
              "candidate": [m.model_dump(mode="json") for m in observation.response_messages],
              "context": [m.model_dump(mode="json") for m in turn.messages],
              "profile_before": observation.profile_before,
              "profile_after": observation.profile_after, "fixture": fixture,
              "replay_request_id": observation.replay_request_id,
              "replay_request_id_echoed": observation.replay_request_id_echoed}
    if observation.status != "completed":
        # 失败的 Turn 不进 Judge，完整观测（走到哪一步、发出的请求、错误）只能留在执行结果里
        raise ExecutionError(observation.error_code or "execution_error",
                             uncertain=observation.status == "delivery_unknown",
                             result={**result, "observation": observation.model_dump(mode="json")})
    return result, {**data, "observation": observation.model_dump(mode="json")}


def grade(job, settings, manifest=None):
    data = job["input"]
    if data["config"]["execution_target"] == "fixture":
        return {"status": "not_evaluated", "reason": "Fixture 只验证执行管线，不生成质量分数"}
    if data["mode"] == Mode.ATOMIC:
        call = ModelCall.model_validate(data["item"])
        case = to_case(call)
        case = case.model_copy(update={"outputs": [*case.outputs,
                                                  to_output(data["response"], "candidate")]})
        judge = frozen_judge(manifest)
        return pairwise.grade_pair(case, call.request, HttpJudge(settings, judge),
                                   seed=f"{job['experiment_id']}:{call.call_id}",
                                   system_prompt=manifest["atomic_prompt"]["text"],
                                   format_attempts=judge["format_attempts"])
    from . import workspace_policy

    judge_model = frozen_judge(manifest)["model"]
    model = OpenAICompatibleStructuredModel(StructuredModelConfig(provider="gateway",
        model=judge_model, base_url=settings.model_gateway_url,
        api_key=gateway.api_key(settings), max_retries=0,
        extra_body=gateway.judge_extras(judge_model)))
    session = GoldenSession.model_validate(data["item"])
    from golden_eval.domain.models import PromptVersion

    frozen_prompt = (PromptVersion.model_validate(manifest["judge_prompt"])
                     if manifest and manifest.get("judge_prompt") else golden_prompt())
    if frozen_prompt.version_label == "full-evaluation-v14":
        if not data.get("judge_rubric"):
            raise ExecutionError("judge_rubric_unavailable")
        session = session.model_copy(update={"turns": [
            turn.model_copy(update={"rubric": data["judge_rubric"]["rubric"]})
            if turn.turn_id == job["case_id"] else turn for turn in session.turns]})
    feedback = None
    usage = {}
    for _ in range(frozen_judge(manifest)["format_attempts"]):
        outcome = judge_batch(run=golden_run(job), sessions={session.session_id: session},
            pending=[TurnObservation.model_validate(data["observation"])], prompt=frozen_prompt,
            model=model, shared=workspace_policy.judging_shared(
                data["shared"], manifest.get("workspace_policy")),
            validation_feedback=feedback,
            execution_evidence={data["observation"]["observation_id"]: data["judge_evidence"]}
            if "judge_evidence" in data else None)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(outcome.usage.get(key), (int, float)):
                usage[key] = usage.get(key, 0) + outcome.usage[key]
        if not outcome.errors and outcome.judged:
            return {"turns": [r.model_dump(mode="json") for r in outcome.judged],
                    "judge_input_format": outcome.input_format,
                    "usage": {**outcome.usage, **usage},
                    **({"judge_rubric": data["judge_rubric"]}
                       if data.get("judge_rubric") else {}),
                    **({"workspace_policy": {
                        key: value for key, value in manifest["workspace_policy"].items()
                        if key != "context"}} if manifest.get("workspace_policy") else {}),
                    **({"judge_evidence": {
                        **{key: value for key, value in data["judge_evidence"].items()
                           if key != "tool_results"},
                        "tool_results_count": len(data["judge_evidence"].get("tool_results", [])),
                    }} if "judge_evidence" in data else {})}
        # 只修复输出合同，不放宽证据引用；冻结观测不变，也不重新执行 Agent。
        feedback = outcome.errors
        if any(error["code"] in {"model_timeout", "model_network", "model_not_configured"}
               or error["code"].startswith("model_http_") for error in feedback):
            raise ExecutionError(feedback[0]["code"])
    raise ExecutionError("judge_result_invalid", result={
        "status": "not_evaluated", "judge_input_format": outcome.input_format,
        "validation_errors": [{key: error[key] for key in (
            "scope", "code", "constraint", "turn_id", "repeat_index") if key in error}
            for error in outcome.errors],
    })
