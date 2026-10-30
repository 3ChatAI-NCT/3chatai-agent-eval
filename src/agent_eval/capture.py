"""Read-only production sampling; raw snapshots remain in server-side object storage."""

from collections import Counter

from atomic_eval.batch import BatchSpec, collect_batch
from atomic_eval.config import Limits
from atomic_eval.sls import CaptureError, SlsRecordSource, build_query
from golden_eval.adapters.platform_profile import PlatformProfileReader
from golden_eval.adapters.platform_source import PlatformCorpusSource
from golden_eval.application.collection import CorpusCollector
from golden_eval.application.golden_build import build_draft_request
from golden_eval.domain.corpus import CollectionPolicy, CorpusSnapshot
from golden_eval.domain.models import BuildDraftFromSnapshotRequest, RunIdentity

from .models import Mode, digest
from .registry import credentials


def collect(job: dict, space: dict, settings):
    spec = job["spec"]
    if job["mode"] == Mode.ATOMIC:
        from datetime import UTC, datetime, timedelta

        # SLS 日志写入有延迟，太新的窗口结果不稳定：结束时间自动截到可稳定查询的时刻
        start = datetime.fromisoformat(spec["start"])
        settled = datetime.now(UTC) - timedelta(seconds=Limits().min_window_delay_seconds + 60)
        end = min(datetime.fromisoformat(spec["end"]), settled)
        if start >= end:
            raise ValueError("所选时间窗口过新，生产日志尚未写完，请把开始时间往前调")
        source = SlsRecordSource()
        batch = collect_batch(
            BatchSpec(
                workspace_ids=[space["source_workspace_id"]],
                start=start, end=end,
                size=spec["size"], model=spec.get("model"), terms=spec.get("terms", []),
            ), source,
        )
        if not batch.calls:
            raise ValueError(_empty_reason(source, space["source_workspace_id"], start, end,
                                           batch.manifest["funnel"], spec))
        snapshot = {"manifest": batch.manifest, "calls": batch.calls,
                    "excluded": batch.excluded}
        models = Counter((call.get("request") or {}).get("model") or "unknown"
                         for call in batch.calls)
        summary = {"count": len(batch.calls), "funnel": batch.manifest["funnel"],
                   "capture": batch.manifest["capture"], "guidance": batch.manifest["guidance"],
                   "window": {"start": start.isoformat(), "end": end.isoformat()},
                   "models": dict(models)}
        dataset = {"name": job["name"], "mode": Mode.ATOMIC,
                   "items": batch.calls, "shared": {"capture": batch.manifest}}
        return snapshot, summary, batch.manifest["calls_sha256"], dataset

    token, _ = credentials(settings, space)
    policy = CollectionPolicy.model_validate(spec["policy"])
    if policy.start is None or policy.end is None:
        raise ValueError("生产采集必须指定开始和结束时间")
    policy = policy.model_copy(update={"workspace_ids": [space["source_workspace_id"]]})
    identity = RunIdentity(tenant_id=space["source_workspace_id"],
                           source="connection_settings")
    source = PlatformCorpusSource(base_url=settings.platform_base_url,
                                  identity=identity, token=token)
    snapshot = CorpusCollector(source).collect(job["id"], policy)
    profile = PlatformProfileReader(base_url=settings.platform_base_url,
                                    identity=identity, token=token).read(
                                        space["source_workspace_id"]
                                    )
    snapshot = snapshot.model_copy(update={"workspaces": [
        workspace.model_copy(update={"profile_contract": profile})
        for workspace in snapshot.workspaces
    ]})
    if snapshot.session_count == 0:
        raise ValueError("所选时间窗口内没有满足条件的会话")
    summary = {"count": snapshot.session_count,
               "window": {"start": policy.start.isoformat(), "end": policy.end.isoformat()},
               "counts": snapshot.counts.model_dump(mode="json"),
               "profile_fields": [field.key for field in profile.fields],
               "profile_contract_sha256": digest(profile.model_dump(mode="json")),
               "warnings": profile.warnings}
    return snapshot.model_dump(mode="json"), summary, snapshot.source_hash, None


def _empty_reason(source, workspace_id: str, start, end, funnel: dict, spec: dict) -> str:
    """区分采不到样本的几种情况，给出能直接照做的提示。"""

    excluded = funnel.get("excluded") or {}
    if set(excluded) == {"model_mismatch"}:
        return f"所选时间窗口内没有模型 {spec.get('model')} 的调用，请去掉模型筛选或换一个时间窗口"
    if excluded:
        return "所选时间窗口内的模型调用都不可等价回放（失败请求或响应不完整），请换一个时间窗口"
    # 采集查询要求带完整请求体；保留其余筛选条件、去掉这一条再数一次，区分没流量与没开请求体日志
    try:
        calls = source.count(int(start.timestamp()), int(end.timestamp()),
                             build_query([workspace_id], terms=spec.get("terms"),
                                         require_body=False))
    except CaptureError:
        return "所选时间窗口内没有可等价回放的模型调用"
    if calls:
        return (f"窗口内有 {calls} 次模型调用，但该空间未开启完整请求体日志，无法等价回放；"
                "请先在阿里云侧为该空间开启请求体记录")
    return "所选时间窗口内该空间没有符合条件的模型调用，请扩大时间窗口或减少筛选条件"


def draft_from_snapshot(snapshot_data: dict, snapshot_id: str, request: dict):
    snapshot = CorpusSnapshot.model_validate(snapshot_data)
    body = BuildDraftFromSnapshotRequest.model_validate(request)
    draft, anomalies, turns = build_draft_request(snapshot, body)
    if not draft.sessions:
        raise ValueError("快照中没有可生成金标的会话")
    shared = draft.shared.model_dump(mode="json")
    shared["dataset"]["source_config"]["session_routing"] = {
        session.session_id: {"bot_user_id": session.bot_user_id or "",
                             "im_bot_id": session.im_bot_id or ""}
        for workspace in snapshot.workspaces for session in workspace.sessions
        if session.included
    }
    return {
        "name": draft.shared.dataset.name,
        "mode": Mode.GOLDEN,
        "shared": shared,
        "items": [session.model_dump(mode="json") for session in draft.sessions],
        "source_snapshot_id": snapshot_id,
    }, {"anomalies": [item.model_dump(mode="json") for item in anomalies],
        "turns_per_session": turns}
