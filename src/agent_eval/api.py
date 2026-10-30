import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError

from atomic_eval import pairwise
from atomic_eval.checks import PROFILE_TOOL_PREFIX
from atomic_eval.config import ConfigError
from atomic_eval.models import ModelCall
from atomic_eval.responses import has_image, tool_rules
from atomic_eval.sampling import to_case
from atomic_eval.sls import CaptureError, SlsRecordSource
from golden_eval.domain.corpus import CollectionPolicy
from golden_eval.domain.models import BuildDraftFromSnapshotRequest, GoldenSession, GoldenShared

from . import (
    annotation,
    blind,
    capture,
    engines,
    evidence_bundle,
    gateway,
    media,
    model_mapping,
    profile_labels,
    registry,
    response_format,
    runs,
    traces,
    vault,
)
from .auth import Principal, authenticate
from .config import Settings
from .models import (
    ArchiveIn,
    AtomicCaptureIn,
    BatchIn,
    BatchReplayIn,
    BlindReviewIn,
    CandidatePresetIn,
    CandidateProbeIn,
    DatasetIn,
    ExperimentIn,
    GoldenCaptureIn,
    GoldenCasePatch,
    JudgeConfig,
    JudgeConfigIn,
    Mode,
    ReportConfigIn,
    RevealIn,
    ReviewIn,
    SpaceModelIn,
    digest,
)
from .objects import create_objects
from .privacy import safe_view
from .reporting import cross_summary as build_cross_summary
from .reporting import current_reviews
from .reporting import latest as latest_reviews
from .reporting import report as build_report
from .store import Conflict, NotFound, Store

logger = logging.getLogger(__name__)

def blind_guard(run: dict) -> None:
    if run["blind_status"] in blind.PENDING:
        raise HTTPException(403, "盲评揭晓前不能查看 AI 评审与双方身份")


def summary(row: dict):
    return {key: jsonable_encoder(safe_view(value)) for key, value in row.items()
            if key not in {"content", "input", "result", "manifest", "review_state",
                           "object_key", "spec", "profile_prompt", "rubric_prompt",
                           "candidate_api_key"}}


def capture_summary(row: dict):
    """采集任务只公开时间窗与目标数量，其余请求参数仍不出服务端。"""

    spec = row["spec"]
    policy = spec.get("policy") or spec
    keys = ("start", "end", "size", "model", "quota_per_workspace", "min_turns",
            "max_scanned_sessions")
    request = {key: policy[key] for key in keys if policy.get(key) is not None}
    return summary(row) | {"request": request}


def annotation_summary(row: dict):
    return summary(row) | {
        "profile_prompt_version": row["profile_prompt"]["version_label"],
        "profile_prompt_sha256": row["profile_prompt"]["content_sha256"],
        "rubric_prompt_version": row["rubric_prompt"]["version_label"],
        "rubric_prompt_sha256": row["rubric_prompt"]["content_sha256"],
    }


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_store = store or Store(resolved_settings.database_url.get_secret_value())
    objects = create_objects(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if store is None:
            resolved_store.open()
            if not resolved_store.ready():
                raise RuntimeError("Run database migrations before starting API")
        try:
            yield
        finally:
            if store is None:
                resolved_store.close()

    docs = resolved_settings.environment == "development"
    app = FastAPI(title="3Chat Agent Eval", lifespan=lifespan,
                  docs_url="/docs" if docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if docs else None)
    app.state.settings = resolved_settings
    app.state.store = resolved_store

    def current_judge() -> dict:
        return runs.current_judge(resolved_store, resolved_settings)

    @app.middleware("http")
    async def enforce_origin(request, call_next):
        if resolved_settings.public_origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("origin") != resolved_settings.public_origin:
                return _problem(403, "请求来源未获授权")
        return await call_next(request)

    @app.exception_handler(NotFound)
    async def not_found(_request, exc):
        return _problem(404, str(exc))

    @app.exception_handler(Conflict)
    async def conflict(_request, exc):
        return _problem(409, str(exc))

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        if not resolved_store.ready():
            raise HTTPException(503, "数据库迁移未就绪")
        return {"status": "ready"}

    @app.get("/api/agent-eval/v1/me")
    def me(principal: Principal = Depends(authenticate)):
        return {"subject": principal.subject}

    @app.get("/api/agent-eval/v1/capabilities")
    def capabilities(_principal: Principal = Depends(authenticate)):
        judge_model = current_judge()["model"]
        return {"judge_configured": bool(judge_model), "judge_model": judge_model,
                "blind_review": runs.current_report(resolved_store),
                "annotation_configured": bool(resolved_settings.annotation_model
                                              or judge_model),
                "registry_configured": bool(resolved_settings.feishu_app_id
                                            and resolved_settings.feishu_base_app_token),
                "environment": resolved_settings.environment}

    @app.post("/api/agent-eval/v1/registry/sync")
    def sync_registry(principal: Principal = Depends(authenticate)):
        try:
            return registry.sync(resolved_store, resolved_settings, principal.subject)
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(503, "飞书台账同步失败，请检查应用授权与配置") from exc

    @app.get("/api/agent-eval/v1/spaces")
    def spaces(_principal: Principal = Depends(authenticate)):
        return [summary(row) for row in resolved_store.spaces()]

    @app.get("/api/agent-eval/v1/spaces/{space_id}")
    def space(space_id: str, _principal: Principal = Depends(authenticate)):
        return summary(resolved_store.space(space_id))

    def platform_models() -> set[str]:
        return {current_judge()["model"], resolved_settings.annotation_model} - {""}

    @app.get("/api/agent-eval/v1/space-models")
    def space_models(_principal: Principal = Depends(authenticate)):
        try:
            return model_mapping.states(resolved_store, resolved_settings, platform_models())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, model_mapping.failure(exc)) from exc

    @app.put("/api/agent-eval/v1/space-models")
    def change_space_models(body: SpaceModelIn, principal: Principal = Depends(authenticate)):
        try:
            return jsonable_encoder(model_mapping.change(
                resolved_store, resolved_settings, body.space_ids, body.model, principal.subject,
                platform_models()))
        except model_mapping.MappingRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except (NotFound, Conflict):
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, model_mapping.failure(exc)) from exc

    @app.post("/api/agent-eval/v1/spaces/{space_id}/model-probe")
    def probe_space_model(space_id: str, _principal: Principal = Depends(authenticate)):
        space = resolved_store.space(space_id)
        return model_mapping.call(resolved_settings, space["target_workspace_id"])

    @app.get("/api/agent-eval/v1/spaces/{space_id}/model-changes")
    def space_model_changes(space_id: str, _principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        return jsonable_encoder(resolved_store.space_model_changes(space_id))

    @app.get("/api/agent-eval/v1/spaces/{space_id}/captures")
    def captures(space_id: str, include_archived: bool = False,
                 _principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        return [capture_summary(row) for row in resolved_store.captures(space_id, include_archived)]

    @app.get("/api/agent-eval/v1/captures/{capture_id}")
    def capture_status(capture_id: str, _principal: Principal = Depends(authenticate)):
        return summary(resolved_store.capture(capture_id))

    @app.post("/api/agent-eval/v1/spaces/{space_id}/captures/atomic", status_code=201)
    def capture_atomic(space_id: str, body: AtomicCaptureIn,
                       idempotency_key: str = Header(min_length=8, max_length=120),
                       principal: Principal = Depends(authenticate)):
        space_row = resolved_store.space(space_id)
        if space_row["status"] != "active":
            raise HTTPException(409, "评测空间尚未验证可用")
        if body.start.tzinfo is None or body.end.tzinfo is None or body.start >= body.end:
            raise HTTPException(422, "请提供带时区的有效采集时间窗")
        return summary(resolved_store.create_capture(
            space_id, Mode.ATOMIC, body.name,
            body.model_dump(mode="json", exclude={"name"}),
            idempotency_key, principal.subject,
        ))

    @app.post("/api/agent-eval/v1/spaces/{space_id}/captures/golden", status_code=201)
    def capture_golden(space_id: str, body: GoldenCaptureIn,
                       idempotency_key: str = Header(min_length=8, max_length=120),
                       principal: Principal = Depends(authenticate)):
        space_row = resolved_store.space(space_id)
        if space_row["status"] != "active":
            raise HTTPException(409, "评测空间尚未验证可用")
        try:
            policy = CollectionPolicy.model_validate(body.policy)
        except ValidationError as exc:
            raise HTTPException(422, "采集规则无效") from exc
        if not policy.start or not policy.end or policy.start.tzinfo is None or (
            policy.end.tzinfo is None
        ):
            raise HTTPException(422, "请提供带时区的采集时间窗")
        if policy.workspace_ids and policy.workspace_ids != [space_row["source_workspace_id"]]:
            raise HTTPException(422, "仅允许采集当前空间登记的来源 Workspace")
        return summary(resolved_store.create_capture(
            space_id, Mode.GOLDEN, body.name,
            {"policy": policy.model_dump(mode="json")}, idempotency_key,
            principal.subject,
        ))

    @app.get("/api/agent-eval/v1/spaces/{space_id}/snapshots")
    def snapshots(space_id: str, include_archived: bool = False,
                 _principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        return [summary(row) for row in resolved_store.snapshots(space_id, include_archived)]

    @app.post("/api/agent-eval/v1/snapshots/{snapshot_id}/drafts", status_code=201)
    def draft_from_snapshot(snapshot_id: str, body: BuildDraftFromSnapshotRequest,
                            principal: Principal = Depends(authenticate)):
        row = resolved_store.snapshot(snapshot_id)
        if row["mode"] != Mode.GOLDEN:
            raise HTTPException(422, "只有对话快照可以生成金标草稿")
        if row["object_purged_at"]:
            raise HTTPException(410, "来源快照已按保留期限清理")
        try:
            raw = objects.get(row["object_key"], row["content_sha256"])
            data, diagnostics = capture.draft_from_snapshot(
                raw, snapshot_id, body.model_dump(mode="json")
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(422, "快照转换失败，请检查来源证据") from exc
        return summary(resolved_store.create_draft(row["space_id"], data, principal.subject)) | {
            "diagnostics": diagnostics,
        }

    @app.get("/api/agent-eval/v1/spaces/{space_id}/drafts")
    def drafts(space_id: str, include_archived: bool = False,
                 _principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        return [summary(row) | {"reviewed_cases": len(row["review_state"])}
                for row in resolved_store.drafts(space_id, include_archived)]

    @app.post("/api/agent-eval/v1/spaces/{space_id}/drafts", status_code=201)
    def create_draft(space_id: str, body: DatasetIn,
                     principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        if body.mode != Mode.GOLDEN:
            raise HTTPException(422, "单步回放使用冻结样本直接发布入口")
        return summary(resolved_store.create_draft(
            space_id, body.model_dump(mode="json"), principal.subject
        ))

    @app.get("/api/agent-eval/v1/drafts/{draft_id}")
    def draft(draft_id: str, _principal: Principal = Depends(authenticate)):
        row = resolved_store.draft(draft_id)
        return summary(row) | {
            "content": (media.draft_view(row["content"], draft_id)
                        if row["status"] == "draft" else None),
            "reviewed_cases": row["review_state"],
        }

    @app.get("/api/agent-eval/v1/drafts/{draft_id}/cases/{case_id}/media/"
             "{turn_index}/{side}/{message_index}/{attachment_index}")
    def draft_image(draft_id: str, case_id: str, turn_index: int,
                    side: Literal["customer", "reference"], message_index: int,
                    attachment_index: int, _principal: Principal = Depends(authenticate)):
        row = resolved_store.draft(draft_id)
        if row["status"] != "draft":
            raise HTTPException(404, "草稿图片不可用")
        attachment = media.attachment_at(row["content"], case_id, turn_index, side,
                                         message_index, attachment_index)
        data, content_type = media.load_image(attachment, resolved_settings.media_allowed_hosts)
        return Response(data, media_type=content_type, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/agent-eval/v1/drafts/{draft_id}/annotations")
    def annotations(draft_id: str, _principal: Principal = Depends(authenticate)):
        resolved_store.draft(draft_id)
        return [annotation_summary(row) for row in resolved_store.annotation_jobs(draft_id)]

    @app.post("/api/agent-eval/v1/drafts/{draft_id}/cases/{case_id}/auto-draft", status_code=201)
    def auto_draft(draft_id: str, case_id: str,
                   idempotency_key: str = Header(min_length=8, max_length=120),
                   principal: Principal = Depends(authenticate)):
        model = resolved_settings.annotation_model or current_judge()["model"]
        if not model:
            raise HTTPException(503, "画像与 Rubric 起草模型尚未配置")
        profile, rubric = annotation.frozen_prompts()
        return annotation_summary(resolved_store.create_annotation(
            draft_id, case_id, profile, rubric, model, idempotency_key,
            principal.subject,
        ))

    @app.post("/api/agent-eval/v1/drafts/{draft_id}/auto-draft", status_code=201)
    def auto_draft_all(draft_id: str,
                       idempotency_key: str = Header(min_length=8, max_length=120),
                       principal: Principal = Depends(authenticate)):
        model = resolved_settings.annotation_model or current_judge()["model"]
        if not model:
            raise HTTPException(503, "画像与 Rubric 起草模型尚未配置")
        profile, rubric = annotation.frozen_prompts()
        result = resolved_store.create_annotations(
            draft_id, profile, rubric, model, idempotency_key, principal.subject,
        )
        return {**result, "jobs": [annotation_summary(job) for job in result["jobs"]]}

    @app.put("/api/agent-eval/v1/drafts/{draft_id}")
    def replace_draft(draft_id: str, body: DatasetIn, if_match: int = Header(),
                      principal: Principal = Depends(authenticate)):
        if body.mode != Mode.GOLDEN:
            raise HTTPException(422, "只允许编辑金标草稿")
        return summary(resolved_store.replace_draft(
            draft_id, body.model_dump(mode="json"), if_match, principal.subject
        ))

    @app.post("/api/agent-eval/v1/drafts/{draft_id}/cases/{case_id}/review")
    def review_case(draft_id: str, case_id: str, if_match: int = Header(),
                    principal: Principal = Depends(authenticate)):
        draft_row = resolved_store.draft(draft_id)
        item = next((item for item in draft_row["content"]["items"]
                     if item.get("session_id") == case_id), None)
        if item is None:
            raise HTTPException(404, "案例不存在")
        try:
            GoldenShared.model_validate(draft_row["content"]["shared"])
            GoldenSession.model_validate(item)
        except ValidationError as exc:
            raise HTTPException(422, "案例结构或画像递推无效") from exc
        return summary(resolved_store.review_draft_case(
            draft_id, case_id, if_match, principal.subject
        ))

    @app.patch("/api/agent-eval/v1/drafts/{draft_id}/cases/{case_id}")
    def edit_case(draft_id: str, case_id: str, body: GoldenCasePatch,
                  if_match: int = Header(), principal: Principal = Depends(authenticate)):
        return summary(resolved_store.edit_draft_case(
            draft_id, case_id, if_match, body.model_dump(mode="json"), principal.subject
        ))

    @app.post("/api/agent-eval/v1/drafts/{draft_id}/publish", status_code=201)
    def publish_draft(draft_id: str, if_match: int = Header(),
                      principal: Principal = Depends(authenticate)):
        row = resolved_store.draft(draft_id)
        try:
            engines.validate_dataset(row["content"])
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, "金标内容未通过合同校验") from exc
        return summary(resolved_store.publish_draft(draft_id, if_match, principal.subject))

    @app.post("/api/agent-eval/v1/spaces/{space_id}/atomic-datasets", status_code=201)
    def publish_atomic(space_id: str, body: DatasetIn,
                       principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        if body.mode != Mode.ATOMIC:
            raise HTTPException(422, "此入口只接收单步回放数据")
        try:
            engines.validate_dataset(body.model_dump(mode="json"))
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, "原子样本不可回放或不完整") from exc
        return summary(resolved_store.publish_atomic(
            space_id, body.model_dump(mode="json"), principal.subject
        ))

    @app.get("/api/agent-eval/v1/spaces/{space_id}/datasets")
    def datasets(space_id: str, include_archived: bool = False,
                 _principal: Principal = Depends(authenticate)):
        resolved_store.space(space_id)
        return [summary(row) for row in resolved_store.datasets(space_id, include_archived)]

    @app.get("/api/agent-eval/v1/datasets/{version_id}")
    def dataset(version_id: str, _principal: Principal = Depends(authenticate)):
        row = resolved_store.dataset(version_id)
        if row["content_purged_at"]:
            raise HTTPException(410, "数据集内容已按保留期限清理")
        return summary(row) | {"content": safe_view(row["content"]),
                               "reviewed_cases": row["review_state"]}

    @app.post("/api/agent-eval/v1/datasets/{version_id}/derive-draft", status_code=201)
    def derive_draft(version_id: str, principal: Principal = Depends(authenticate)):
        return summary(resolved_store.derive_draft(version_id, principal.subject))

    @app.post("/api/agent-eval/v1/spaces/{space_id}/experiments", status_code=201)
    def create_experiment(space_id: str, body: ExperimentIn,
                          idempotency_key: str = Header(min_length=8, max_length=120),
                          principal: Principal = Depends(authenticate)):
        try:
            return runs.create(resolved_store, resolved_settings, space_id, body,
                               idempotency_key, principal.subject)
        except runs.RunRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/agent-eval/v1/batches", status_code=201)
    def create_batch(body: BatchIn, idempotency_key: str = Header(min_length=8, max_length=120),
                     principal: Principal = Depends(authenticate)):
        """批量评测：先建各空间的采集，采集成功后由 Worker 自动开跑评测。"""

        spec = body.model_dump(mode="json", exclude={"name", "space_ids"})
        if body.candidate_preset_id:
            spec["candidate_preset_name"] = resolved_store.candidate_preset(
                body.candidate_preset_id)["name"]
        elif body.mode == Mode.ATOMIC:
            try:
                runs.check_gateway_model(resolved_settings, body.candidate_gateway_model)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        if not current_judge()["model"]:
            raise HTTPException(422, "请先在设置页配置 Judge 模型")
        batch_id = resolved_store.create_batch(body.name, spec, body.space_ids,
                                               idempotency_key, principal.subject)
        return {"id": batch_id}

    @app.post("/api/agent-eval/v1/data/{kind}/archive")
    def archive_data(kind: Literal["drafts", "datasets", "captures", "snapshots"],
                     body: ArchiveIn, principal: Principal = Depends(authenticate)):
        return {"changed": resolved_store.set_data_archived(kind, body.ids, body.archived,
                                                            principal.subject)}

    @app.post("/api/agent-eval/v1/batches/{batch_id}/retry")
    def retry_batch_preparation(batch_id: str, principal: Principal = Depends(authenticate)):
        resolved_store.batch(batch_id)
        return {"changed": resolved_store.retry_batch_preparation(batch_id, principal.subject)}

    @app.get("/api/agent-eval/v1/batches/{batch_id}/replay")
    def batch_replay_preview(batch_id: str, _principal: Principal = Depends(authenticate)):
        try:
            return runs.batch_replay_preview(resolved_store, batch_id)
        except runs.RunRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.post("/api/agent-eval/v1/batches/{batch_id}/replay", status_code=201)
    def replay_batch(batch_id: str, body: BatchReplayIn,
                     idempotency_key: str = Header(min_length=8, max_length=120),
                     principal: Principal = Depends(authenticate)):
        try:
            return runs.replay_batch(resolved_store, resolved_settings, batch_id, body,
                                     idempotency_key, principal.subject)
        except runs.RunRejected as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/agent-eval/v1/batches")
    def batches(include_archived: bool = False, _principal: Principal = Depends(authenticate)):
        return jsonable_encoder(resolved_store.batches(include_archived=include_archived))

    @app.post("/api/agent-eval/v1/batches/archive")
    def archive_batches(body: ArchiveIn, principal: Principal = Depends(authenticate)):
        """批次与其中已结束的评测一起归档或恢复；只从列表隐藏，数据与审计完整保留。"""

        return {"changed": resolved_store.set_batches_archived(body.ids, body.archived,
                                                               principal.subject)}

    @app.delete("/api/agent-eval/v1/batches/{batch_id}")
    def delete_batch(batch_id: str, principal: Principal = Depends(authenticate)):
        remove_objects(resolved_store.delete_batch(batch_id, principal.subject), batch_id)
        return {"status": "deleted"}

    @app.get("/api/agent-eval/v1/batches/{batch_id}")
    def batch(batch_id: str, _principal: Principal = Depends(authenticate)):
        found, items = resolved_store.batch(batch_id)
        return jsonable_encoder({"batch": found, "items": items})

    @app.get("/api/agent-eval/v1/summary")
    def cross_summary(ids: str, _principal: Principal = Depends(authenticate)):
        run_ids = list(dict.fromkeys(item for item in ids.split(",") if item))[:30]
        if not run_ids:
            raise HTTPException(422, "请选择要汇总的评测")
        runs_found = [resolved_store.experiment(run_id) for run_id in run_ids]
        if any(run["mode"] != Mode.ATOMIC for run in runs_found):
            raise HTTPException(422, "汇总只支持单步回放")
        if any(run["evidence_purged_at"] for run in runs_found):
            raise HTTPException(410, "部分评测的逐条证据已按保留期限清理，无法重新汇总")

        hidden = [run for run in runs_found if run["blind_status"] in blind.PENDING]

        def loaded():
            for run in runs_found:
                if run not in hidden:
                    yield (run, resolved_store.jobs(run["id"]), resolved_store.reviews(run["id"]),
                           resolved_store.space(run["space_id"]))

        return jsonable_encoder(safe_view(build_cross_summary(
            loaded(), [(run, resolved_store.space(run["space_id"])) for run in hidden])))

    @app.get("/api/agent-eval/v1/experiments")
    def experiments(space_id: str | None = None, include_archived: bool = False,
                    _principal: Principal = Depends(authenticate)):
        return [summary(row) | {"trial": is_trial(row)}
                for row in resolved_store.experiments(space_id, include_archived)]

    @app.get("/api/agent-eval/v1/experiments/{run_id}")
    def experiment(run_id: str, _principal: Principal = Depends(authenticate)):
        row = resolved_store.experiment(run_id)
        manifest = row["manifest"]
        if row["blind_status"] in blind.PENDING:
            config = manifest["config"]
            manifest = {"mode": manifest["mode"], "blind_review": manifest["blind_review"],
                        "config": {key: config.get(key) for key in
                                   ("candidate_source", "repeats", "trial")}}
        view = summary(row) | {"manifest": safe_view(manifest), "trial": is_trial(row),
                               # 运行中必然不可删除，省掉轮询时的一次查询
                               "deletable": row["status"] not in ("queued", "running")
                               and resolved_store.deletable(run_id)}
        if (row.get("progress") or {}).get("execution_records", 1) > 1:
            view["execution_records"] = resolved_store.execution_records(run_id)
        if row["mode"] == Mode.ATOMIC and resolved_settings.platform_base_url:
            view["profile_fields"] = safe_view(profile_labels.profile_fields(
                resolved_settings, resolved_store.space(row["space_id"])))
        return view

    @app.get("/api/agent-eval/v1/experiments/{run_id}/results")
    def results(run_id: str, principal: Principal = Depends(authenticate)):
        blind_guard(resolved_store.experiment(run_id))
        jobs = resolved_store.report_jobs(run_id)
        mine = [review for review in resolved_store.reviews(run_id)
                if review["subject"] == principal.subject]
        by_job = latest_reviews(mine)
        displayed = {**current_reviews(jobs, mine, actor_type="ai_assisted"),
                     **current_reviews(jobs, mine)}
        output = []
        for row in jobs:
            view = _job_view(row)
            review = displayed.get(row["id"])
            view["review_revision"] = by_job.get(row["id"], {}).get("revision", 0)
            view["review"] = safe_view(review) if review and (
                review["outputs_sha256"] == view["result_sha256"]
            ) else None
            output.append(view)
        return output

    @app.get("/api/agent-eval/v1/experiments/{run_id}/report")
    def report(run_id: str, _principal: Principal = Depends(authenticate)):
        run = resolved_store.experiment(run_id)
        if run["evidence_purged_at"]:
            exports = resolved_store.report_exports(run_id)
            artifact = next((item for item in exports if item["status"] == "succeeded"
                             and item["object_purged_at"] is None), None)
            if artifact is None:
                raise HTTPException(410, "报告明细已按保留期限归档清理")
            archived = objects.get(artifact["object_key"], artifact["content_sha256"])
            return {key: value for key, value in archived.items() if key != "cases"} | {
                "artifact": summary(artifact)
            }
        if run["blind_status"] in blind.PENDING:
            # 未揭晓只展示进度，按状态计数即可，不读取完整请求、响应或数据集。
            counts = resolved_store.job_counts(run_id)
            return {"mode": run["mode"], "status": run["status"],
                    "review_status": run["review_status"], **counts,
                    "fixture": run["manifest"]["config"]["execution_target"] == "fixture",
                    "blind": {"status": run["blind_status"]}, "artifact": None}
        exports = resolved_store.report_exports(run_id)
        jobs, reviews = resolved_store.report_jobs(run_id), resolved_store.reviews(run_id)
        payload = safe_view(build_report(
            run, (resolved_store.dataset_hash(run["dataset_version_id"])
                  if run["mode"] == Mode.ATOMIC else
                  resolved_store.dataset(run["dataset_version_id"])), jobs, reviews,
        ))
        if run["blind_status"]:
            payload["blind"] = blind.stats(resolved_store.blind_review(run_id), jobs, reviews)
        if run["mode"] == Mode.GOLDEN:
            payload["trace"] = traces.overview(resolved_store.experiment_traces(run_id))
        return payload | {"artifact": summary(exports[0]) if exports and run["status"] not in
                          ("queued", "running") else None}

    @app.get("/api/agent-eval/v1/experiments/{run_id}/report/artifact")
    def report_artifact(run_id: str, _principal: Principal = Depends(authenticate)):
        run = resolved_store.experiment(run_id)
        blind_guard(run)
        if run["status"] in ("queued", "running"):
            raise HTTPException(409, "评测仍在运行，报告归档尚未更新")
        artifact = next((item for item in resolved_store.report_exports(run_id)
                         if item["revision"] == run["report_revision"] and
                         item["status"] == "succeeded" and
                         item["object_purged_at"] is None), None)
        if artifact is None:
            raise HTTPException(404, "报告归档尚未生成")
        try:
            return objects.get(artifact["object_key"], artifact["content_sha256"])
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "报告归档校验失败") from exc

    @app.get("/api/agent-eval/v1/experiments/{run_id}/events")
    def events(run_id: str, after_id: int = 0,
               _principal: Principal = Depends(authenticate)):
        resolved_store.experiment(run_id)
        return [safe_view(row) for row in resolved_store.events(run_id, after_id)]

    @app.get("/api/agent-eval/v1/experiments/{run_id}/evidence-bundle")
    def export_evidence_bundle(run_id: str, _principal: Principal = Depends(authenticate)):
        run = resolved_store.experiment(run_id)
        blind_guard(run)
        if run["status"] in {"queued", "running"}:
            raise HTTPException(409, "评测结束后可导出完整证据包")
        bundle = evidence_bundle.EvidenceBundle(resolved_store, objects, run)
        for related in bundle.evaluations:
            blind_guard(related)
        filename = quote(f"{run['name']}-{run_id}-evidence.zip", safe="")
        return StreamingResponse(bundle.stream(), media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename=eval-evidence.zip; "
                                   f"filename*=UTF-8''{filename}",
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        })

    @app.post("/api/agent-eval/v1/experiments/{run_id}/cancel")
    def cancel(run_id: str, principal: Principal = Depends(authenticate)):
        resolved_store.cancel(run_id, principal.subject)
        return {"status": "cancelled"}

    @app.post("/api/agent-eval/v1/experiments/{run_id}/retry-failed")
    def retry_failed(run_id: str,
                     idempotency_key: str = Header(min_length=8, max_length=120),
                     principal: Principal = Depends(authenticate)):
        return resolved_store.retry_failed(run_id, idempotency_key, principal.subject)

    @app.post("/api/agent-eval/v1/experiments/archive")
    def archive(body: ArchiveIn, principal: Principal = Depends(authenticate)):
        """批量归档或恢复；归档只是从列表隐藏，数据与审计完整保留。"""

        return {"changed": resolved_store.set_archived(body.ids, body.archived,
                                                       principal.subject)}

    @app.delete("/api/agent-eval/v1/experiments/{run_id}")
    def delete_experiment(run_id: str, principal: Principal = Depends(authenticate)):
        remove_objects(resolved_store.delete_experiment(run_id, principal.subject), run_id)
        return {"status": "deleted"}

    @app.post("/api/agent-eval/v1/experiments/{run_id}/results/{job_id}/review")
    def review(run_id: str, job_id: str, body: ReviewIn,
               principal: Principal = Depends(authenticate)):
        run = resolved_store.experiment(run_id)
        blind_guard(run)
        allowed = ({"G", "S", "B", "U", "unavailable"} if run["mode"] == Mode.ATOMIC
                   else {"pass", "fail", "unavailable"})
        if body.label not in allowed:
            raise HTTPException(422, "复核标签与评测策略不匹配")
        if run["mode"] == Mode.ATOMIC and body.issue_kind == "gold_error":
            raise HTTPException(422, "单步回放没有金标依据")
        return resolved_store.review(run_id, job_id, body.model_dump(), principal.subject)

    @app.post("/api/agent-eval/v1/experiments/{run_id}/review/complete")
    def complete_review(run_id: str, principal: Principal = Depends(authenticate)):
        resolved_store.complete_review(run_id, principal.subject)
        return {"status": "completed"}

    @app.get("/api/agent-eval/v1/experiments/{run_id}/blind-review")
    def blind_review(run_id: str, principal: Principal = Depends(authenticate)):
        """盲评样本：A/B 顺序按样本随机，不含 AI 评审、模型名、耗时与 token。"""

        run = resolved_store.experiment(run_id)
        if not run["blind_status"]:
            raise HTTPException(404, "该评测未开启人工盲评")
        if run["blind_status"] not in blind.PENDING:
            return {"status": run["blind_status"]}
        row = resolved_store.blind_review(run_id)
        if row is None:
            if run["status"] in ("queued", "running"):
                return {"status": "blind_waiting"}
            row = resolved_store.draw_blind_review(
                run_id, blind.draw(resolved_store.blind_draw_jobs(run_id),
                                   run["manifest"]["blind_review"]))
        jobs = resolved_store.blind_sample_jobs(run_id, row["sample"])
        reviews = resolved_store.reviews(run_id)
        active = current_reviews(jobs, reviews)
        mine = latest_reviews([review for review in reviews
                               if review["subject"] == principal.subject])
        by_id = {job["id"]: job for job in jobs}
        found = blind.targets(row["sample"], jobs)
        items = []
        for item in row["sample"]:
            execute, target = by_id[item["job_id"]], found.get(item["job_id"])
            outputs = response_format.annotate(execute["input"]["item"]["request"],
                                               execute["result"]) or {}
            other = "baseline" if item["a"] == "candidate" else "candidate"
            own = mine.get(target["id"]) if target else None
            current = own if own and own["outputs_sha256"] == target["result_sha256"] else None
            called = {call["name"] for side in (item["a"], other)
                      for call in (outputs.get(side) or {}).get("tool_calls", [])}
            case = _atomic_case(execute["input"]["item"], called)
            case.pop("upstream_model")
            items.append({
                # 上下文已按报告页规则脱敏并保留图片原链接，不能再整体脱敏
                "job_id": execute["id"], "case": case, "ready": target is not None,
                # 只取消息与工具调用：输出里的 side_id 会暴露身份
                "outputs": safe_view({name: {key: (outputs.get(side) or {}).get(key, [])
                                             for key in ("messages", "tool_calls", "format_check")}
                                      for name, side in (("A", item["a"]), ("B", other))}),
                "result_sha256": target["result_sha256"] if target else None,
                "reviewed": bool(target and target["id"] in active),
                "revision": own["revision"] if own else 0,
                "review": safe_view({"choice": blind.to_choice(current["label"], item["a"]),
                                     "issue_kind": current["issue_kind"], "note": current["note"]})
                if current else None,
            })
        return {"status": "blind_open", "items": items,
                "reviewed": sum(item["reviewed"] for item in items)}

    @app.post("/api/agent-eval/v1/experiments/{run_id}/blind-review/items/{job_id}")
    def blind_review_item(run_id: str, job_id: str, body: BlindReviewIn,
                          principal: Principal = Depends(authenticate)):
        if resolved_store.experiment(run_id)["blind_status"] not in blind.PENDING:
            raise HTTPException(409, "盲评已揭晓，请在报告页修订")
        row = resolved_store.blind_review(run_id)
        item = next((item for item in (row["sample"] if row else [])
                     if item["job_id"] == job_id), None)
        if item is None:
            raise HTTPException(404, "不是本次盲评的样本")
        target = blind.targets([item], resolved_store.blind_sample_jobs(run_id, [item])).get(job_id)
        if target is None:
            raise HTTPException(409, "这条样本正在补跑，完成后再评")
        return resolved_store.review(run_id, target["id"], body.model_dump(exclude={"choice"}) | {
            "label": blind.to_label(body.choice, item["a"])}, principal.subject)

    @app.post("/api/agent-eval/v1/experiments/{run_id}/blind-review/reveal")
    def reveal_blind_review(run_id: str, body: RevealIn,
                            principal: Principal = Depends(authenticate)):
        resolved_store.reveal_blind_review(run_id, principal.subject, body.reason)
        return {"status": "revealed"}

    @app.get("/api/agent-eval/v1/experiments/{run_id}/results/{job_id}/evidence")
    def evidence(run_id: str, job_id: str, _principal: Principal = Depends(authenticate)):
        """单步回放给原始请求与双方完整响应，逐轮回放给执行观测（画像回读、AgentLogs、
        工具诊断）；按需加载，与页面同样脱敏。"""

        job, judge = resolved_store.evidence_jobs(run_id, job_id)
        if job["evidence_purged_at"]:
            raise HTTPException(410, "逐条证据已按保留期限清理")
        data = job["input"] or {}
        if data.get("mode") == Mode.GOLDEN:
            # 判分任务的输入里是成功执行的观测；失败的 Turn 不进 Judge，观测留在执行结果里
            observation = (((judge or {}).get("input") or {}).get("observation")
                           or (job["result"] or {}).get("observation"))
            if not observation:
                raise HTTPException(404, "该轮次尚无执行观测")
            return safe_view({"observation": observation})
        item = data.get("item")
        if data.get("mode") != Mode.ATOMIC or not item:
            raise HTTPException(404, "该结果没有保存原始请求")
        # 揭晓前只给原始请求：双方响应体里带着模型名
        if resolved_store.experiment(run_id)["blind_status"] in blind.PENDING:
            return safe_view({"request": item["request"]})
        candidate = ((judge or {}).get("input") or {}).get("response")
        return safe_view({"request": item["request"], "baseline": item["response"],
                          "candidate": candidate})

    @app.get("/api/agent-eval/v1/experiments/{run_id}/results/{job_id}/media/"
             "{side}/{message_index}/{attachment_index}")
    def result_image(run_id: str, job_id: str,
                     side: Literal["context", "candidate", "reference"],
                     message_index: int, attachment_index: int,
                     _principal: Principal = Depends(authenticate)):
        job, _ = resolved_store.evidence_jobs(run_id, job_id)
        if job["evidence_purged_at"]:
            raise HTTPException(410, "逐条证据已按保留期限清理")
        if job["stage"] != "execute" or (job["input"] or {}).get("mode") != Mode.GOLDEN:
            raise HTTPException(404, "该结果没有逐轮图片证据")
        if min(message_index, attachment_index) < 0:
            raise HTTPException(404, "图片附件不存在")
        try:
            messages = (job["input"]["turn"]["expected"]["messages"] if side == "reference"
                        else (job["result"] or {})[side])
            attachment = messages[message_index]["attachments"][attachment_index]
        except (KeyError, IndexError, TypeError) as exc:
            raise HTTPException(404, "图片附件不存在") from exc
        data, content_type = media.load_image(attachment, resolved_settings.media_allowed_hosts)
        return Response(data, media_type=content_type, headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        })

    @app.get("/api/agent-eval/v1/experiments/{run_id}/results/{job_id}/trace")
    def trace(run_id: str, job_id: str, _principal: Principal = Depends(authenticate)):
        """一个轮次的模型调用链路：索引来自数据库，完整请求与响应从对象存储按需读取并脱敏。"""

        rows = resolved_store.experiment_traces(run_id, job_id)
        if not rows:
            raise HTTPException(404, "该轮次没有登记模型调用链路（Fixture 执行或执行前已失败）")
        row = rows[0]
        view = {key: row[key] for key in (
            "status", "request_id", "request_id_echoed", "echo_verified", "expected_calls",
            "call_count", "summary", "error_code", "collected_at", "object_purged_at")}
        if row["object_key"] and not row["object_purged_at"]:
            try:
                view["spans"] = objects.get(row["object_key"], row["content_sha256"])["spans"]
            except (OSError, ValueError) as exc:
                raise HTTPException(503, "模型调用链路校验失败") from exc
        return safe_view(view)

    @app.get("/api/agent-eval/v1/spaces/{space_id}/trace-readiness")
    def trace_readiness(space_id: str, _principal: Principal = Depends(authenticate)):
        """建逐轮回放前预检：EVAL 空间是否开启了完整请求体日志（从网关日志推断）。"""

        space = resolved_store.space(space_id)
        if space["target_workspace_id"] == resolved_settings.internal_tenant_id:
            # Judge 经网关调用时记在内部租户名下，日志计数会把 Judge 当成被测 Agent 的调用
            return {"status": "unknown", "detail": "该 EVAL 空间与评测平台 Judge 同属一个租户，"
                    "网关日志混有 Judge 调用；首轮执行后会在报告中复核"}
        try:
            return traces.readiness(SlsRecordSource(), space["target_workspace_id"],
                                    datetime.now(UTC))
        except (ConfigError, CaptureError) as exc:
            return {"status": "unknown", "detail": str(exc)[:200]}

    @app.get("/api/agent-eval/v1/settings/candidates")
    def candidate_presets(_principal: Principal = Depends(authenticate)):
        return [preset_view(row) for row in resolved_store.candidate_presets()]

    @app.post("/api/agent-eval/v1/settings/candidates", status_code=201)
    def create_candidate_preset(body: CandidatePresetIn,
                                principal: Principal = Depends(authenticate)):
        if body.api_key is None or not body.api_key.get_secret_value().strip():
            raise HTTPException(422, "新建候选模型需要填写 API Key")
        return resolved_store.save_candidate_preset(None, preset_data(body), principal.subject)

    @app.put("/api/agent-eval/v1/settings/candidates/{preset_id}")
    def update_candidate_preset(preset_id: str, body: CandidatePresetIn,
                                principal: Principal = Depends(authenticate)):
        return resolved_store.save_candidate_preset(preset_id, preset_data(body),
                                                    principal.subject)

    @app.delete("/api/agent-eval/v1/settings/candidates/{preset_id}")
    def delete_candidate_preset(preset_id: str, principal: Principal = Depends(authenticate)):
        resolved_store.delete_candidate_preset(preset_id, principal.subject)
        return {"status": "deleted"}

    @app.post("/api/agent-eval/v1/settings/candidates/probe")
    def probe_candidate(body: CandidateProbeIn, _principal: Principal = Depends(authenticate)):
        """用一次极小的 Responses 请求确认接口、模型与 key 可用；编辑时 key 留空则用已保存的。"""

        key = body.api_key.get_secret_value() if body.api_key else ""
        if not key and body.preset_id:
            key = vault.unseal(resolved_settings,
                               resolved_store.candidate_preset(body.preset_id)["sealed_key"])
        if not key:
            raise HTTPException(422, "请填写 API Key")
        return engines.probe_candidate(body.base_url, body.model, key)

    def preset_data(body: CandidatePresetIn) -> dict:
        key = body.api_key.get_secret_value().strip() if body.api_key else ""
        try:
            sealed = vault.seal(resolved_settings, key) if key else None
        except ValueError as exc:  # 平台未配置加密密钥
            raise HTTPException(503, str(exc)) from exc
        return {"name": body.name.strip(), "base_url": body.base_url, "model": body.model.strip(),
                "note": body.note.strip(), "revision": body.revision, "sealed_key": sealed,
                "key_hint": key_hint(key) if key else None}

    def remove_objects(keys: list[str], owner: str) -> None:
        """记录已在事务内删除；报告归档对象尽力清理，失败只留下无引用的孤立文件。"""

        for key in keys:
            try:
                objects.delete(key)
            except Exception:  # OSS SDK 的异常类型不统一，清理失败不影响删除结果
                logger.warning("report_object_cleanup_failed owner=%s", owner)

    def setting_view(key: str, config: dict) -> dict:
        """设置的当前值与修改记录；最新一版即生效值，从未保存过时 revision 为 0。"""

        revisions = resolved_store.setting_revisions(key)
        return {"config": config, "revision": revisions[0]["revision"] if revisions else 0,
                "history": [{"revision": row["revision"], "config": row["value"],
                             "subject": row["subject"], "created_at": row["created_at"]}
                            for row in revisions]}

    @app.get("/api/agent-eval/v1/settings/report")
    def report_settings(_principal: Principal = Depends(authenticate)):
        return setting_view("report", runs.current_report(resolved_store))

    @app.put("/api/agent-eval/v1/settings/report")
    def save_report_settings(body: ReportConfigIn,
                             principal: Principal = Depends(authenticate)):
        return resolved_store.save_setting("report", body.config.model_dump(), body.revision,
                                           principal.subject)

    @app.get("/api/agent-eval/v1/settings/judge")
    def judge_settings(_principal: Principal = Depends(authenticate)):
        atomic = pairwise.prompt()
        golden = engines.golden_prompt()
        return setting_view("judge", current_judge()) | {
            "prompts": [
                {"mode": Mode.ATOMIC, "version": atomic["version"],
                 "sha256": atomic["sha256"], "text": atomic["text"]},
                {"mode": Mode.GOLDEN, "version": golden.version_label,
                 "sha256": golden.content_sha256, "text": golden.content},
            ],
            "prompt_usage": [dict(row) for row in resolved_store.prompt_usage()],
        }

    @app.put("/api/agent-eval/v1/settings/judge")
    def save_judge_settings(body: JudgeConfigIn, principal: Principal = Depends(authenticate)):
        return resolved_store.save_setting("judge", body.config.model_dump(), body.revision,
                                           principal.subject)

    @app.get("/api/agent-eval/v1/settings/judge/models")
    def judge_models(_principal: Principal = Depends(authenticate)):
        try:
            return gateway.chat_models(resolved_settings)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "暂时无法读取模型网关的模型列表") from exc

    @app.post("/api/agent-eval/v1/settings/judge/probe")
    def probe_judge(body: JudgeConfig, _principal: Principal = Depends(authenticate)):
        try:
            return gateway.probe(resolved_settings, body.model, body.timeout_seconds)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, str(exc)[:200]) from exc

    @app.get("/api/agent-eval/v1/workbench")
    def workbench(principal: Principal = Depends(authenticate)):
        runs = resolved_store.experiments()
        return {"pending_reports": [summary(row) for row in runs
                                    if row["blind_status"] == "blind_open" or (
                                        row["mode"] == Mode.GOLDEN
                                        and row["review_status"] != "completed")],
                "running_batches": [row for row in resolved_store.batches()
                                    if row["finished"] + row["failed"] < row["spaces"]],
                "gold_drafts": [summary(row) | {
                    "reviewed_cases": len(row["review_state"]),
                    "case_count": len(row["content"]["items"]),
                } for row in resolved_store.pending_drafts()],
                "recent": [summary(row) for row in runs[:20]]}

    return app


def key_hint(key: str) -> str:
    """只露出首尾几位，便于确认换没换 key，无法据此还原。"""

    return f"{key[:3]}…{key[-4:]}" if len(key) > 12 else f"…{key[-2:]}"


def is_trial(row: dict) -> bool:
    return bool((row["manifest"].get("config") or {}).get("trial"))


def preset_view(row: dict) -> dict:
    return {key: row[key] for key in ("id", "name", "base_url", "model", "key_hint", "note",
                                      "revision", "updated_by", "updated_at")}


def _problem(status: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status, content={"detail": detail})


def _job_view(row: dict):
    view = summary(row) | {
        "result_sha256": row["result_sha256"] or digest(
            {"status": row["status"], "error_code": row["error_code"]}
        ),
        "result": safe_view(row["result"]) if row["result"] is not None else None,
    }
    item = (row["input"] or {}).get("item")
    if row["stage"] == "execute" and row["input"].get("mode") == Mode.ATOMIC and item:
        view["result"] = safe_view(response_format.annotate(item["request"], row["result"]))
        outputs = row["result"] or {}
        called = {call["name"] for side in ("baseline", "candidate")
                  for call in (outputs.get(side) or {}).get("tool_calls", [])}
        view["case"] = _atomic_case(item, called)
    return view


# 客户发送的图片在消息文本里是「[客户发送了图片] 链接」，拆出来单独展示原图
IMAGE = re.compile(r"\[客户发送了图片\][ \t]*(https?://[^\s\"'<>]+)?")


def _split_images(text: str) -> tuple[str, list[str]]:
    urls = [url for url in IMAGE.findall(text) if url]
    return IMAGE.sub("", text).strip(), urls


def _atomic_case(item: dict, called: set[str] = frozenset()) -> dict:
    """把原始请求拆成业务可读的上下文，供逐条核对；不返回完整请求体。

    保留业务正文、联系方式和图片链接，只过滤凭据，以便人工按原始输入复核。
    """

    call = ModelCall.model_validate(item)
    case = to_case(call)
    current, current_images = _split_images(case.current_turn)
    turns = [(turn["role"], *_split_images(turn["text"])) for turn in case.prior_turns]
    view = safe_view({
        "task_name": case.task_name, "current_turn": current, "task_input": case.task_input,
        "prior_turns": [{"role": role, "text": text} for role, text, _ in turns],
        "business_rules": case.business_rules,
        "history_tool_calls": [c.model_dump() for c in case.history_tool_calls],
        "profile_snapshot": case.profile_snapshot, "session_facts": case.session_facts,
        "tool_labels": case.tool_labels, "has_image": has_image(call.request),
        "upstream_model": call.upstream_model or call.request.get("model", ""),
        "timestamp": call.timestamp,
        # 只摘双方本次与历史上出现过的画像写入工具，避免把整份提示词按工具拆开重复下发
        "tool_rules": tool_rules(call.request, sorted(
            name for name in called | {c.name for c in case.history_tool_calls}
            if name.startswith(PROFILE_TOOL_PREFIX))),
    })
    view["current_images"] = current_images
    for turn, (_, _, images) in zip(view["prior_turns"], turns, strict=True):
        turn["images"] = images
    return view
