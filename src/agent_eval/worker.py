import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from time import monotonic

import httpx

from atomic_eval.config import ConfigError
from atomic_eval.pairwise import JudgeError
from atomic_eval.sls import CaptureError, SlsRecordSource

from . import (
    annotation,
    capture,
    engines,
    judge_evidence,
    rubric_policy,
    runs,
    traces,
    workspace_policy,
)
from .models import ExperimentIn, Mode
from .objects import create_objects
from .privacy import safe_view
from .reporting import report as build_report
from .store import Conflict, NotFound

logger = logging.getLogger(__name__)
RECOVER_INTERVAL = 15.0
MAX_ATTEMPTS = 3
# 平台自己抛出的校验与配置错误可以展示给用户；其他异常可能夹带第三方响应内容，只记类型。
# 按精确类型匹配，避免 pydantic 校验错误这类 ValueError 子类把输入数据带出去。
USER_FACING_ERRORS = (ValueError, ConfigError, CaptureError)


def run_one(store, settings, worker_id):
    job = store.claim(worker_id, settings.lease_seconds,
                      space_limit=settings.space_concurrency)
    if job is None:
        return False
    stopped, lost = Event(), Event()

    def renew():
        while not stopped.wait(settings.lease_seconds / 3):
            try:
                if not store.heartbeat(job, settings.lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    heartbeat = Thread(target=renew, daemon=True)
    heartbeat.start()
    traced = False
    try:
        run = store.one("SELECT manifest,candidate_api_key FROM eval_experiments WHERE id=%s",
                        (job["experiment_id"],))
        if (job["input"]["mode"] == Mode.GOLDEN
                and job["input"]["config"]["execution_target"] == "platform"
                and run["manifest"].get("judge_prompt_version") in {
                    "full-evaluation-v13", "full-evaluation-v14"}):
            run["manifest"]["workspace_policy"] = workspace_policy.prepare(
                job, store, settings, run["manifest"])
        if job["stage"] == "execute":
            workspace_id = engines.trace_workspace(job)
            request_id = store.register_trace(job, workspace_id) if workspace_id else None
            traced = request_id is not None
            result, judge_input = engines.execute(job, settings, run["candidate_api_key"],
                                                  request_id)
        else:
            if (job["input"]["mode"] == Mode.GOLDEN
                    and job["input"]["config"]["execution_target"] == "platform"
                    and run["manifest"].get("judge_prompt_version") == "full-evaluation-v14"):
                job["input"]["judge_rubric"] = rubric_policy.prepare(
                    job, store, settings, run["manifest"])
            if (job["input"]["mode"] == Mode.GOLDEN
                    and job["input"]["config"]["execution_target"] == "platform"
                    and run["manifest"].get("judge_prompt_version") in {
                        "full-evaluation-v12", "full-evaluation-v13", "full-evaluation-v14"}):
                job["input"]["judge_evidence"] = judge_evidence.prepare(
                    job, store, create_objects(settings))
            result, judge_input = engines.grade(job, settings, run["manifest"]), None
        if not lost.is_set():
            store.finish(job, result=result, judge_input=judge_input,
                         engine_sha256=engines.engine_fingerprint())
    except engines.ExecutionError as exc:
        store.finish(job, result=exc.result, error_code=exc.code, uncertain=exc.uncertain,
                     engine_sha256=engines.engine_fingerprint())
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
        # Judge 与原子回放只读取模型输出、没有副作用，限流、服务端错误和网络失败可安全重试；
        # 金标执行会创建测试联系人，重发可能重复，只能标记待确认
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        transient = status is None or status == 429 or status >= 500
        side_effect_free = job["stage"] == "judge" or job["input"]["mode"] == Mode.ATOMIC
        if transient and side_effect_free and store.retry(job, MAX_ATTEMPTS):
            logger.warning("job_retry job_id=%s attempt=%s", job["id"], job["attempt"])
        elif status is not None:
            # 状态码足以说明原因（如 413 请求体过大、401 key 无效），不含响应正文
            logger.error("job_failed job_id=%s http_status=%s", job["id"], status)
            store.finish(job, error_code=f"http_{status}",
                         engine_sha256=engines.engine_fingerprint())
        else:
            store.finish(job, error_code="external_delivery_unknown", uncertain=True,
                         engine_sha256=engines.engine_fingerprint())
    except Exception as exc:
        # 异常正文可能包含请求体、URL 或凭据，日志/页面只保留类别及关联 ID。
        logger.error("job_failed job_id=%s error_type=%s", job["id"], type(exc).__name__)
        code = "judge_output_invalid" if isinstance(exc, JudgeError) else "execution_failed"
        store.finish(job, error_code=code, engine_sha256=engines.engine_fingerprint())
    finally:
        stopped.set()
        heartbeat.join(timeout=2)
        if traced:
            store.settle_trace(job, settings.trace_settle_seconds, settings.trace_deadline_seconds)
    return True


def run_trace(store, settings, objects) -> bool:
    """采集一条到期的模型调用链路。只读网关日志，失败可安全重试，不影响评测结论。"""

    trace = store.claim_trace(settings.lease_seconds)
    if trace is None:
        return False
    now = datetime.now(UTC)
    try:
        traces.poll(store, objects, SlsRecordSource(), trace, now)
    except ConfigError:
        store.finish_trace(trace, status="failed", error_code="sls_not_configured")
    except CaptureError:
        # SLS 重试后仍失败：截止前继续排期，之后记为失败
        if now >= trace["deadline_at"]:
            store.finish_trace(trace, status="failed", error_code="sls_query_failed")
        else:
            store.reschedule_trace(trace, trace["call_count"],
                                   now + traces.poll_delay(trace["poll_count"]))
    except Exception as exc:
        logger.error("trace_failed trace_id=%s error_type=%s", trace["id"], type(exc).__name__)
        store.finish_trace(trace, status="failed", error_code="trace_collect_failed")
    return True


def run_capture(store, settings, objects, worker_id):
    job = store.claim_capture(worker_id, settings.lease_seconds)
    if job is None:
        return False
    stopped, lost = Event(), Event()

    def renew():
        while not stopped.wait(settings.lease_seconds / 3):
            try:
                if not store.heartbeat_capture(job, settings.lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    heartbeat = Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        space = store.space(job["space_id"])
        raw, summary, source_hash, dataset = capture.collect(job, space, settings)
        key = f"captures/{job['id']}.json.gz"
        sha = objects.put(key, raw)
        if not lost.is_set():
            store.finish_capture(job, object_key=key, content_sha256=sha,
                                 source_hash=source_hash, summary=summary, dataset=dataset)
    except Exception as exc:
        message = str(exc)[:300] if type(exc) in USER_FACING_ERRORS else None
        logger.error("capture_failed capture_id=%s error_type=%s message=%s",
                     job["id"], type(exc).__name__, message or "-")
        if not lost.is_set():
            store.finish_capture(job, error_code="capture_failed", error_message=message)
    finally:
        stopped.set()
        heartbeat.join(timeout=2)
    return True


def run_batch_starts(store, settings) -> bool:
    """批量评测里采集成功的空间自动开跑评测。

    由周期性的恢复任务调用（约 15 秒一次、单线程）。评测以条目 ID 作幂等键创建：
    Worker 在建完评测后、回写条目前重启，重做也只会得到同一条评测。
    """

    items = store.pending_batch_items()
    for item in items:
        spec = item["spec"]
        try:
            dataset_id = item["dataset_version_id"]
            if spec.get("mode") == Mode.GOLDEN and not dataset_id:
                snapshot = store.snapshot(item["snapshot_id"])
                raw = create_objects(settings).get(
                    snapshot["object_key"], snapshot["content_sha256"])
                data, _ = capture.draft_from_snapshot(raw, snapshot["id"], {
                    "name": f"{item['batch_name']} · {item['space_name']}"})
                data["shared"]["context"]["reference_status"] = "generated"
                engines.validate_dataset(data)
                dataset_id = store.freeze_reference_capture(item["capture_id"], data,
                                                             item["created_by"])
            body = ExperimentIn(name=f"{item['batch_name']} · {item['space_name']}",
                                dataset_version_id=dataset_id,
                                repeats=(spec.get("repeats", 1)
                                         if spec.get("mode") == Mode.GOLDEN else 1),
                                trial=spec["trial"],
                                candidate_gateway_model=spec.get("candidate_gateway_model"),
                                candidate_preset_id=spec.get("candidate_preset_id"))
            run = runs.create(store, settings, item["space_id"], body, f"batch:{item['id']}",
                              item["created_by"])
        except (runs.RunRejected, NotFound, Conflict, ValueError) as exc:
            store.finish_batch_item(item["id"], error_message=str(exc)[:300])
            continue
        except Exception as exc:
            # 这里的异常会让整个 Worker 线程退出，逐条兜底；正文可能含第三方响应，只记类型
            logger.error("batch_start_failed item_id=%s error_type=%s", item["id"],
                         type(exc).__name__)
            store.finish_batch_item(item["id"],
                                    error_message="自动开跑失败，请单独为该空间创建评测")
            continue
        store.finish_batch_item(item["id"], experiment_id=run["id"])
    return bool(items)


def run_annotation(store, settings, worker_id):
    job = store.claim_annotation(worker_id, settings.lease_seconds)
    if job is None:
        return False
    stopped, lost = Event(), Event()

    def renew():
        while not stopped.wait(settings.lease_seconds / 3):
            try:
                if not store.heartbeat_annotation(job, settings.lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    heartbeat = Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        session = annotation.generate(job, store, settings)
        if not lost.is_set():
            store.finish_annotation(job, session=session)
    except Exception as exc:
        error_code = getattr(exc, "code", "annotation_failed")
        logger.error("annotation_failed annotation_id=%s error_type=%s error_code=%s",
                     job["id"], type(exc).__name__, error_code)
        if not lost.is_set():
            store.finish_annotation(job, error_code=error_code,
                                    **({"error_detail": exc.detail}
                                       if isinstance(exc, annotation.AnnotationError) else {}))
    finally:
        stopped.set()
        heartbeat.join(timeout=2)
    return True


def run_report(store, settings, objects, worker_id):
    job = store.claim_report(worker_id, settings.lease_seconds)
    if job is None:
        return False
    stopped, lost = Event(), Event()

    def renew():
        while not stopped.wait(settings.lease_seconds / 3):
            try:
                if not store.heartbeat_report(job, settings.lease_seconds):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    heartbeat = Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        run = store.experiment(job["experiment_id"])
        payload = build_report(run, store.dataset(run["dataset_version_id"]),
                               store.report_jobs(run["id"]), store.reviews(run["id"]),
                               include_cases=True)
        payload = safe_view({**payload, "experiment_id": run["id"],
                             "report_revision": job["revision"]})
        key = f"reports/{run['id']}/v{job['revision']}.json.gz"
        sha = objects.put(key, payload)
        if not lost.is_set():
            store.finish_report(job, object_key=key, sha=sha)
    except Exception as exc:
        logger.error("report_export_failed report_id=%s error_type=%s",
                     job["id"], type(exc).__name__)
        if not lost.is_set():
            store.finish_report(job, error_code="report_export_failed")
    finally:
        stopped.set()
        heartbeat.join(timeout=2)
    return True


def serve(store, settings):
    stop = Event()
    objects = create_objects(settings)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    # 回收过期租约只需要偶尔做一次：原先每个线程每秒都扫一遍，共享的生产库白白承担负载
    recover_lock, last_recover = Lock(), [0.0]

    def recover():
        with recover_lock:
            if monotonic() - last_recover[0] < RECOVER_INTERVAL:
                return
            last_recover[0] = monotonic()
        store.recover_expired()
        store.recover_captures()
        store.recover_reports()
        store.recover_annotations()
        store.recover_traces(settings.trace_settle_seconds, settings.trace_deadline_seconds)
        # 批量评测开跑只由拿到本轮时间片的一个线程做，避免多个线程重复加载同一批数据集
        run_batch_starts(store, settings)

    def loop(index):
        while not stop.is_set():
            recover()
            if (not run_report(store, settings, objects, f"worker-{index}")
                    and not run_capture(store, settings, objects, f"worker-{index}")
                    and not run_annotation(store, settings, f"worker-{index}")
                    and not run_trace(store, settings, objects)
                    and not run_one(store, settings, f"worker-{index}")):
                stop.wait(1)
    with ThreadPoolExecutor(max_workers=settings.worker_concurrency) as pool:
        futures = [pool.submit(loop, index) for index in range(settings.worker_concurrency)]
        for future in futures:
            future.result()
