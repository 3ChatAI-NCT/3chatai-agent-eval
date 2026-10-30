"""创建评测：页面提交与批量评测自动开跑共用同一套校验、候选解析与冻结逻辑。"""

from copy import deepcopy

import httpx
from pydantic import ValidationError

from . import engines, gateway, model_mapping, vault
from .models import ExperimentIn, Mode, ReportConfig, digest

CANDIDATE_INPUTS = {"candidate", "candidate_preset_id", "candidate_gateway_model"}


class RunRejected(ValueError):
    """不能创建评测的业务原因；status 对应 HTTP 状态码，批量评测里原样记为失败原因。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def current_report(store) -> dict:
    saved = store.setting_revisions("report", 1)
    value = saved[0]["value"] if saved else {}
    # 早期保存的只有报告页默认开关，已由强制盲评取代，读取时忽略
    return ReportConfig.model_validate(
        {key: item for key, item in value.items() if key in ReportConfig.model_fields}
    ).model_dump()


def current_judge(store, settings) -> dict:
    """设置页保存的最新版本；从未保存过时沿用部署时的环境变量。"""

    saved = store.setting_revisions("judge", 1)
    if saved:
        return saved[0]["value"]
    return {"model": settings.judge_model, "timeout_seconds": 180, "format_attempts": 3}


def check_gateway_model(settings, model: str) -> None:
    try:
        known = {item["id"] for item in gateway.chat_models(settings)}
    except (httpx.HTTPError, ValueError):
        return  # 读不到列表时不阻塞创建，调用失败会按状态码记录
    if model not in known:
        raise ValueError(f"3Chat 网关没有可用的模型 {model}")


def resolve_candidate(store, settings, body: ExperimentIn,
                      source_config: dict) -> tuple[dict, str | None]:
    """确定候选模型来源；key 不进评测配置、任务输入和请求指纹，只加密存到评测结束。"""

    if body.candidate_gateway_model:
        model = body.candidate_gateway_model
        check_gateway_model(settings, model)
        return {"candidate_source": "gateway", "candidate_model": model,
                "candidate_endpoint": settings.model_gateway_url.rstrip("/"),
                "candidate_preset": None}, None
    if body.candidate_preset_id:
        preset = store.candidate_preset(body.candidate_preset_id)
        # 预设的密文直接复制给本次评测：之后改 key 或删除预设都不影响已创建的评测
        return {"candidate_source": "preset", "candidate_model": preset["model"],
                "candidate_endpoint": preset["base_url"],
                "candidate_preset": {"id": preset["id"], "name": preset["name"]}}, \
            preset["sealed_key"]
    if body.candidate:
        return {"candidate_source": "manual", "candidate_model": body.candidate.model,
                "candidate_endpoint": body.candidate.base_url, "candidate_preset": None}, \
            vault.seal(settings, body.candidate.api_key.get_secret_value())
    # 重新判分不再调用候选模型，沿用来源评测的候选配置
    return {key: source_config.get(key, "") for key in
            ("candidate_source", "candidate_model", "candidate_endpoint")} | {
                "candidate_preset": source_config.get("candidate_preset")}, None


def create(store, settings, space_id: str, body: ExperimentIn, key: str,
           subject: str) -> dict:
    space = store.space(space_id)
    if space["status"] != "active":
        raise RunRejected(409, "评测空间尚未验证可用")
    dataset = store.dataset(body.dataset_version_id)
    if dataset["content_purged_at"]:
        raise RunRejected(410, "数据集内容已按保留期限清理")
    if dataset["space_id"] != space_id:
        raise RunRejected(422, "数据集与评测空间不匹配")
    request_data = body.model_dump(mode="json", exclude=CANDIDATE_INPUTS)
    source_run = store.experiment(body.reuse_experiment_id) if body.reuse_experiment_id else None
    if source_run and dataset["mode"] == Mode.GOLDEN:
        # 重新判分沿用原观测口径；旧配置缺省代表固定90秒，不能标成新稳定窗口策略。
        request_data["profile_readback"] = source_run["manifest"]["config"].get(
            "profile_readback", {"timeout_seconds": 90, "poll_seconds": 3,
                                 "min_wait_seconds": 12})
    try:
        candidate, sealed_key = resolve_candidate(
            store, settings, body, source_run["manifest"]["config"] if source_run else {})
        request_data.update(candidate)
        manifest, tasks = engines.plan(dataset["content"], request_data, settings, space,
                                       current_judge(store, settings))
    except (ValidationError, ValueError) as exc:
        raise RunRejected(422, str(exc)[:300]) from exc
    if source_run:
        # 重新判分复用原执行结果，执行模型也沿用原评测的冻结值
        if "target_model" in source_run["manifest"]:
            manifest["target_model"] = source_run["manifest"]["target_model"]
    elif dataset["mode"] == Mode.GOLDEN and request_data["execution_target"] != "fixture":
        manifest["target_model"] = model_mapping.snapshot(settings, space)
    generated_reference = (dataset["source_snapshot_id"]
                           and dataset["content"]["shared"].get("context", {}).get(
                               "reference_status") == "generated")
    if dataset["mode"] == Mode.GOLDEN and not dataset["review_state"] and not generated_reference:
        raise RunRejected(422, "金标数据集没有审核记录")
    review = current_report(store)
    # Fixture 不产生质量结论，无需盲评
    if dataset["mode"] == Mode.ATOMIC and review.pop("blind_review") and (
            request_data["execution_target"] != "fixture"):
        manifest["blind_review"] = review
    if source_run:
        run_id = store.submit_regrade(space, request_data, key, subject, manifest, tasks,
                                      source_run, store.jobs(source_run["id"]))
    else:
        run_id = store.submit(space, request_data, key, subject, manifest, tasks, sealed_key)
    return {"id": run_id, "planned_results": len(tasks)}


def batch_replay_sources(store, batch_id):
    batch, items = store.batch(batch_id)
    if not items:
        raise RunRejected(409, "原批次没有可复用的采样数据")
    sources = []
    for item in items:
        if not item["experiment_id"] or item["experiment_status"] not in (
                "completed", "failed", "cancelled", "uncertain"):
            raise RunRejected(409, f"{item['space_name']} 尚未结束或没有来源评测，不能整批复用")
        run = store.experiment(item["experiment_id"])
        space = store.space(item["space_id"])
        dataset = store.dataset(run["dataset_version_id"])
        if space["status"] != "active":
            raise RunRejected(409, f"{item['space_name']} 尚未验证可用")
        if dataset["content_purged_at"]:
            raise RunRejected(410, f"{item['space_name']} 的采样数据已清理，不能完整复用")
        if (run["mode"] != Mode.ATOMIC or dataset["space_id"] != space["id"]
                or dataset["id"] != item["dataset_version_id"]
                or digest(dataset["content"]) != run["manifest"]["dataset_sha256"]
                or not run["manifest"].get("atomic_prompt")):
            raise RunRejected(409, f"{item['space_name']} 的冻结数据或评分依据不完整")
        sources.append((item, run, space, dataset))
    return batch, sources


def batch_replay_preview(store, batch_id):
    batch, sources = batch_replay_sources(store, batch_id)
    items = []
    for item, run, _space, dataset in sources:
        manifest = run["manifest"]
        items.append({"space_name": item["space_name"], "source_name": item["source_name"],
                      "dataset_version_id": dataset["id"],
                      "dataset_sha256": manifest["dataset_sha256"],
                      "samples": len(dataset["content"]["items"]),
                      "repeats": manifest["config"]["repeats"],
                      "judge_model": manifest["judge_model"],
                      "judge_prompt_version": manifest["judge_prompt_version"]})
    return {"source_batch_id": batch_id, "source_batch_name": batch["name"], "items": items}


def replay_batch(store, settings, batch_id, body, key, subject):
    batch, sources = batch_replay_sources(store, batch_id)
    choice = body.model_dump(exclude={"name"}, exclude_none=True)
    try:
        candidate, sealed_key = resolve_candidate(store, settings, ExperimentIn(
            name=body.name, dataset_version_id=sources[0][3]["id"], **choice), {})
    except ValueError as exc:
        if type(exc) is not ValueError:
            raise
        raise RunRejected(422, str(exc)) from exc
    prepared = []
    for item, run, space, dataset in sources:
        original = run["manifest"]
        request = deepcopy(original["config"])
        request.update(candidate)
        request.update(name=f"{body.name} · {space['name']}", reuse_experiment_id=None)
        try:
            manifest, tasks = engines.plan(dataset["content"], request, settings, space,
                                           engines.frozen_judge(original))
        except ValueError as exc:
            raise RunRejected(422, str(exc)[:300]) from exc
        # 复用评分依据、样本与生产基线；候选回答、判分结果和人工结论不沿用。
        for field in ("judge_policy", "atomic_prompt", "judge_prompt_version",
                      "judge_prompt_sha256", "blind_review"):
            if field in original:
                manifest[field] = deepcopy(original[field])
        manifest["replayed_from_experiment_id"] = run["id"]
        prepared.append((item, space, request, manifest, tasks, sealed_key))
    spec = {key: value for key, value in batch["spec"].items()
            if not key.startswith("candidate_")}
    spec.update(choice, source_batch_id=batch_id, source_batch_name=batch["name"])
    if candidate["candidate_preset"]:
        spec["candidate_preset_name"] = candidate["candidate_preset"]["name"]
    return {"id": store.create_replay_batch(body.name, spec, prepared, key, subject)}
