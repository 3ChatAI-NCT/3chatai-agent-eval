"""PostgreSQL facts for datasets, evaluations, worker leases and review."""

from contextlib import nullcontext
from copy import deepcopy
from uuid import uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from golden_eval.adapters.jsonl_corpus import JsonlCorpusStore
from golden_eval.application.rubric_draft import align_turn_profile_line
from golden_eval.domain.models import GoldenProfileUpdate, GoldenSession, GoldenShared

from . import blind
from .execution_groups import GROUP_CTES, PROGRESS_SQL, with_progress
from .models import digest


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


# 评测的终态；SQL 片段里直接拼接这个常量
FINISHED = "('completed','failed','cancelled','uncertain')"


def definite_failure(error_code: str | None) -> bool:
    """重做结果也不会变的执行失败：候选接口明确拒绝（4xx，超时/冲突/限流除外）或请求不可回放。"""

    code = error_code or ""
    return code == "request_not_replayable" or (
        code.startswith("http_4") and code not in ("http_408", "http_409", "http_429"))


class Store:
    DISPLAY_STATUS = """CASE WHEN e.status='failed'
        AND EXISTS (SELECT 1 FROM eval_jobs j WHERE j.experiment_id=e.id
                    AND j.stage='judge' AND j.status='succeeded')
        AND EXISTS (SELECT 1 FROM eval_jobs j WHERE j.experiment_id=e.id
                    AND j.status='failed')
        THEN 'partial' ELSE e.status END"""
    EXECUTION_FAILED = """(SELECT count(*) FROM eval_jobs j
        WHERE j.experiment_id=e.id AND j.stage='execute' AND j.status='failed')"""
    # 未开启盲评或批次条目还没有评测时为 NULL；样本在评测结束后首次打开时才抽取，结束即视为待盲评
    BLIND_STATUS = f"""CASE WHEN NOT coalesce(e.manifest ? 'blind_review', false) THEN NULL
        ELSE coalesce((SELECT CASE WHEN b.revealed_at IS NULL THEN 'blind_open'
                                   WHEN b.skip_reason IS NULL THEN 'revealed' ELSE 'skipped' END
                       FROM eval_blind_reviews b WHERE b.experiment_id=e.id),
                      CASE WHEN e.status IN {FINISHED} THEN 'blind_open'
                           ELSE 'blind_waiting' END) END"""

    def __init__(self, dsn: str):
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=12, open=False, kwargs={"row_factory": dict_row}
        )

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    def ready(self) -> bool:
        """数据库已建表即可接流量。不比对具体版本：迁移只做向前兼容的改动，
        新旧版本代码都要能在新表结构上运行，发布与回滚才不会互相卡住。"""
        with self.pool.connection() as c:
            if c.execute("SELECT to_regclass('alembic_version') AS t").fetchone()["t"] is None:
                return False
            return c.execute("SELECT 1 FROM alembic_version LIMIT 1").fetchone() is not None

    def query(self, sql: str, params=()):
        with self.pool.connection() as c:
            return c.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        rows = self.query(sql, params)
        if not rows:
            raise NotFound("资源不存在")
        return rows[0]

    def spaces(self):
        return self.query("SELECT * FROM eval_spaces ORDER BY name,id")

    def space(self, space_id: str):
        return self.one("SELECT * FROM eval_spaces WHERE id=%s", (space_id,))

    def upsert_space(self, record: dict, subject: str):
        record = {"status": "active", "source_name": "", **record}
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO eval_spaces
                   (id,name,source_name,source_workspace_id,target_workspace_id,
                    registry_record_id,status)
                   VALUES (%(id)s,%(name)s,%(source_name)s,%(source_workspace_id)s,
                           %(target_workspace_id)s,%(id)s,%(status)s)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                    source_name=excluded.source_name,
                    source_workspace_id=excluded.source_workspace_id,
                    target_workspace_id=excluded.target_workspace_id,
                    registry_updated_at=now(),status=%(status)s
                   RETURNING *""",
                record,
            ).fetchone()
            self._audit(c, subject, record["id"], "space.synced", record["id"])
            return row

    def busy_tenants(self) -> set[str]:
        """有逐轮回放尚未结束的 EVAL 租户：执行中切换模型会让同一评测前后用上两个模型。"""

        return {row["target_workspace_id"] for row in self.query(
            """SELECT DISTINCT s.target_workspace_id FROM eval_experiments e
               JOIN eval_spaces s ON s.id=e.space_id
               WHERE e.mode='golden_regression' AND e.status IN ('queued','running')""")}

    def change_space_models(self, space_ids: list[str], apply, subject: str) -> list[dict]:
        """锁住空间行后确认没有进行中的逐轮回放，再由 apply 写网关并返回各空间的变更。

        Worker 领取任务时对空间行 SKIP LOCKED，切换期间不会有新轮次开始执行。
        """

        with self.pool.connection() as c:
            spaces = c.execute(
                "SELECT * FROM eval_spaces WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
                (space_ids,)).fetchall()
            if len(spaces) != len(set(space_ids)):
                raise NotFound("评测空间不存在")
            busy = c.execute(
                """SELECT DISTINCT s.name FROM eval_experiments e
                   JOIN eval_spaces s ON s.id=e.space_id
                   WHERE e.mode='golden_regression' AND e.status IN ('queued','running')
                     AND s.target_workspace_id=ANY(%s) ORDER BY s.name""",
                ([space["target_workspace_id"] for space in spaces],)).fetchall()
            if busy:
                raise Conflict("以下空间有逐轮回放尚未结束，暂不能切换执行模型："
                               + "、".join(row["name"] for row in busy))
            changes = apply(spaces)
            for change in changes:
                c.execute(
                    """INSERT INTO eval_space_model_changes
                       (space_id,tenant_id,from_model,previous_model,model,subject)
                       VALUES (%(space_id)s,%(tenant_id)s,%(from_model)s,%(previous_model)s,
                               %(model)s,%(subject)s)""", change | {"subject": subject})
                self._audit(c, subject, change["space_id"], "space.model_changed",
                            change["space_id"])
            return changes

    def space_model_changes(self, space_id: str, limit: int = 20):
        return self.query(
            """SELECT * FROM eval_space_model_changes WHERE space_id=%s
               ORDER BY created_at DESC,id DESC LIMIT %s""", (space_id, limit))

    def snapshots(self, space_id: str, include_archived: bool = False):
        return self.query(
            """SELECT * FROM eval_source_snapshots WHERE space_id=%s
               AND (%s OR archived_at IS NULL) ORDER BY created_at DESC""",
            (space_id, include_archived),
        )

    def snapshot(self, snapshot_id: str):
        return self.one("SELECT * FROM eval_source_snapshots WHERE id=%s", (snapshot_id,))

    def capture(self, capture_id: str):
        return self.one("SELECT * FROM eval_capture_jobs WHERE id=%s", (capture_id,))

    def captures(self, space_id: str, include_archived: bool = False):
        return self.query(
            """SELECT j.*, s.summary AS outcome FROM eval_capture_jobs j
               LEFT JOIN eval_source_snapshots s ON s.id=j.snapshot_id
               WHERE j.space_id=%s AND (%s OR j.archived_at IS NULL)
               ORDER BY j.created_at DESC""",
            (space_id, include_archived),
        )

    def create_capture(self, space_id: str, mode: str, name: str, spec: dict,
                       key: str, subject: str):
        capture_id = uid("capture")
        request_hash = digest({"mode": mode, "name": name, "spec": spec})
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO eval_capture_jobs
                   (id,space_id,mode,name,spec,request_sha256,idempotency_key,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(space_id,idempotency_key) DO NOTHING RETURNING id""",
                (capture_id, space_id, mode, name, Jsonb(spec), request_hash, key, subject),
            ).fetchone()
            if row is None:
                old = c.execute(
                    """SELECT id,request_sha256 FROM eval_capture_jobs
                       WHERE space_id=%s AND idempotency_key=%s""", (space_id, key),
                ).fetchone()
                if old["request_sha256"] != request_hash:
                    raise Conflict("幂等键已被另一采集请求使用")
                return self.capture(old["id"])
            self._audit(c, subject, space_id, "capture.created", capture_id)
        return self.capture(capture_id)

    def claim_capture(self, worker_id: str, lease_seconds: int):
        with self.pool.connection() as c:
            candidates = c.execute(
                """SELECT id,space_id FROM eval_capture_jobs WHERE status='queued'
                   ORDER BY created_at,id LIMIT 100"""
            ).fetchall()
            for candidate in candidates:
                job = c.execute(
                    """SELECT id,space_id FROM eval_capture_jobs WHERE id=%s
                       AND status='queued' FOR UPDATE SKIP LOCKED""",
                    (candidate["id"],),
                ).fetchone()
                if not job or not c.execute(
                    "SELECT id FROM eval_spaces WHERE id=%s FOR UPDATE SKIP LOCKED",
                    (job["space_id"],),
                ).fetchone():
                    continue
                active = c.execute(
                    """SELECT 1 FROM eval_capture_jobs WHERE space_id=%s
                       AND status='running' LIMIT 1""", (job["space_id"],),
                ).fetchone()
                if active:
                    continue
                return c.execute(
                    """UPDATE eval_capture_jobs SET status='running',worker_id=%s,
                       lease_token=%s,lease_until=now()+%s*interval '1 second',
                       attempt=attempt+1 WHERE id=%s RETURNING *""",
                    (worker_id, uid("lease"), lease_seconds, job["id"]),
                ).fetchone()
            return None

    def heartbeat_capture(self, job: dict, lease_seconds: int):
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_capture_jobs SET lease_until=now()+%s*interval '1 second'
                   WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now()
                   RETURNING id""", (lease_seconds, job["id"], job["lease_token"]),
            ).fetchone() is not None

    def finish_capture(self, job: dict, *, object_key: str | None = None,
                       content_sha256: str | None = None, source_hash: str | None = None,
                       summary: dict | None = None, dataset: dict | None = None,
                       error_code: str | None = None, error_message: str | None = None):
        snapshot_id = uid("snap") if not error_code else None
        dataset_id = uid("dataset") if dataset else None
        with self.pool.connection() as c:
            row = c.execute(
                """UPDATE eval_capture_jobs SET status=%s,error_code=%s,error_message=%s,
                   lease_until=NULL,finished_at=now()
                   WHERE id=%s AND status='running' AND lease_token=%s AND lease_until>now()
                   RETURNING *""", ("failed" if error_code else "succeeded", error_code,
                                    error_message, job["id"], job["lease_token"]),
            ).fetchone()
            if not row:
                return False
            if not error_code:
                c.execute(
                    """INSERT INTO eval_source_snapshots
                       (id,space_id,mode,object_key,content_sha256,source_hash,summary,created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (snapshot_id, job["space_id"], job["mode"], object_key,
                     content_sha256, source_hash, Jsonb(summary), job["created_by"]),
                )
                if dataset:
                    dataset["source_snapshot_id"] = snapshot_id
                    c.execute(
                        """INSERT INTO eval_dataset_versions
                           (id,space_id,source_snapshot_id,name,mode,content,content_sha256,
                            created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (dataset_id, job["space_id"], snapshot_id, dataset["name"],
                         dataset["mode"], Jsonb(dataset), digest(dataset), job["created_by"]),
                    )
                c.execute(
                    """UPDATE eval_capture_jobs SET snapshot_id=%s,dataset_version_id=%s
                       WHERE id=%s""", (snapshot_id, dataset_id, job["id"]),
                )
            self._audit(c, job["created_by"], job["space_id"],
                        "capture.failed" if error_code else "capture.completed", job["id"])
        return True

    def recover_captures(self, max_attempts: int = 3):
        with self.pool.connection() as c:
            # 反复中断的采集多半会把 worker 拖垮（如内存耗尽），不再重新入队，避免崩溃循环
            failed = c.execute(
                """UPDATE eval_capture_jobs SET status='failed',error_code='capture_interrupted',
                   error_message='采集多次中断未能完成，请缩短时间窗口后重试',
                   lease_token=NULL,lease_until=NULL,finished_at=now()
                   WHERE status='running' AND lease_until<=now() AND attempt>=%s
                   RETURNING id,space_id,created_by""", (max_attempts,),
            ).fetchall()
            for job in failed:
                self._audit(c, job["created_by"], job["space_id"], "capture.failed", job["id"])
            rows = c.execute(
                """UPDATE eval_capture_jobs SET status='queued',lease_token=NULL,
                   lease_until=NULL WHERE status='running' AND lease_until<=now()
                   RETURNING id"""
            ).fetchall()
            return len(rows)

    def create_draft(self, space_id: str, data: dict, subject: str):
        draft_id = uid("draft")
        with self.pool.connection() as c:
            snapshot_id = data.get("source_snapshot_id")
            if snapshot_id and not c.execute(
                """SELECT 1 FROM eval_source_snapshots WHERE id=%s AND space_id=%s
                   AND object_purged_at IS NULL FOR KEY SHARE""",
                (snapshot_id, space_id),
            ).fetchone():
                raise Conflict("来源快照与评测空间不匹配")
            c.execute(
                """INSERT INTO eval_dataset_drafts
                   (id,space_id,source_snapshot_id,name,mode,content,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (draft_id, space_id, snapshot_id, data["name"], data["mode"],
                 Jsonb(data), subject),
            )
            self._audit(c, subject, space_id, "draft.created", draft_id)
        return self.draft(draft_id)

    def draft(self, draft_id: str):
        return self.one("SELECT * FROM eval_dataset_drafts WHERE id=%s", (draft_id,))

    def drafts(self, space_id: str, include_archived: bool = False):
        return self.query(
            """SELECT * FROM eval_dataset_drafts WHERE space_id=%s
               AND (%s OR archived_at IS NULL) ORDER BY updated_at DESC""",
            (space_id, include_archived),
        )

    def annotation_jobs(self, draft_id: str):
        return self.query(
            """SELECT * FROM eval_annotation_jobs WHERE draft_id=%s
               ORDER BY created_at DESC""", (draft_id,),
        )

    def create_annotation(self, draft_id: str, session_id: str, profile_prompt: dict,
                          rubric_prompt: dict, model: str, key: str, subject: str):
        job_id = uid("annotation")
        with self.pool.connection() as c:
            draft = c.execute(
                "SELECT * FROM eval_dataset_drafts WHERE id=%s FOR UPDATE", (draft_id,)
            ).fetchone()
            if not draft or draft["status"] != "draft":
                raise Conflict("仅可对未发布草稿生成参考内容")
            item = next((item for item in draft["content"]["items"]
                         if item["session_id"] == session_id), None)
            if item is None:
                raise NotFound("案例不存在")
            existing = c.execute(
                """SELECT * FROM eval_annotation_jobs
                   WHERE draft_id=%s AND idempotency_key=%s""", (draft_id, key),
            ).fetchone()
            if existing:
                if existing["session_id"] != session_id:
                    raise Conflict("幂等键已被另一案例使用")
                return existing
            if c.execute(
                """SELECT 1 FROM eval_annotation_jobs WHERE draft_id=%s AND session_id=%s
                   AND status IN ('queued','running') LIMIT 1""", (draft_id, session_id),
            ).fetchone():
                raise Conflict("此案例已有正在生成的任务")
            return self._insert_annotation(c, draft, item, profile_prompt, rubric_prompt,
                                           model, key, subject, job_id)

    def _insert_annotation(self, c, draft, item, profile_prompt, rubric_prompt,
                           model, key, subject, job_id):
        row = c.execute(
                """INSERT INTO eval_annotation_jobs
                   (id,draft_id,session_id,case_sha256,shared_sha256,profile_prompt,rubric_prompt,
                    model,idempotency_key,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (job_id, draft["id"], item["session_id"], digest(item),
                 digest(draft["content"]["shared"]),
                 Jsonb(profile_prompt),
                 Jsonb(rubric_prompt), model, key, subject),
            ).fetchone()
        self._audit(c, subject, draft["space_id"], "annotation.created", job_id)
        return row

    def create_annotations(self, draft_id, profile_prompt, rubric_prompt, model, key, subject):
        result = {"jobs": [], "skipped_success": [], "skipped_active": []}
        with self.pool.connection() as c:
            draft = c.execute(
                "SELECT * FROM eval_dataset_drafts WHERE id=%s FOR UPDATE", (draft_id,),
            ).fetchone()
            if not draft or draft["status"] != "draft":
                raise Conflict("仅可对未发布草稿生成参考内容")
            jobs = c.execute(
                "SELECT * FROM eval_annotation_jobs WHERE draft_id=%s ORDER BY created_at DESC",
                (draft_id,),
            ).fetchall()
            by_key = {job["idempotency_key"]: job for job in jobs}
            latest = {}
            for job in jobs:
                latest.setdefault(job["session_id"], job)
            for item in draft["content"]["items"]:
                session_id = item["session_id"]
                case_key = digest({"batch_key": key, "session_id": session_id})
                if case_key in by_key:
                    result["jobs"].append(by_key[case_key])
                    continue
                previous = latest.get(session_id)
                if previous and previous["status"] == "succeeded":
                    result["skipped_success"].append(session_id)
                    continue
                if previous and previous["status"] in ("queued", "running"):
                    result["skipped_active"].append(session_id)
                    continue
                result["jobs"].append(self._insert_annotation(
                    c, draft, item, profile_prompt, rubric_prompt,
                    model, case_key, subject, uid("annotation"),
                ))
        return result

    def claim_annotation(self, worker_id: str, lease_seconds: int):
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_annotation_jobs SET status='running',worker_id=%s,
                   lease_token=%s,lease_until=now()+%s*interval '1 second',attempt=attempt+1
                   WHERE id=(SELECT id FROM eval_annotation_jobs WHERE status='queued'
                             ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING *""", (worker_id, uid("lease"), lease_seconds),
            ).fetchone()

    def heartbeat_annotation(self, job: dict, lease_seconds: int):
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_annotation_jobs SET lease_until=now()+%s*interval '1 second'
                   WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now()
                   RETURNING id""", (lease_seconds, job["id"], job["lease_token"]),
            ).fetchone() is not None

    def finish_annotation(self, job: dict, session: dict | None = None,
                          error_code: str | None = None, error_detail: dict | None = None):
        if session is None and error_code is None:
            raise ValueError("Annotation requires a result or error code")
        with self.pool.connection() as c:
            row = c.execute(
                """SELECT * FROM eval_annotation_jobs WHERE id=%s AND status='running'
                   AND lease_token=%s AND lease_until>now() FOR UPDATE""",
                (job["id"], job["lease_token"]),
            ).fetchone()
            if not row:
                return False
            draft = c.execute(
                "SELECT * FROM eval_dataset_drafts WHERE id=%s FOR UPDATE", (row["draft_id"],)
            ).fetchone()
            if session is not None and error_code is None:
                items = list(draft["content"]["items"])
                index = next((i for i, item in enumerate(items)
                              if item["session_id"] == row["session_id"]), None)
                if (draft["status"] != "draft" or index is None
                        or digest(items[index]) != row["case_sha256"]
                        or digest(draft["content"]["shared"]) != row["shared_sha256"]):
                    error_code = "draft_changed"
                else:
                    items[index] = session
                    content = {**draft["content"], "items": items}
                    review_state = dict(draft["review_state"])
                    review_state.pop(row["session_id"], None)
                    c.execute(
                        """UPDATE eval_dataset_drafts SET content=%s,review_state=%s,
                           revision=revision+1,updated_at=now() WHERE id=%s""",
                        (Jsonb(content), Jsonb(review_state), row["draft_id"]),
                    )
            status = "failed" if error_code else "succeeded"
            c.execute(
                """UPDATE eval_annotation_jobs SET status=%s,error_code=%s,error_detail=%s,
                   lease_until=NULL,finished_at=now() WHERE id=%s""",
                (status, error_code, Jsonb(error_detail) if error_detail else None, row["id"]),
            )
            self._audit(c, row["created_by"], draft["space_id"],
                        "annotation." + status, row["id"])
            return True

    def recover_annotations(self):
        with self.pool.connection() as c:
            rows = c.execute(
                """UPDATE eval_annotation_jobs SET status='failed',error_code='lease_expired',
                   lease_token=NULL,lease_until=NULL,finished_at=now()
                   WHERE status='running' AND lease_until<=now() RETURNING id"""
            ).fetchall()
            return len(rows)

    def derive_draft(self, version_id: str, subject: str):
        draft_id = uid("draft")
        with self.pool.connection() as c:
            version = c.execute(
                "SELECT * FROM eval_dataset_versions WHERE id=%s", (version_id,)
            ).fetchone()
            if not version:
                raise NotFound("数据集版本不存在")
            if version["mode"] != "golden_regression":
                raise Conflict("只有金标版本可以派生修订草稿")
            if version["content_purged_at"]:
                raise Conflict("数据集内容已按保留期限清理")
            data = dict(version["content"])
            data["name"] = version["name"] + " · 修订"
            c.execute(
                """INSERT INTO eval_dataset_drafts
                   (id,space_id,source_snapshot_id,derived_from_version_id,
                    name,mode,content,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (draft_id, version["space_id"], version["source_snapshot_id"],
                 version_id, data["name"], version["mode"], Jsonb(data), subject),
            )
            self._audit(c, subject, version["space_id"], "draft.derived", draft_id)
        return self.draft(draft_id)

    def pending_drafts(self):
        return self.query(
            """SELECT * FROM eval_dataset_drafts WHERE status='draft' AND archived_at IS NULL
               ORDER BY updated_at DESC LIMIT 30"""
        )

    def replace_draft(self, draft_id: str, data: dict, revision: int, subject: str):
        with self.pool.connection() as c:
            current = c.execute(
                "SELECT space_id FROM eval_dataset_drafts WHERE id=%s", (draft_id,)
            ).fetchone()
            snapshot_id = data.get("source_snapshot_id")
            if snapshot_id and (not current or not c.execute(
                """SELECT 1 FROM eval_source_snapshots WHERE id=%s AND space_id=%s
                   AND object_purged_at IS NULL FOR KEY SHARE""",
                (snapshot_id, current["space_id"]),
            ).fetchone()):
                raise Conflict("来源快照与评测空间不匹配")
            row = c.execute(
                """UPDATE eval_dataset_drafts SET name=%s,content=%s,
                   source_snapshot_id=%s,review_state='{}'::jsonb,
                   revision=revision+1,updated_at=now()
                   WHERE id=%s AND revision=%s AND status='draft' RETURNING *""",
                (data["name"], Jsonb(data), data.get("source_snapshot_id"),
                 draft_id, revision),
            ).fetchone()
            if not row:
                raise Conflict("草稿已变更或已发布，请刷新")
            self._audit(c, subject, row["space_id"], "draft.updated", draft_id)
            return row

    def review_draft_case(self, draft_id: str, case_id: str, revision: int, subject: str):
        with self.pool.connection() as c:
            draft = c.execute(
                "SELECT * FROM eval_dataset_drafts WHERE id=%s FOR UPDATE", (draft_id,)
            ).fetchone()
            if not draft:
                raise NotFound("草稿不存在")
            if draft["status"] != "draft" or draft["revision"] != revision:
                raise Conflict("草稿已变更或已发布，请刷新")
            case = next(
                (item for item in draft["content"]["items"] if item.get("session_id") == case_id),
                None,
            )
            if case is None:
                raise NotFound("案例不存在")
            state = dict(draft["review_state"])
            state[case_id] = {"sha256": digest(case), "reviewer": subject}
            row = c.execute(
                """UPDATE eval_dataset_drafts SET review_state=%s,revision=revision+1,
                   updated_at=now() WHERE id=%s RETURNING *""",
                (Jsonb(state), draft_id),
            ).fetchone()
            self._audit(c, subject, draft["space_id"], "draft.case_reviewed", case_id)
            return row

    def edit_draft_case(self, draft_id: str, case_id: str, revision: int,
                        patch: dict, subject: str):
        with self.pool.connection() as c:
            draft = c.execute(
                "SELECT * FROM eval_dataset_drafts WHERE id=%s FOR UPDATE", (draft_id,)
            ).fetchone()
            if not draft:
                raise NotFound("草稿不存在")
            if draft["status"] != "draft" or draft["revision"] != revision:
                raise Conflict("草稿已变更或已发布，请刷新")
            content = dict(draft["content"])
            items = [dict(item) for item in content["items"]]
            index = next((n for n, item in enumerate(items)
                          if item.get("session_id") == case_id), None)
            if index is None:
                raise NotFound("案例不存在")
            session = deepcopy(items[index])
            if patch["turn_index"] >= len(session["turns"]):
                raise NotFound("评测轮次不存在")
            turn = session["turns"][patch["turn_index"]]
            expected = dict(turn["expected"])
            if len(expected["messages"]) != len(patch["expected_messages"]):
                raise Conflict("预期回复条数已变化，请刷新")
            expected["messages"] = [dict(message, content=text) if text is not None else message
                                    for message, text in zip(expected["messages"],
                                                             patch["expected_messages"],
                                                             strict=True)]
            if patch.get("expected_profile_update") is not None:
                original_updates = expected["profile_update"]
                expected["profile_update"] = patch["expected_profile_update"]
                previous = dict(session["input"]["memory_before"]["profile_snapshot"])
                for turn_index, current in enumerate(session["turns"]):
                    current_expected = dict(current["expected"])
                    raw_updates = (expected if turn_index == patch["turn_index"]
                                   else current_expected)["profile_update"]
                    updates = {}
                    for key, raw in raw_updates.items():
                        try:
                            update = GoldenProfileUpdate.model_validate(raw).model_dump(mode="json")
                        except ValidationError as exc:
                            raise Conflict(f"画像字段 {key} 的格式无效") from exc
                        if turn_index == patch["turn_index"] and raw != original_updates.get(key):
                            update["source"] = "manual"
                        prior = previous.get(key)
                        update["updated_from"] = None if prior in (None, "", []) else prior
                        updates[key] = update
                    current_expected["profile_update"] = updates
                    previous = {**previous, **{key: value["value"]
                                               for key, value in updates.items()}}
                    current_expected["profile_snapshot"] = previous
                    session["turns"][turn_index] = dict(current, expected=current_expected)
                session["expected"]["memory_after"]["profile_snapshot"] = previous
                expected = session["turns"][patch["turn_index"]]["expected"]
            turn = dict(turn, expected=expected, rubric=patch["rubric"])
            if patch.get("expected_profile_update") is not None:
                turn["rubric"] = align_turn_profile_line(
                    turn["rubric"], sorted(expected["profile_update"])
                )
            session["turns"] = [*session["turns"][:patch["turn_index"]], turn,
                                *session["turns"][patch["turn_index"] + 1:]]
            try:
                JsonlCorpusStore._validate_shared_contract(
                    GoldenShared.model_validate(content["shared"]),
                    GoldenSession.model_validate(session),
                )
            except (ValidationError, ValueError) as exc:
                raise Conflict("修改后的金标案例无效，请核对画像递推与回复格式") from exc
            items[index] = session
            content["items"] = items
            state = dict(draft["review_state"])
            state.pop(case_id, None)
            row = c.execute(
                """UPDATE eval_dataset_drafts SET content=%s,review_state=%s,
                   revision=revision+1,updated_at=now() WHERE id=%s RETURNING *""",
                (Jsonb(content), Jsonb(state), draft_id),
            ).fetchone()
            self._audit(c, subject, draft["space_id"], "draft.case_edited", case_id)
            return row

    def publish_draft(self, draft_id: str, revision: int, subject: str):
        version_id = uid("dataset")
        with self.pool.connection() as c:
            draft = c.execute(
                "SELECT * FROM eval_dataset_drafts WHERE id=%s FOR UPDATE", (draft_id,)
            ).fetchone()
            if not draft:
                raise NotFound("草稿不存在")
            if draft["status"] != "draft" or draft["revision"] != revision:
                raise Conflict("草稿已变更或已发布，请刷新")
            if draft["mode"] != "golden_regression":
                raise Conflict("此发布入口只处理金标草稿")
            for item in draft["content"]["items"]:
                review = draft["review_state"].get(item["session_id"])
                if not review or review["sha256"] != digest(item):
                    raise Conflict(f"案例尚未审核: {item['session_id']}")
            c.execute(
                """INSERT INTO eval_dataset_versions
                   (id,space_id,source_draft_id,source_snapshot_id,derived_from_version_id,
                    name,mode,content,content_sha256,
                    review_state,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (version_id, draft["space_id"], draft_id, draft["source_snapshot_id"],
                 draft["derived_from_version_id"], draft["name"], draft["mode"],
                 Jsonb(draft["content"]), digest(draft["content"]),
                 Jsonb(draft["review_state"]), subject),
            )
            c.execute(
                """UPDATE eval_dataset_drafts SET status='published',
                   content='{"purged":true}'::jsonb,updated_at=now() WHERE id=%s""",
                (draft_id,),
            )
            self._audit(c, subject, draft["space_id"], "dataset.published", version_id)
        return self.dataset(version_id)

    def publish_atomic(self, space_id: str, data: dict, subject: str):
        version_id = uid("dataset")
        with self.pool.connection() as c:
            snapshot_id = data.get("source_snapshot_id")
            if snapshot_id and not c.execute(
                """SELECT 1 FROM eval_source_snapshots WHERE id=%s AND space_id=%s
                   AND object_purged_at IS NULL FOR KEY SHARE""",
                (snapshot_id, space_id),
            ).fetchone():
                raise Conflict("来源快照与评测空间不匹配")
            c.execute(
                """INSERT INTO eval_dataset_versions
                   (id,space_id,source_snapshot_id,name,mode,content,content_sha256,
                    created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (version_id, space_id, snapshot_id, data["name"], data["mode"],
                 Jsonb(data), digest(data), subject),
            )
            self._audit(c, subject, space_id, "dataset.published", version_id)
        return self.dataset(version_id)

    def dataset(self, version_id: str):
        return self.one("SELECT * FROM eval_dataset_versions WHERE id=%s", (version_id,))

    def dataset_hash(self, version_id: str) -> dict:
        return self.one("SELECT content_sha256 FROM eval_dataset_versions WHERE id=%s",
                        (version_id,))

    def datasets(self, space_id: str, include_archived: bool = False):
        return self.query(
            """SELECT * FROM eval_dataset_versions WHERE space_id=%s
               AND (%s OR archived_at IS NULL) ORDER BY created_at DESC""",
            (space_id, include_archived),
        )

    def submit(self, space: dict, request: dict, key: str, subject: str,
               manifest: dict, tasks: list[tuple], candidate_api_key: str | None = None,
               *, _connection=None):
        run_id = uid("exp")
        request_hash = digest(request)
        with nullcontext(_connection) if _connection is not None else self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO eval_experiments
                   (id,space_id,dataset_version_id,name,mode,manifest,request_sha256,
                    idempotency_key,created_by,candidate_api_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(space_id,idempotency_key) DO NOTHING RETURNING id""",
                (run_id, space["id"], request["dataset_version_id"], request["name"],
                 manifest["mode"], Jsonb(manifest), request_hash, key, subject,
                 candidate_api_key),
            ).fetchone()
            if row is None:
                old = c.execute(
                    """SELECT id,request_sha256 FROM eval_experiments
                       WHERE space_id=%s AND idempotency_key=%s""",
                    (space["id"], key),
                ).fetchone()
                if old["request_sha256"] != request_hash:
                    raise Conflict("幂等键已被另一请求使用")
                return old["id"]
            for case_id, repeat_index, payload in tasks:
                c.execute(
                    """INSERT INTO eval_jobs
                       (id,experiment_id,case_id,repeat_index,stage,input)
                       VALUES (%s,%s,%s,%s,'execute',%s)""",
                    (uid("job"), run_id, case_id, repeat_index, Jsonb(payload)),
                )
            self._event(c, run_id, "experiment.created", {"tasks": len(tasks)})
            self._audit(c, subject, space["id"], "experiment.created", run_id)
        return run_id

    def submit_regrade(self, space: dict, request: dict, key: str, subject: str,
                       manifest: dict, tasks: list[tuple], source_run: dict,
                       source_jobs: list[dict]):
        from .engines import execution_basis

        golden = manifest["mode"] == "golden_regression"
        if source_run["space_id"] != space["id"] or source_run["mode"] != manifest["mode"]:
            raise Conflict("只能复用同一空间、同一策略的执行结果")
        # 判分失败不影响复用：下面逐条要求执行成功且执行证据完整
        if source_run["status"] not in ("completed", "failed", "uncertain"):
            raise Conflict("来源评测尚未结束")
        if source_run["evidence_purged_at"]:
            raise Conflict("来源执行证据已过保留期，需要重新执行")
        if source_run["manifest"]["target_config_sha256"] != manifest["target_config_sha256"]:
            raise Conflict("EVAL 连接配置已变化，需要重新执行")
        if source_run["manifest"]["config"]["execution_target"] != request["execution_target"]:
            raise Conflict("执行目标不同，需要重新执行")
        previous = {(job["case_id"], job["repeat_index"]): job for job in source_jobs
                    if job["stage"] == "execute"}
        observations = {(job["case_id"], job["repeat_index"]): job for job in source_jobs
                        if job["stage"] == "judge"}
        planned = {(case_id, repeat_index): payload for case_id, repeat_index, payload in tasks}
        if previous.keys() != planned.keys():
            raise Conflict("案例或重复次数已改变，需要重新执行")
        basis = execution_basis if golden else (lambda task: digest(task["item"]))
        for identity, payload in planned.items():
            old = previous[identity]
            if old["input"].get("purged"):
                raise Conflict("来源评测有执行证据缺失，需要重新执行")
            if basis(old["input"]) != basis(payload):
                raise Conflict(f"案例 {identity[0]} 的执行输入已改变，需要重新执行")
            # 确定的执行失败（如候选接口返回 413）重做也一样，原样带入；
            # 限流、服务端错误等暂时性失败与结果未知的必须重新执行
            if old["status"] == "failed" and definite_failure(old["error_code"]):
                continue
            if (old["status"] != "succeeded" or old["result"] is None
                    or identity not in observations
                    or observations[identity]["input"].get("purged")):
                raise Conflict("来源评测有执行证据缺失，需要重新执行")
        run_id = uid("exp")
        request_hash = digest(request)
        manifest = {**manifest, "source_experiment_id": source_run["id"],
                    "execution_reused": True}
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO eval_experiments
                   (id,space_id,dataset_version_id,source_experiment_id,name,mode,
                    manifest,request_sha256,idempotency_key,created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(space_id,idempotency_key) DO NOTHING RETURNING id""",
                (run_id, space["id"], request["dataset_version_id"], source_run["id"],
                 request["name"], manifest["mode"], Jsonb(manifest), request_hash, key,
                 subject),
            ).fetchone()
            if row is None:
                old = c.execute(
                    """SELECT id,request_sha256 FROM eval_experiments
                       WHERE space_id=%s AND idempotency_key=%s""", (space["id"], key),
                ).fetchone()
                if old["request_sha256"] != request_hash:
                    raise Conflict("幂等键已被另一请求使用")
                return old["id"]
            for identity, payload in planned.items():
                old = previous[identity]
                if old["status"] == "failed" and definite_failure(old["error_code"]):
                    c.execute(
                        """INSERT INTO eval_jobs
                           (id,experiment_id,source_job_id,case_id,repeat_index,stage,status,
                            input,error_code,runtime_engine_sha256,finished_at)
                           VALUES (%s,%s,%s,%s,%s,'execute','failed',%s,%s,%s,now())""",
                        (uid("job"), run_id, old["id"], identity[0], identity[1],
                         Jsonb(payload), old["error_code"], old["runtime_engine_sha256"]),
                    )
                    continue
                copied_id = uid("job")
                copied_result = {**old["result"], "execution_source_sha256": old["result_sha256"]}
                if golden:
                    copied_result["baseline"] = payload["turn"]["expected"]["messages"]
                c.execute(
                    """INSERT INTO eval_jobs
                       (id,experiment_id,source_job_id,case_id,repeat_index,stage,status,
                        input,result,result_sha256,runtime_engine_sha256,finished_at)
                       VALUES (%s,%s,%s,%s,%s,'execute','succeeded',%s,%s,%s,%s,now())""",
                    (copied_id, run_id, old["id"], identity[0], identity[1],
                     Jsonb(payload), Jsonb(copied_result), digest(copied_result),
                     old["runtime_engine_sha256"]),
                )
                evidence = "observation" if golden else "response"
                judge_input = {**payload, evidence: observations[identity]["input"][evidence]}
                c.execute(
                    """INSERT INTO eval_jobs
                       (id,experiment_id,case_id,repeat_index,stage,input)
                       VALUES (%s,%s,%s,%s,'judge',%s)""",
                    (uid("job"), run_id, identity[0], identity[1], Jsonb(judge_input)),
                )
            self._event(c, run_id, "experiment.regrade_created",
                        {"source_experiment_id": source_run["id"], "tasks": len(tasks)})
            # 全部是带入的失败项时没有任务可跑，需要立即结算状态
            if all(previous[identity]["status"] == "failed" for identity in planned):
                self._settle(c, run_id)
            self._audit(c, subject, space["id"], "experiment.regrade_created", run_id)
        return run_id

    # 永久删除只针对已归档、无人复核、也没被复用的已结束记录
    DELETABLE = f"""e.archived_at IS NOT NULL
        AND e.status IN {FINISHED}
        AND NOT EXISTS (SELECT 1 FROM eval_reviews r JOIN eval_jobs j ON j.id=r.job_id
                        WHERE j.experiment_id=e.id)
        AND NOT EXISTS (SELECT 1 FROM eval_experiments x WHERE x.source_experiment_id=e.id)
        AND NOT EXISTS (SELECT 1 FROM eval_experiments x
            WHERE x.manifest#>>'{{execution_continuation,parent_experiment_id}}'=e.id)
        AND e.manifest#>>'{{execution_continuation,parent_experiment_id}}' IS NULL"""

    def experiments(self, space_id: str | None = None, include_archived: bool = False):
        rows = self.query(
            f"""SELECT e.*,{self.DELETABLE} AS deletable,
                       {self.DISPLAY_STATUS} AS display_status,
                       {self.EXECUTION_FAILED} AS execution_failed,
                       {self.BLIND_STATUS} AS blind_status
                FROM eval_experiments e
                WHERE (%s OR e.archived_at IS NULL) AND (%s::text IS NULL OR e.space_id=%s)
                  AND e.manifest#>>'{{execution_continuation,parent_experiment_id}}' IS NULL
                ORDER BY e.created_at DESC""", (include_archived, space_id, space_id),
        )
        progress = {row["logical_id"]: row for row in self.query(
            PROGRESS_SQL, ([row["id"] for row in rows],))} if rows else {}
        return [with_progress(row, progress.get(row["id"])) for row in rows]

    def deletable(self, run_id: str) -> bool:
        return self.one(f"SELECT {self.DELETABLE} AS ok FROM eval_experiments e WHERE e.id=%s",
                        (run_id,))["ok"]

    def experiment(self, run_id: str):
        row = self.one(
            f"""SELECT e.*,{self.DISPLAY_STATUS} AS display_status,
                       {self.EXECUTION_FAILED} AS execution_failed,
                       {self.BLIND_STATUS} AS blind_status
                FROM eval_experiments e WHERE e.id=%s""", (run_id,)
        )
        progress = self.query(PROGRESS_SQL, ([run_id],))
        return with_progress(row, progress[0] if progress else None)

    def execution_records(self, run_id: str):
        return self.query(
            f"""WITH {GROUP_CTES} SELECT e.id,e.name,e.status,e.created_at
                FROM group_runs g JOIN eval_experiments e ON e.id=g.run_id
                ORDER BY g.continuation,g.created_at,e.id""", ([run_id],),
        )

    def link_execution(self, parent_id: str, child_id: str, subject: str):
        """关联相同冻结输入的后续执行；任务和结果不移动、不覆盖。"""

        with self.pool.connection() as c:
            rows = c.execute(
                "SELECT * FROM eval_experiments WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
                ([parent_id, child_id],),
            ).fetchall()
            found = {row["id"]: row for row in rows}
            if parent_id == child_id or len(found) != 2:
                raise Conflict("需要两条不同的评测记录")
            parent, child = found[parent_id], found[child_id]
            existing = child["manifest"].get("execution_continuation")
            if existing:
                if existing["parent_experiment_id"] == parent_id:
                    return
                raise Conflict("执行已经关联到其他评测")
            if parent["manifest"].get("execution_continuation") or child["source_experiment_id"]:
                raise Conflict("不能将重新评分版本或已有分组嵌套为后续执行")
            if (parent["mode"] != "golden_regression" or any(
                parent[key] != child[key] for key in ("space_id", "dataset_version_id", "mode")
            ) or any(parent["manifest"].get(key) != child["manifest"].get(key)
                     for key in ("target_config_sha256", "judge_prompt_sha256", "judge_model"))
                    or any(parent["manifest"]["config"].get(key)
                           != child["manifest"]["config"].get(key)
                           for key in ("execution_target", "repeats", "space", "profile_readback"))
                    or any(row["manifest"].get("blind_review") for row in rows)):
                raise Conflict("空间、数据集、执行条件或评分方案不一致，不能合并")
            invalid = c.execute(
                """SELECT n.id FROM eval_jobs n LEFT JOIN eval_jobs old
                   ON old.experiment_id=%s AND old.stage='execute'
                   AND old.case_id=n.case_id AND old.repeat_index=n.repeat_index
                   WHERE n.experiment_id=%s AND n.stage='execute' AND (
                     old.id IS NULL OR old.status IN ('queued','running')
                     OR (n.input-'config') IS DISTINCT FROM (old.input-'config')) LIMIT 1""",
                (parent_id, child_id),
            ).fetchone()
            if invalid or not c.execute(
                "SELECT 1 FROM eval_jobs WHERE experiment_id=%s AND stage='execute' LIMIT 1",
                (child_id,),
            ).fetchone():
                raise Conflict("后续执行必须对应已结束且冻结输入相同的原执行槽位")
            if c.execute(
                "SELECT 1 FROM eval_experiments WHERE manifest#>>"
                "'{execution_continuation,parent_experiment_id}'=%s LIMIT 1", (child_id,),
            ).fetchone():
                raise Conflict("不能嵌套执行分组")
            meta = {"parent_experiment_id": parent_id, "selection": "latest_linked_execution",
                    "original_evidence_preserved": True}
            c.execute("UPDATE eval_experiments SET manifest=jsonb_set(manifest,"
                      "'{execution_continuation}',%s) WHERE id=%s", (Jsonb(meta), child_id))
            c.execute("UPDATE eval_experiments SET review_status=CASE WHEN review_status="
                      "'completed' THEN 'in_progress' ELSE review_status END WHERE id=%s",
                      (parent_id,))
            self._event(c, parent_id, "experiment.execution_linked", {**meta, "child_id": child_id})
            self._audit(c, subject, parent["space_id"], "experiment.execution_linked", child_id)
            if not c.execute(f"WITH {GROUP_CTES} SELECT 1 FROM logical_jobs "
                             "WHERE status IN ('queued','running') LIMIT 1",
                             ([parent_id],)).fetchone():
                self._queue_report(c, parent_id)

    def jobs(self, run_id: str):
        rows = self.query(
            f"""WITH {GROUP_CTES} SELECT * FROM logical_jobs
                ORDER BY case_id,repeat_index,stage""", ([run_id],),
        )
        for row in rows:
            row.pop("logical_id", None)
        return rows

    def report_jobs(self, run_id: str):
        """报告只读取汇总需要的输入；完整逐条证据与重新判分仍使用原始任务。"""

        return self.query(
            f"""WITH {GROUP_CTES}
               SELECT id,experiment_id,case_id,repeat_index,stage,status,result,result_sha256,
                      runtime_engine_sha256,error_code,worker_id,lease_token,lease_until,
                      attempt,created_at,finished_at,source_job_id,
                      CASE WHEN input->>'mode'='golden_regression' THEN
                        jsonb_strip_nulls(jsonb_build_object('mode',input->'mode',
                            'config',input->'config','observation',input->'observation'))
                        ELSE input END AS input
               FROM logical_jobs ORDER BY case_id,repeat_index,stage""",
            ([run_id],),
        )

    def job_counts(self, run_id: str) -> dict:
        return self.one(
            f"""WITH {GROUP_CTES} SELECT count(*) FILTER (WHERE stage='execute') AS planned,
                      count(*) FILTER (WHERE stage='execute' AND status='succeeded') AS executed,
                      count(*) FILTER (WHERE stage='execute' AND status='failed')
                          AS execution_failed,
                      count(*) FILTER (WHERE stage='execute' AND status='uncertain') AS uncertain,
                      count(*) FILTER (WHERE stage='judge' AND status='succeeded') AS judged,
                      count(*) FILTER (WHERE stage='judge' AND status IN ('failed','uncertain'))
                          AS judge_failed,
                      count(DISTINCT case_id) FILTER (WHERE stage='execute') AS case_count
               FROM logical_jobs""", ([run_id],),
        )

    def bundle_job_index(self, run_id: str) -> list[dict]:
        """证据包只预读小索引，包含原执行、关联执行和递归复用来源。"""

        return self.query(
            f"""WITH RECURSIVE {GROUP_CTES}, related AS (
                  SELECT j.id,j.source_job_id FROM eval_jobs j
                  JOIN group_runs g ON g.run_id=j.experiment_id
                  UNION
                  SELECT j.id,j.source_job_id FROM eval_jobs j
                  JOIN related r ON j.id=r.source_job_id)
                SELECT j.id,j.experiment_id,j.case_id,j.repeat_index,j.stage,j.status,j.attempt,
                       j.source_job_id,j.result_sha256,j.error_code,j.created_at,j.finished_at,
                       j.runtime_engine_sha256,
                       EXISTS(SELECT 1 FROM logical_jobs l WHERE l.id=j.id) AS selected_for_report
                FROM related r JOIN eval_jobs j ON j.id=r.id
                ORDER BY j.case_id,j.repeat_index,j.experiment_id,j.stage""", ([run_id],),
        )

    def bundle_job(self, job_id: str) -> dict:
        return self.one(
            """SELECT j.*,e.evidence_purged_at FROM eval_jobs j
               JOIN eval_experiments e ON e.id=j.experiment_id WHERE j.id=%s""", (job_id,),
        )

    def bundle_traces(self, job_ids: list[str]) -> list[dict]:
        return self.query(
            "SELECT * FROM eval_turn_traces WHERE job_id=ANY(%s::text[]) "
            "ORDER BY job_id,attempt,id", (job_ids,),
        )

    def bundle_dataset(self, version_id: str) -> dict:
        return self.one(
            """SELECT id,name,mode,space_id,source_snapshot_id,content_sha256,created_at,
                      content_purged_at,review_state,content->'shared' AS shared
               FROM eval_dataset_versions WHERE id=%s""", (version_id,),
        )

    def blind_draw_jobs(self, run_id: str) -> list[dict]:
        """抽样只读取任务状态与风险标签，不传输完整请求和响应。"""

        return self.query(
            """SELECT id,case_id,repeat_index,stage,status,
                      CASE WHEN stage='judge' THEN jsonb_build_object(
                          'verdict',result->'verdict','worst',result->'worst')
                      ELSE NULL END AS result
               FROM eval_jobs WHERE experiment_id=%s
               ORDER BY case_id,repeat_index,stage""", (run_id,),
        )

    def blind_sample_jobs(self, run_id: str, sample: list[dict]) -> list[dict]:
        """已冻结样本只取对应的执行和判分任务。"""

        if not sample:
            return []
        return self.query(
            """SELECT job.* FROM eval_jobs selected
               JOIN eval_jobs job ON job.experiment_id=selected.experiment_id
                   AND job.case_id=selected.case_id
                   AND job.repeat_index=selected.repeat_index
               WHERE selected.experiment_id=%s AND selected.id=ANY(%s::text[])
               ORDER BY job.case_id,job.repeat_index,job.stage""",
            (run_id, [item["job_id"] for item in sample]),
        )

    def evidence_jobs(self, run_id: str, job_id: str):
        """一条执行结果及其对应的评审任务，只取这两行，避免读出整批完整请求。"""

        job = self.one(
            f"""WITH {GROUP_CTES} SELECT j.*,e.evidence_purged_at FROM logical_jobs j
               JOIN eval_experiments e ON e.id=j.experiment_id
               WHERE j.id=%s""", ([run_id], job_id),
        )
        judge = self.query(
            """SELECT input FROM eval_jobs WHERE experiment_id=%s AND case_id=%s
               AND repeat_index=%s AND stage='judge'""",
            (job["experiment_id"], job["case_id"], job["repeat_index"]),
        )
        return job, judge[0] if judge else None

    def create_batch(self, name: str, spec: dict, space_ids: list[str], key: str,
                     subject: str) -> str:
        """一个事务里建批次、各空间的采集任务与条目；同一幂等键重复提交返回原批次。"""

        batch_id = uid("batch")
        mode = spec.get("mode", "atomic_replay")
        capture_spec = ({"policy": {"start": spec["start"], "end": spec["end"],
                         "quota_per_workspace": spec["size"], "min_turns": spec["min_turns"]}}
                        if mode == "golden_regression" else
                        {field: spec[field] for field in ("start", "end", "size")} | {
                            "model": None, "terms": []})
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO eval_batches(id,name,spec,idempotency_key,created_by)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING
                   RETURNING id""", (batch_id, name, Jsonb(spec), key, subject),
            ).fetchone()
            if row is None:
                old = c.execute("SELECT id,name,spec FROM eval_batches WHERE idempotency_key=%s",
                                (key,)).fetchone()
                old_spaces = {row["space_id"] for row in c.execute(
                    "SELECT space_id FROM eval_batch_items WHERE batch_id=%s", (old["id"],))}
                if old["name"] != name or old["spec"] != spec or old_spaces != set(space_ids):
                    raise Conflict("幂等键已被另一请求使用")
                return old["id"]
            spaces = {row["id"]: row for row in c.execute(
                "SELECT id,status FROM eval_spaces WHERE id=ANY(%s)", (space_ids,)).fetchall()}
            for space_id in space_ids:
                if spaces.get(space_id, {}).get("status") != "active":
                    raise Conflict(f"评测空间 {space_id} 不存在或尚未验证可用")
                capture_id = uid("capture")
                c.execute(
                    """INSERT INTO eval_capture_jobs
                       (id,space_id,mode,name,spec,request_sha256,idempotency_key,created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (capture_id, space_id, mode, name, Jsonb(capture_spec),
                     digest({"mode": mode, "name": name, "spec": capture_spec}),
                     f"batch:{batch_id}", subject),
                )
                c.execute(
                    """INSERT INTO eval_batch_items(id,batch_id,space_id,capture_id)
                       VALUES (%s,%s,%s,%s)""", (uid("item"), batch_id, space_id, capture_id),
                )
                self._audit(c, subject, space_id, "capture.created", capture_id)
            self._audit(c, subject, None, "batch.created", batch_id)
        return batch_id

    def create_replay_batch(self, name, spec, prepared, key, subject):
        """整批原子提交：复用采集记录，新建全部评测与执行任务，不触发重新采样。"""

        batch_id = uid("batch")
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO eval_batches(id,name,spec,idempotency_key,created_by)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING
                   RETURNING id""", (batch_id, name, Jsonb(spec), key, subject),
            ).fetchone()
            if row is None:
                old = c.execute("SELECT id,name,spec FROM eval_batches WHERE idempotency_key=%s",
                                (key,)).fetchone()
                if old["name"] != name or old["spec"] != spec:
                    raise Conflict("幂等键已被另一请求使用")
                return old["id"]
            for item, space, request, manifest, tasks, sealed_key in prepared:
                run_id = self.submit(space, request, f"batch:{batch_id}", subject,
                                     manifest, tasks, sealed_key, _connection=c)
                c.execute(
                    """INSERT INTO eval_batch_items
                       (id,batch_id,space_id,capture_id,experiment_id)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (uid("item"), batch_id, space["id"], item["capture_id"], run_id),
                )
            self._audit(c, subject, None, "batch.replayed", batch_id)
        return batch_id

    def pending_batch_items(self, limit: int = 10):
        """采集已成功、还没创建评测也没记录失败原因的条目。"""

        return self.query(
            """SELECT i.*,b.name AS batch_name,b.spec,b.created_by,s.name AS space_name,
                      c.dataset_version_id,c.snapshot_id
               FROM eval_batch_items i JOIN eval_batches b ON b.id=i.batch_id
               JOIN eval_capture_jobs c ON c.id=i.capture_id
               JOIN eval_spaces s ON s.id=i.space_id
               WHERE i.experiment_id IS NULL AND i.error_message IS NULL
                 AND c.status='succeeded' AND (c.dataset_version_id IS NOT NULL
                      OR b.spec->>'mode'='golden_regression')
               ORDER BY b.created_at LIMIT %s""", (limit,),
        )

    def freeze_reference_capture(self, capture_id: str, data: dict, subject: str):
        """冻结机器整理的参考数据；不写入人工审核记录，并发重做复用同一版本。"""

        with self.pool.connection() as c:
            job = c.execute("SELECT * FROM eval_capture_jobs WHERE id=%s FOR UPDATE",
                            (capture_id,)).fetchone()
            if not job or job["mode"] != "golden_regression" or job["status"] != "succeeded":
                raise Conflict("仅成功的逐轮采集可冻结参考数据")
            if job["dataset_version_id"]:
                return job["dataset_version_id"]
            if data.get("source_snapshot_id") != job["snapshot_id"]:
                raise Conflict("参考数据与采集快照不匹配")
            if data["shared"]["context"].get("reference_status") != "generated":
                raise Conflict("自动整理的参考数据必须标记为未经人工确认")
            version_id = uid("dataset")
            c.execute(
                """INSERT INTO eval_dataset_versions
                   (id,space_id,source_snapshot_id,name,mode,content,content_sha256,
                    review_state,created_by)
                   VALUES (%s,%s,%s,%s,'golden_regression',%s,%s,'{}'::jsonb,%s)""",
                (version_id, job["space_id"], job["snapshot_id"], data["name"],
                 Jsonb(data), digest(data), subject),
            )
            c.execute("UPDATE eval_capture_jobs SET dataset_version_id=%s WHERE id=%s",
                      (version_id, capture_id))
            self._audit(c, subject, job["space_id"], "dataset.reference_frozen", version_id)
        return version_id

    def retry_batch_preparation(self, batch_id: str, subject: str):
        """仅重试采样或数据准备，不重新发送已有回放请求。"""

        with self.pool.connection() as c:
            c.execute("SELECT id FROM eval_batches WHERE id=%s FOR UPDATE", (batch_id,))
            items = c.execute(
                """SELECT i.*,c.status AS capture_status FROM eval_batch_items i
                   JOIN eval_capture_jobs c ON c.id=i.capture_id
                   WHERE i.batch_id=%s AND i.experiment_id IS NULL
                   AND (i.error_message IS NOT NULL OR c.status='failed') FOR UPDATE""",
                (batch_id,),
            ).fetchall()
            for item in items:
                if item["capture_status"] == "failed":
                    c.execute(
                        """UPDATE eval_capture_jobs SET status='queued',attempt=0,
                           error_code=NULL,error_message=NULL,finished_at=NULL,
                           lease_token=NULL,lease_until=NULL WHERE id=%s""",
                        (item["capture_id"],),
                    )
                c.execute("UPDATE eval_batch_items SET error_message=NULL WHERE id=%s",
                          (item["id"],))
                self._audit(c, subject, item["space_id"], "batch.preparation_retried", item["id"])
        return len(items)

    def finish_batch_item(self, item_id: str, *, experiment_id: str | None = None,
                          error_message: str | None = None):
        with self.pool.connection() as c:
            c.execute(
                """UPDATE eval_batch_items SET experiment_id=%s,error_message=%s
                   WHERE id=%s AND experiment_id IS NULL AND error_message IS NULL""",
                (experiment_id, error_message, item_id),
            )

    # 批次里没有还在采集、等待开跑或运行中的空间
    BATCH_SETTLED = f"""NOT EXISTS (
        SELECT 1 FROM eval_batch_items i JOIN eval_capture_jobs c ON c.id=i.capture_id
        LEFT JOIN eval_experiments e ON e.id=i.experiment_id
        WHERE i.batch_id=b.id AND (e.status NOT IN {FINISHED} OR EXISTS (
            SELECT 1 FROM eval_experiments child
            WHERE child.manifest#>>'{{execution_continuation,parent_experiment_id}}'=e.id
              AND child.status IN ('queued','running')) OR (
            i.experiment_id IS NULL AND i.error_message IS NULL
            AND c.status NOT IN ('failed','cancelled'))))"""

    def batches(self, limit: int = 100, include_archived: bool = False):
        return self.query(
            f"""SELECT b.*,count(i.id) AS spaces,count(i.experiment_id) AS started,
                      count(*) FILTER (WHERE e.status IN {FINISHED} AND NOT EXISTS (
                        SELECT 1 FROM eval_experiments child WHERE child.manifest#>>
                            '{{execution_continuation,parent_experiment_id}}'=e.id
                          AND child.status IN ('queued','running'))) AS finished,
                      count(*) FILTER (WHERE i.error_message IS NOT NULL
                                       OR c.status IN ('failed','cancelled')) AS failed,
                      count(*) FILTER (WHERE {self.BLIND_STATUS} IS NOT NULL) AS blind,
                      count(*) FILTER (WHERE {self.BLIND_STATUS} IN ('revealed','skipped'))
                        AS revealed
               FROM eval_batches b JOIN eval_batch_items i ON i.batch_id=b.id
               JOIN eval_capture_jobs c ON c.id=i.capture_id
               LEFT JOIN eval_experiments e ON e.id=i.experiment_id
               WHERE %s OR b.archived_at IS NULL
               GROUP BY b.id ORDER BY b.created_at DESC LIMIT %s""", (include_archived, limit),
        )

    def batch(self, batch_id: str):
        batch = self.one("SELECT * FROM eval_batches WHERE id=%s", (batch_id,))
        items = self.query(
            f"""SELECT i.id,i.space_id,i.capture_id,i.experiment_id,i.error_message,
                      s.name AS space_name,s.source_name,c.status AS capture_status,
                      c.error_message AS capture_error,c.dataset_version_id,
                      snap.summary AS capture_outcome,
                      progress.planned_turns,progress.executed_turns,progress.judged_turns,
                      progress.cancelled_turns,progress.execution_errors,
                      e.manifest->'execution_stop' AS execution_stop,
                      e.status AS experiment_status,
                      {self.DISPLAY_STATUS} AS experiment_display_status,
                      {self.EXECUTION_FAILED} AS execution_failed,
                      {self.BLIND_STATUS} AS blind_status
               FROM eval_batch_items i JOIN eval_spaces s ON s.id=i.space_id
               JOIN eval_capture_jobs c ON c.id=i.capture_id
               LEFT JOIN eval_experiments e ON e.id=i.experiment_id
               LEFT JOIN eval_source_snapshots snap ON snap.id=c.snapshot_id
               LEFT JOIN LATERAL (
                   SELECT count(*) FILTER (WHERE stage='execute') AS planned_turns,
                          count(*) FILTER (WHERE stage='execute' AND status IN
                              ('succeeded','failed','uncertain')) AS executed_turns,
                          count(*) FILTER (WHERE stage='execute' AND status='cancelled')
                              AS cancelled_turns,
                          count(*) FILTER (WHERE stage='execute' AND status IN
                              ('failed','uncertain')) AS execution_errors,
                          count(*) FILTER (WHERE stage='judge' AND status='succeeded')
                              AS judged_turns
                   FROM eval_jobs WHERE experiment_id=i.experiment_id
               ) progress ON true
               WHERE i.batch_id=%s ORDER BY s.name""", (batch_id,),
        )
        ids = [item["experiment_id"] for item in items if item["experiment_id"]]
        progress = ({row["logical_id"]: row for row in self.query(PROGRESS_SQL, (ids,))}
                    if ids else {})
        for item in items:
            counts = progress.get(item["experiment_id"])
            if not counts:
                continue
            state = with_progress({"status": item["experiment_status"]}, counts)
            item.update({"experiment_status": state["status"],
                         "experiment_display_status": state.get("display_status",
                             item["experiment_display_status"]),
                         "planned_turns": counts["planned"],
                         "executed_turns": counts["executed"] + counts["execution_failed"]
                             + counts["uncertain"],
                         "judged_turns": counts["judged"], "cancelled_turns": counts["excluded"],
                         "execution_errors": counts["execution_failed"] + counts["uncertain"]})
        return batch, items

    def candidate_presets(self):
        return self.query(
            """SELECT * FROM eval_candidate_presets WHERE deleted_at IS NULL
               ORDER BY updated_at DESC"""
        )

    def candidate_preset(self, preset_id: str):
        return self.one(
            "SELECT * FROM eval_candidate_presets WHERE id=%s AND deleted_at IS NULL",
            (preset_id,),
        )

    def save_candidate_preset(self, preset_id: str | None, data: dict, subject: str):
        """preset_id 为空时新建；更新时 data 带 sealed_key 才替换 key，并校验修订号。"""

        with self.pool.connection() as c:
            if preset_id is None:
                preset_id = uid("candidate")
                try:
                    c.execute(
                        """INSERT INTO eval_candidate_presets
                           (id,name,base_url,model,sealed_key,key_hint,note,created_by,updated_by)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (preset_id, data["name"], data["base_url"], data["model"],
                         data["sealed_key"], data["key_hint"], data["note"], subject, subject),
                    )
                except UniqueViolation as exc:
                    raise Conflict("已有同名的候选模型") from exc
                self._audit(c, subject, None, "candidate_preset.created", preset_id)
                return {"id": preset_id, "revision": 1}
            try:
                row = c.execute(
                    """UPDATE eval_candidate_presets SET name=%s,base_url=%s,model=%s,note=%s,
                       sealed_key=coalesce(%s,sealed_key),key_hint=coalesce(%s,key_hint),
                       revision=revision+1,updated_by=%s,updated_at=now()
                       WHERE id=%s AND revision=%s AND deleted_at IS NULL RETURNING revision""",
                    (data["name"], data["base_url"], data["model"], data["note"],
                     data.get("sealed_key"), data.get("key_hint"), subject, preset_id,
                     data["revision"]),
                ).fetchone()
            except UniqueViolation as exc:
                raise Conflict("已有同名的候选模型") from exc
            if row is None:
                raise Conflict("候选模型已被他人修改或删除，请刷新后重试")
            self._audit(c, subject, None, "candidate_preset.updated", preset_id)
            return {"id": preset_id, "revision": row["revision"]}

    def delete_candidate_preset(self, preset_id: str, subject: str):
        with self.pool.connection() as c:
            row = c.execute(
                """UPDATE eval_candidate_presets SET deleted_at=now(),updated_by=%s
                   WHERE id=%s AND deleted_at IS NULL RETURNING id""", (subject, preset_id),
            ).fetchone()
            if row is None:
                raise NotFound("候选模型不存在")
            self._audit(c, subject, None, "candidate_preset.deleted", preset_id)

    def setting_revisions(self, key: str, limit: int = 20):
        """最新在前；第一条即当前生效值。"""

        return self.query(
            """SELECT * FROM eval_setting_revisions WHERE key=%s
               ORDER BY revision DESC LIMIT %s""", (key, limit),
        )

    def save_setting(self, key: str, value: dict, revision: int, subject: str):
        """revision 为调用方看到的当前版本号，不一致说明别人已修改。"""

        with self.pool.connection() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"setting:{key}",))
            current = c.execute(
                "SELECT coalesce(max(revision),0) AS n FROM eval_setting_revisions WHERE key=%s",
                (key,),
            ).fetchone()["n"]
            if current != revision:
                raise Conflict("配置已被他人修改，请刷新后重试")
            c.execute(
                """INSERT INTO eval_setting_revisions(key,revision,value,subject)
                   VALUES (%s,%s,%s,%s)""", (key, current + 1, Jsonb(value), subject),
            )
            self._audit(c, subject, None, "setting.updated", f"{key}:{current + 1}")
            return {"revision": current + 1}

    def prompt_usage(self):
        """各评审提示词版本被哪些评测用过；提示词原文冻结在评测 manifest 中。"""

        return self.query(
            """SELECT mode, manifest->>'judge_prompt_version' AS version,
                      manifest->>'judge_prompt_sha256' AS sha256, count(*) AS runs,
                      min(created_at) AS first_used, max(created_at) AS last_used,
                      (array_agg(coalesce(manifest->'atomic_prompt'->>'text',
                                          manifest->'judge_prompt'->>'content')
                                 ORDER BY created_at DESC))[1] AS text
               FROM eval_experiments GROUP BY 1,2,3 ORDER BY max(created_at) DESC"""
        )

    def reviews(self, run_id: str):
        return self.query(
            f"""WITH {GROUP_CTES} SELECT r.* FROM eval_reviews r
                JOIN logical_jobs j ON j.id=r.job_id ORDER BY r.created_at,r.id""", ([run_id],),
        )

    def events(self, run_id: str, after_id: int = 0):
        return self.query(
            f"""WITH {GROUP_CTES} SELECT e.* FROM eval_events e
                JOIN group_runs g ON g.run_id=e.experiment_id
                WHERE e.id>%s ORDER BY e.id LIMIT 200""", ([run_id], after_id),
        )

    def report_exports(self, run_id: str):
        return self.query(
            """SELECT * FROM eval_report_exports WHERE experiment_id=%s
               ORDER BY revision DESC""", (run_id,),
        )

    def expired_evidence(self, limit=100):
        return self.query(
            """SELECT e.id FROM eval_experiments e
               WHERE e.evidence_purged_at IS NULL
                 AND e.created_at < now()-interval '30 days'
                 AND e.status IN ('completed','failed','uncertain','cancelled')
                 AND (e.mode='atomic_replay' OR e.review_status='completed')
                 AND EXISTS (SELECT 1 FROM eval_report_exports r
                   WHERE r.experiment_id=e.id AND r.revision=e.report_revision
                     AND r.status='succeeded' AND r.object_purged_at IS NULL)
                 AND NOT EXISTS (SELECT 1 FROM eval_experiments child
                   WHERE child.source_experiment_id=e.id
                     AND child.status IN ('queued','running'))
               ORDER BY e.created_at LIMIT %s""", (limit,),
        )

    def purge_evidence(self, run_id: str):
        with self.pool.connection() as c:
            row = c.execute(
                """SELECT id,space_id FROM eval_experiments WHERE id=%s
                   AND evidence_purged_at IS NULL
                   AND created_at < now()-interval '30 days'
                   AND status IN ('completed','failed','uncertain','cancelled')
                   AND (mode='atomic_replay' OR review_status='completed')
                   FOR UPDATE""", (run_id,),
            ).fetchone()
            if not row:
                return False
            if not c.execute(
                """SELECT 1 FROM eval_report_exports r JOIN eval_experiments e
                   ON e.id=r.experiment_id WHERE e.id=%s
                   AND r.revision=e.report_revision AND r.status='succeeded'
                   AND r.object_purged_at IS NULL""", (run_id,),
            ).fetchone():
                return False
            if c.execute(
                """SELECT 1 FROM eval_experiments WHERE source_experiment_id=%s
                   AND status IN ('queued','running') LIMIT 1""", (run_id,),
            ).fetchone():
                return False
            c.execute(
                """UPDATE eval_jobs SET input='{"purged":true}'::jsonb,result=NULL
                   WHERE experiment_id=%s""", (run_id,),
            )
            c.execute(
                "UPDATE eval_experiments SET evidence_purged_at=now() WHERE id=%s",
                (run_id,),
            )
            self._audit(c, "retention", row["space_id"], "evidence.purged", run_id)
            return True

    def expired_snapshots(self, limit=100):
        return self.query(
            """SELECT s.id,s.object_key FROM eval_source_snapshots s
               WHERE s.object_purged_at IS NULL
                 AND s.created_at < now()-interval '30 days'
                 AND NOT EXISTS (SELECT 1 FROM eval_dataset_drafts d
                   WHERE d.source_snapshot_id=s.id AND d.status='draft')
               ORDER BY s.created_at LIMIT %s""", (limit,),
        )

    def purge_snapshot_object(self, snapshot_id: str, delete_object):
        with self.pool.connection() as c:
            row = c.execute(
                """SELECT id,object_key FROM eval_source_snapshots
                   WHERE id=%s AND object_purged_at IS NULL
                     AND created_at < now()-interval '30 days'
                   FOR UPDATE""", (snapshot_id,),
            ).fetchone()
            if not row or c.execute(
                """SELECT 1 FROM eval_dataset_drafts
                   WHERE source_snapshot_id=%s AND status='draft' LIMIT 1""",
                (snapshot_id,),
            ).fetchone():
                return False
            delete_object(row["object_key"])
            c.execute(
                "UPDATE eval_source_snapshots SET object_purged_at=now() WHERE id=%s",
                (snapshot_id,),
            )
            return True

    def expired_datasets(self, limit=100):
        return self.query(
            """SELECT v.id FROM eval_dataset_versions v
               WHERE v.content_purged_at IS NULL
                 AND v.created_at < now()-interval '730 days'
                 AND NOT EXISTS (SELECT 1 FROM eval_dataset_drafts d
                   WHERE d.derived_from_version_id=v.id AND d.status='draft')
                 AND NOT EXISTS (SELECT 1 FROM eval_dataset_versions child
                   WHERE child.derived_from_version_id=v.id
                     AND child.content_purged_at IS NULL)
                 AND NOT EXISTS (SELECT 1 FROM eval_experiments e
                   WHERE e.dataset_version_id=v.id
                     AND (e.evidence_purged_at IS NULL
                       OR e.created_at >= now()-interval '180 days'))
               ORDER BY v.created_at LIMIT %s""", (limit,),
        )

    def purge_dataset_content(self, version_id: str):
        with self.pool.connection() as c:
            row = c.execute(
                """SELECT id,space_id FROM eval_dataset_versions
                   WHERE id=%s AND content_purged_at IS NULL
                     AND created_at < now()-interval '730 days' FOR UPDATE""",
                (version_id,),
            ).fetchone()
            if not row:
                return False
            if c.execute(
                """SELECT 1 FROM eval_dataset_drafts
                   WHERE derived_from_version_id=%s AND status='draft'
                   UNION ALL SELECT 1 FROM eval_dataset_versions
                   WHERE derived_from_version_id=%s AND content_purged_at IS NULL
                   UNION ALL SELECT 1 FROM eval_experiments
                   WHERE dataset_version_id=%s AND (evidence_purged_at IS NULL
                     OR created_at >= now()-interval '180 days') LIMIT 1""",
                (version_id, version_id, version_id),
            ).fetchone():
                return False
            c.execute(
                """UPDATE eval_dataset_versions SET content='{"purged":true}'::jsonb,
                   content_purged_at=now() WHERE id=%s""", (version_id,),
            )
            self._audit(c, "retention", row["space_id"], "dataset.purged", version_id)
            return True

    def expired_reports(self, limit=100):
        return self.query(
            """SELECT r.id,r.object_key FROM eval_report_exports r
               JOIN eval_experiments e ON e.id=r.experiment_id
               WHERE r.object_purged_at IS NULL AND r.status='succeeded'
                 AND r.created_at < now()-interval '180 days'
                 AND (e.mode='atomic_replay' OR e.review_status='completed')
               ORDER BY r.created_at LIMIT %s""", (limit,),
        )

    def purge_report_object(self, report_id: str, delete_object):
        with self.pool.connection() as c:
            row = c.execute(
                """SELECT r.id,r.object_key FROM eval_report_exports r
                   JOIN eval_experiments e ON e.id=r.experiment_id
                   WHERE r.id=%s AND r.object_purged_at IS NULL
                     AND r.created_at < now()-interval '180 days'
                     AND (e.mode='atomic_replay' OR e.review_status='completed')
                   FOR UPDATE OF r""", (report_id,),
            ).fetchone()
            if not row:
                return False
            delete_object(row["object_key"])
            c.execute(
                "UPDATE eval_report_exports SET object_purged_at=now() WHERE id=%s",
                (report_id,),
            )
            return True

    def claim_report(self, worker_id: str, lease_seconds: int):
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_report_exports SET status='running',worker_id=%s,
                   lease_token=%s,lease_until=now()+%s*interval '1 second',attempt=attempt+1
                   WHERE id=(SELECT id FROM eval_report_exports WHERE status='queued'
                             ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING *""", (worker_id, uid("lease"), lease_seconds),
            ).fetchone()

    def heartbeat_report(self, job: dict, lease_seconds: int):
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_report_exports SET lease_until=now()+%s*interval '1 second'
                   WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now()
                   RETURNING id""", (lease_seconds, job["id"], job["lease_token"]),
            ).fetchone() is not None

    def finish_report(self, job: dict, *, object_key=None, sha=None, error_code=None):
        with self.pool.connection() as c:
            row = c.execute(
                """UPDATE eval_report_exports SET status=%s,object_key=%s,
                   content_sha256=%s,error_code=%s,lease_until=NULL,finished_at=now()
                   WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now()
                   RETURNING id""",
                (("queued" if job["attempt"] < 3 else "failed") if error_code
                 else "succeeded", object_key, sha, error_code,
                 job["id"], job["lease_token"]),
            ).fetchone()
            return bool(row)

    def recover_reports(self):
        with self.pool.connection() as c:
            rows = c.execute(
                """UPDATE eval_report_exports SET status='queued',lease_token=NULL,
                   lease_until=NULL WHERE status='running' AND lease_until<=now()
                   RETURNING id"""
            ).fetchall()
            return len(rows)

    def claim(self, worker_id: str, lease_seconds: int, space_limit: int = 2):
        with self.pool.connection() as c:
            candidates = c.execute(
                """SELECT e.id,e.space_id FROM eval_experiments e
                   WHERE e.status IN ('queued','running') AND EXISTS
                     (SELECT 1 FROM eval_jobs j WHERE j.experiment_id=e.id
                      AND j.status='queued')
                   ORDER BY e.updated_at,e.created_at LIMIT 100"""
            ).fetchall()
            for candidate in candidates:
                run = c.execute(
                    """SELECT id,space_id FROM eval_experiments WHERE id=%s
                       AND status IN ('queued','running') FOR UPDATE SKIP LOCKED""",
                    (candidate["id"],),
                ).fetchone()
                if not run:
                    continue
                space = c.execute(
                    "SELECT id FROM eval_spaces WHERE id=%s FOR UPDATE SKIP LOCKED",
                    (run["space_id"],),
                ).fetchone()
                if not space:
                    continue
                next_job = c.execute(
                    """SELECT id,stage FROM eval_jobs WHERE experiment_id=%s
                       AND status='queued' ORDER BY (stage='judge') DESC,created_at,id
                       FOR UPDATE SKIP LOCKED LIMIT 1""", (run["id"],),
                ).fetchone()
                if not next_job:
                    continue
                if next_job["stage"] == "execute":
                    active = c.execute(
                        """SELECT count(*) AS n FROM eval_jobs j JOIN eval_experiments e
                           ON e.id=j.experiment_id WHERE e.space_id=%s
                           AND j.stage='execute' AND j.status='running'""",
                        (run["space_id"],),
                    ).fetchone()["n"]
                    if active >= space_limit:
                        continue
                job = c.execute(
                    """UPDATE eval_jobs SET status='running',worker_id=%s,
                       lease_token=%s,lease_until=now()+%s*interval '1 second',
                       attempt=attempt+1 WHERE id=%s RETURNING *""",
                    (worker_id, uid("lease"), lease_seconds, next_job["id"]),
                ).fetchone()
                c.execute(
                    "UPDATE eval_experiments SET status='running',updated_at=now() WHERE id=%s",
                    (run["id"],),
                )
                self._event(c, run["id"], "job.started",
                            {"job_id": job["id"], "stage": job["stage"]})
                return job
            return None

    def heartbeat(self, job: dict, lease_seconds: int) -> bool:
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_jobs SET lease_until=now()+%s*interval '1 second'
                   WHERE id=%s AND status='running' AND lease_token=%s AND lease_until>now()
                   RETURNING id""",
                (lease_seconds, job["id"], job["lease_token"]),
            ).fetchone() is not None

    def finish(self, job: dict, *, result=None, error_code=None,
               uncertain=False, judge_input=None, engine_sha256=None):
        status = "uncertain" if uncertain else ("failed" if error_code else "succeeded")
        result_hash = digest(result) if result is not None else digest({
            "status": status, "error_code": error_code, "attempt": job["attempt"],
        })
        with self.pool.connection() as c:
            run = c.execute(
                "SELECT * FROM eval_experiments WHERE id=%s FOR UPDATE",
                (job["experiment_id"],),
            ).fetchone()
            row = c.execute(
                """UPDATE eval_jobs SET status=%s,result=%s,result_sha256=%s,
                   runtime_engine_sha256=%s,error_code=%s,finished_at=now(),lease_until=NULL
                   WHERE id=%s AND status='running' AND lease_token=%s AND lease_until>now()
                   RETURNING id""",
                (status, Jsonb(result) if result is not None else None,
                 result_hash, engine_sha256, error_code,
                 job["id"], job["lease_token"]),
            ).fetchone()
            if not row:
                return False
            if judge_input is not None and status == "succeeded" and run["status"] != "cancelled":
                c.execute(
                    """INSERT INTO eval_jobs
                       (id,experiment_id,case_id,repeat_index,stage,input)
                       VALUES (%s,%s,%s,%s,'judge',%s)""",
                    (uid("job"), run["id"], job["case_id"], job["repeat_index"],
                     Jsonb(judge_input)),
                )
            self._event(c, run["id"], "job.finished", {"job_id": job["id"], "status": status})
            # 最近 5 次执行均为同类平台请求异常时停止后续回放，避免故障请求耗尽整批。
            # 已发出的任务继续回写事实，不自动重发任何投递未知的请求。
            if (job["stage"] == "execute" and run["mode"] == "golden_regression"
                    and run["status"] != "cancelled" and not run["manifest"].get("execution_stop")
                    and error_code
                    and error_code.startswith("platform_http_")):
                counts = c.execute(
                    """SELECT count(*) FILTER (WHERE error_code=%s) AS errors,
                              count(*) AS recent
                       FROM (SELECT error_code FROM eval_jobs WHERE experiment_id=%s
                             AND stage='execute' AND status IN ('succeeded','failed','uncertain')
                             ORDER BY finished_at DESC,id DESC LIMIT 5) latest""",
                    (error_code, run["id"]),
                ).fetchone()
                if counts["errors"] == 5 and counts["recent"] == 5:
                    stop = {"error_code": error_code, "error_count": counts["errors"],
                            "reason": "最近 5 次执行均为同类平台请求异常，已停止后续回放。"
                                      "请核实接口或配额后新建回放；已发出请求的证据保留。"}
                    c.execute(
                        """UPDATE eval_experiments SET updated_at=now(),
                           manifest=jsonb_set(manifest,'{execution_stop}',%s)
                           WHERE id=%s""", (Jsonb(stop), run["id"]),
                    )
                    c.execute(
                        """UPDATE eval_jobs SET status='cancelled',finished_at=now()
                           WHERE experiment_id=%s AND status='queued' AND stage='execute'""",
                        (run["id"],),
                    )
                    self._event(c, run["id"], "experiment.execution_stopped", stop)
            self._settle(c, run["id"])
            return True

    def register_trace(self, job: dict, workspace_id: str) -> str:
        """对话请求发出前登记回放请求 ID：投递结果未知时也能按它到网关日志追溯。"""

        request_id = uuid4().hex
        with self.pool.connection() as c:
            c.execute(
                """INSERT INTO eval_turn_traces
                   (id,experiment_id,job_id,attempt,request_id,workspace_id)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (uid("trace"), job["experiment_id"], job["id"], job["attempt"],
                 request_id, workspace_id),
            )
        return request_id

    # 结算链路行：记下网关回写值与 Agent 日志里的模型调用次数，并排定首次查询时间。
    # 对话没发出去（停在建联系人之前）的直接记为未找到，不去查日志
    SETTLE_TRACE = """
        UPDATE eval_turn_traces t SET settled_at=now(),
          request_id_echoed=j.result->>'replay_request_id_echoed',
          echo_verified=(j.result->>'replay_request_id_echoed')=t.request_id,
          expected_calls=NULLIF((SELECT count(*) FROM jsonb_array_elements(COALESCE(
              (SELECT k.input->'observation'->'agent_logs' FROM eval_jobs k
               WHERE k.experiment_id=j.experiment_id AND k.case_id=j.case_id
                 AND k.repeat_index=j.repeat_index AND k.stage='judge'),
              j.result->'observation'->'agent_logs', '[]'::jsonb)) log
            WHERE log->>'type'='modelCall'), 0),
          status=CASE WHEN j.result->'observation'->>'stage_reached' IN ('planned','created')
                      THEN 'not_found' ELSE t.status END,
          error_code=CASE WHEN j.result->'observation'->>'stage_reached' IN ('planned','created')
                          THEN 'chat_not_sent' END,
          due_at=CASE WHEN j.result->'observation'->>'stage_reached' IN ('planned','created')
                      THEN NULL ELSE now()+%s*interval '1 second' END,
          deadline_at=now()+%s*interval '1 second'
        FROM eval_jobs j WHERE j.id=t.job_id AND t.settled_at IS NULL"""

    def settle_trace(self, job: dict, settle_seconds: int, deadline_seconds: int) -> None:
        """执行结束后结算该次尝试的链路行（取自已保存的执行结果）。"""

        with self.pool.connection() as c:
            c.execute(self.SETTLE_TRACE + " AND t.job_id=%s AND t.attempt=%s",
                      (settle_seconds, deadline_seconds, job["id"], job["attempt"]))

    def recover_traces(self, settle_seconds: int, deadline_seconds: int) -> int:
        """Worker 中途退出时执行任务已被回收但链路行没结算，补上结算与排期。"""

        with self.pool.connection() as c:
            return c.execute(self.SETTLE_TRACE + " AND j.status NOT IN ('queued','running')",
                             (settle_seconds, deadline_seconds)).rowcount

    def claim_trace(self, lease_seconds: int):
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_turn_traces SET lease_owner=%s,
                     lease_until=now()+%s*interval '1 second'
                   WHERE id=(SELECT id FROM eval_turn_traces WHERE status='pending'
                     AND due_at<=now() AND (lease_until IS NULL OR lease_until<now())
                     ORDER BY due_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING *""", (uid("lease"), lease_seconds),
            ).fetchone()

    def reschedule_trace(self, trace: dict, call_count: int, due_at) -> bool:
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_turn_traces SET call_count=%s,poll_count=poll_count+1,due_at=%s,
                     lease_owner=NULL,lease_until=NULL
                   WHERE id=%s AND lease_owner=%s AND status='pending' RETURNING id""",
                (call_count, due_at, trace["id"], trace["lease_owner"]),
            ).fetchone() is not None

    def finish_trace(self, trace: dict, *, status: str, summary: list | None = None,
                     object_key: str | None = None, content_sha256: str | None = None,
                     error_code: str | None = None) -> bool:
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_turn_traces SET status=%s,summary=%s,call_count=%s,object_key=%s,
                     content_sha256=%s,error_code=%s,collected_at=now(),poll_count=poll_count+1,
                     due_at=NULL,lease_owner=NULL,lease_until=NULL
                   WHERE id=%s AND lease_owner=%s AND status='pending' RETURNING id""",
                (status, Jsonb(summary) if summary is not None else None,
                 len(summary) if summary is not None else None, object_key, content_sha256,
                 error_code, trace["id"], trace["lease_owner"]),
            ).fetchone() is not None

    def freeze_turn_rubric(self, job: dict, rubric: dict) -> dict:
        """每个 Turn 仅首次写入有效；重复执行、重试和并发起草共用同一标准。"""

        with self.pool.connection() as c:
            row = c.execute(
                """UPDATE eval_experiments e SET manifest=jsonb_set(e.manifest,
                     '{judge_rubrics}', COALESCE(e.manifest->'judge_rubrics','{}'::jsonb)
                     || jsonb_build_object(%s::text,
                       COALESCE(e.manifest->'judge_rubrics'->%s,%s)))
                   WHERE e.id=%s AND EXISTS (SELECT 1 FROM eval_jobs j
                     WHERE j.id=%s AND j.experiment_id=e.id AND j.stage='judge' AND j.case_id=%s
                       AND j.status='running' AND j.lease_token=%s AND j.lease_until>now())
                   RETURNING manifest->'judge_rubrics'->%s AS rubric""",
                (job["case_id"], job["case_id"], Jsonb(rubric), job["experiment_id"],
                 job["id"], job["case_id"], job["lease_token"], job["case_id"]),
            ).fetchone()
            if row is None:
                raise Conflict("判分租约已失效，不能冻结本轮评分标准")
            return row["rubric"]

    def freeze_workspace_policy(self, job: dict, policy: dict) -> dict:
        """首个有效租约冻结空间规则；同一运行的其他轮和重试复用，不继承来源重判的旧规则。"""

        with self.pool.connection() as c:
            row = c.execute(
                """UPDATE eval_experiments e SET manifest=jsonb_set(e.manifest,
                     '{workspace_policy}',COALESCE(e.manifest->'workspace_policy',%s))
                   WHERE e.id=%s AND EXISTS (SELECT 1 FROM eval_jobs j
                     WHERE j.id=%s AND j.experiment_id=e.id AND j.status='running'
                       AND j.lease_token=%s AND j.lease_until>now())
                   RETURNING manifest->'workspace_policy' AS policy""",
                (Jsonb(policy), job["experiment_id"], job["id"], job["lease_token"]),
            ).fetchone()
            if row is None:
                raise Conflict("任务租约已失效，不能冻结空间规则")
            return row["policy"]

    def freeze_judge_evidence(self, job: dict, evidence: dict) -> dict:
        """首次判分冻结当时可用证据；重试不可悄悄换成后来采集的材料。"""

        with self.pool.connection() as c:
            row = c.execute(
                """UPDATE eval_jobs SET input=jsonb_set(input,'{judge_evidence}',
                     COALESCE(input->'judge_evidence',%s))
                   WHERE id=%s AND stage='judge' AND status='running'
                     AND lease_token=%s AND lease_until>now()
                   RETURNING input->'judge_evidence' AS evidence""",
                (Jsonb(evidence), job["id"], job["lease_token"]),
            ).fetchone()
            if row is None:
                raise Conflict("判分任务租约已失效，不能冻结证据")
            return row["evidence"]

    def experiment_traces(self, run_id: str, job_id: str | None = None) -> list[dict]:
        """沿重新判分的直接来源链找到原执行，每个任务只取原执行最近一次尝试的链路。"""

        return self.query(
            f"""WITH RECURSIVE {GROUP_CTES}, sources AS (
                 SELECT j.id AS result_job_id,j.id,j.source_job_id,ARRAY[j.id] AS path
                 FROM logical_jobs j WHERE j.stage='execute'
                   AND (%s::text IS NULL OR j.id=%s)
                 UNION ALL
                 SELECT s.result_job_id,j.id,j.source_job_id,s.path || j.id
                 FROM sources s JOIN eval_jobs j ON j.id=s.source_job_id
                 WHERE j.stage='execute' AND NOT j.id=ANY(s.path)
               )
               SELECT DISTINCT ON (s.result_job_id) s.result_job_id,t.*
               FROM sources s JOIN eval_turn_traces t ON t.job_id=s.id
               WHERE s.source_job_id IS NULL
               ORDER BY s.result_job_id,t.attempt DESC""", ([run_id], job_id, job_id),
        )

    def expired_traces(self, limit=100):
        return self.query(
            """SELECT id,object_key FROM eval_turn_traces
               WHERE object_key IS NOT NULL AND object_purged_at IS NULL
                 AND collected_at < now()-interval '60 days'
               ORDER BY collected_at LIMIT %s""", (limit,),
        )

    def purge_trace_object(self, trace_id: str, delete_object) -> bool:
        """链路正文含未脱敏的模型输入输出，独立于逐条证据保留 60 天；索引行长期保留。"""

        with self.pool.connection() as c:
            row = c.execute(
                """SELECT object_key FROM eval_turn_traces WHERE id=%s
                   AND object_key IS NOT NULL AND object_purged_at IS NULL
                   AND collected_at < now()-interval '60 days' FOR UPDATE""", (trace_id,),
            ).fetchone()
            if not row:
                return False
            delete_object(row["object_key"])
            c.execute("UPDATE eval_turn_traces SET object_purged_at=now() WHERE id=%s",
                      (trace_id,))
            return True

    def retry(self, job: dict, max_attempts: int) -> bool:
        """把网络失败的任务重新排队；已达尝试上限时返回 False，由调用方记为失败。"""

        if job["attempt"] >= max_attempts:
            return False
        with self.pool.connection() as c:
            return c.execute(
                """UPDATE eval_jobs SET status='queued',lease_token=NULL,lease_until=NULL
                   WHERE id=%s AND status='running' AND lease_token=%s RETURNING id""",
                (job["id"], job["lease_token"]),
            ).fetchone() is not None

    def retry_failed(self, run_id: str, key: str, subject: str) -> dict:
        """原子任务可补跑；逐轮评测仅重试评分，保留原执行及冻结标准。"""

        key_hash = digest(key)
        with self.pool.connection() as c:
            run = c.execute(
                "SELECT * FROM eval_experiments WHERE id=%s FOR UPDATE", (run_id,),
            ).fetchone()
            if not run:
                raise NotFound("评测不存在")
            previous = c.execute(
                """SELECT payload FROM eval_events WHERE experiment_id=%s
                   AND kind='experiment.retry_requested' AND payload->>'key_hash'=%s
                   LIMIT 1""", (run_id, key_hash),
            ).fetchone()
            if previous:
                state = with_progress(run, c.execute(PROGRESS_SQL, ([run_id],)).fetchone())
                return {"status": state["status"], "requeued": previous["payload"]["requeued"]}
            progress = c.execute(PROGRESS_SQL, ([run_id],)).fetchone()
            if with_progress(run, progress)["status"] not in ("failed", "uncertain"):
                raise Conflict("只有已结束且存在失败项的评测可以重试")
            if run["evidence_purged_at"]:
                raise Conflict("评测证据已过保留期，不能补跑")
            if run["archived_at"]:
                raise Conflict("请先恢复已归档评测，再补跑失败项")
            config = run["manifest"]["config"]
            if (run["mode"] == "atomic_replay" and config.get("execution_target") != "fixture"
                    and config.get("candidate_source") != "gateway"):
                raise Conflict("仅支持补跑 3Chat 网关模型；其他接口的密钥已在评测结束时清除")
            failed = c.execute(
                f"""WITH {GROUP_CTES} SELECT j.id,j.experiment_id,j.stage,j.error_code,j.attempt
                   FROM eval_jobs j JOIN logical_jobs chosen ON chosen.id=j.id
                   WHERE j.status IN ('failed','uncertain')
                   ORDER BY j.created_at,j.id FOR UPDATE OF j""", ([run_id],),
            ).fetchall()
            if not failed:
                raise Conflict("当前评测没有可补跑的失败项")
            if run["mode"] == "golden_regression" and any(
                    job["stage"] != "judge" for job in failed):
                raise Conflict("逐轮评测仅支持重试评分异常；执行失败需要核实后新建回放")
            ids = [job["id"] for job in failed]
            c.execute(
                """UPDATE eval_jobs SET status='queued',error_code=NULL,result=NULL,
                   result_sha256=NULL,runtime_engine_sha256=NULL,finished_at=NULL,
                   worker_id=NULL,lease_token=NULL,lease_until=NULL
                   WHERE id=ANY(%s)""", (ids,),
            )
            c.execute(
                """UPDATE eval_experiments SET status='running',updated_at=now(),
                   review_status=CASE WHEN review_status='completed' THEN 'in_progress'
                                      ELSE review_status END
                   WHERE id=ANY(%s)""", (list({job["experiment_id"] for job in failed}),),
            )
            c.execute("UPDATE eval_experiments SET review_status='in_progress' "
                      "WHERE id=%s AND review_status='completed'", (run_id,))
            self._event(c, run_id, "experiment.retry_requested", {
                "key_hash": key_hash, "requeued": len(ids),
                "previous_failures": [{"job_id": job["id"], "stage": job["stage"],
                                       "error_code": job["error_code"],
                                       "attempt": job["attempt"]} for job in failed],
            })
            self._audit(c, subject, run["space_id"], "experiment.retry_requested", run_id)
            return {"status": "running", "requeued": len(ids)}

    def recover_expired(self, max_attempts: int = 3):
        """租约过期（Worker 被重启或崩溃）的任务：无副作用的重新排队，其余标为待核实。

        评审与原子回放的候选调用都没有副作用，可以安全重做；金标执行会写入 EVAL 空间，
        不能盲目重放。重排也有次数上限，避免把 Worker 拖垮的任务反复执行。
        """

        recovered = 0
        with self.pool.connection() as c:
            runs = c.execute(
                """SELECT e.id,e.mode,e.status FROM eval_experiments e WHERE EXISTS
                   (SELECT 1 FROM eval_jobs j WHERE j.experiment_id=e.id
                    AND j.status='running' AND j.lease_until<=now())
                   ORDER BY e.id FOR UPDATE SKIP LOCKED"""
            ).fetchall()
            for run in runs:
                if run["status"] == "cancelled":
                    # 已取消的评测不会再被领取，过期任务直接结束，不要重新排队
                    c.execute(
                        """UPDATE eval_jobs SET status='cancelled',finished_at=now(),
                           lease_until=NULL WHERE experiment_id=%s AND status='running'
                             AND lease_until<=now()""", (run["id"],),
                    )
                    continue
                safe = c.execute(
                    """UPDATE eval_jobs SET status='queued',lease_until=NULL,lease_token=NULL
                       WHERE experiment_id=%s AND (stage='judge' OR %s) AND status='running'
                         AND lease_until<=now() AND attempt<%s RETURNING id""",
                    (run["id"], run["mode"] == "atomic_replay", max_attempts),
                ).fetchall()
                uncertain = c.execute(
                    """UPDATE eval_jobs SET status='uncertain',
                       error_code='worker_lease_expired',finished_at=now(),lease_until=NULL
                       WHERE experiment_id=%s AND status='running'
                         AND lease_until<=now() RETURNING id""",
                    (run["id"],),
                ).fetchall()
                if safe or uncertain:
                    self._event(c, run["id"], "jobs.recovered",
                                {"requeued": len(safe), "uncertain": len(uncertain)})
                    self._settle(c, run["id"])
                    recovered += len(safe) + len(uncertain)
        return recovered

    def cancel(self, run_id: str, subject: str):
        with self.pool.connection() as c:
            runs = c.execute(
                f"""WITH {GROUP_CTES} SELECT e.* FROM group_runs g
                    JOIN eval_experiments e ON e.id=g.run_id ORDER BY e.id FOR UPDATE OF e""",
                ([run_id],),
            ).fetchall()
            if not runs:
                raise NotFound("评测不存在")
            for run in runs:
                if run["status"] in ("completed", "failed", "cancelled", "uncertain"):
                    continue
                c.execute(
                    """UPDATE eval_experiments SET status='cancelled',candidate_api_key=NULL,
                       updated_at=now() WHERE id=%s""", (run["id"],),
                )
                c.execute(
                    """UPDATE eval_jobs SET status='cancelled',finished_at=now()
                       WHERE experiment_id=%s AND status='queued'""", (run["id"],),
                )
                self._event(c, run["id"], "experiment.cancelled", {})
                self._queue_report(c, run["id"])
                self._audit(c, subject, run["space_id"], "experiment.cancelled", run["id"])
            return next(run for run in runs if run["id"] == run_id)

    def set_data_archived(self, kind: str, ids: list[str], archived: bool, subject: str):
        tables = {"drafts": "eval_dataset_drafts", "datasets": "eval_dataset_versions",
                  "captures": "eval_capture_jobs", "snapshots": "eval_source_snapshots"}
        table = tables[kind]
        guard = "AND status NOT IN ('queued','running')" if kind == "captures" else ""
        if kind == "drafts":
            guard = ("AND NOT EXISTS (SELECT 1 FROM eval_annotation_jobs a "
                     "WHERE a.draft_id=eval_dataset_drafts.id "
                     "AND a.status IN ('queued','running'))")
        with self.pool.connection() as c:
            rows = c.execute(
                f"""UPDATE {table} SET archived_at=CASE WHEN %s THEN now() END
                    WHERE id=ANY(%s) AND (archived_at IS NULL)=%s {guard}
                    RETURNING id,space_id""", (archived, ids, archived),
            ).fetchall()
            for row in rows:
                self._audit(c, subject, row["space_id"],
                            f"{kind}.archived" if archived else f"{kind}.unarchived", row["id"])
        return len(rows)

    def set_archived(self, run_ids: list[str], archived: bool, subject: str) -> int:
        """批量归档或恢复；只处理已结束的评测，返回实际变更的条数。"""

        with self.pool.connection() as c:
            rows = c.execute(
                f"""UPDATE eval_experiments
                   SET archived_at=CASE WHEN %s THEN now() END
                   WHERE id=ANY(%s) AND (archived_at IS NULL)=%s AND status IN {FINISHED}
                     AND NOT EXISTS (SELECT 1 FROM eval_experiments child
                       WHERE child.manifest#>>'{{execution_continuation,parent_experiment_id}}'
                           =eval_experiments.id AND child.status IN ('queued','running'))
                   RETURNING id,space_id""", (archived, run_ids, archived),
            ).fetchall()
            action = "experiment.archived" if archived else "experiment.unarchived"
            for row in rows:
                self._audit(c, subject, row["space_id"], action, row["id"])
            return len(rows)

    def archive_expired_trials(self, days: int = 7) -> int:
        """试跑评测结束满 days 天自动归档，不再占据评测列表。"""

        with self.pool.connection() as c:
            rows = c.execute(
                f"""UPDATE eval_experiments SET archived_at=now()
                   WHERE archived_at IS NULL AND manifest->'config'->>'trial'='true'
                     AND status IN {FINISHED} AND created_at < now()-%s*interval '1 day'
                   RETURNING id,space_id""", (days,),
            ).fetchall()
            for row in rows:
                self._audit(c, "retention", row["space_id"], "experiment.archived", row["id"])
            return len(rows)

    def delete_experiment(self, run_id: str, subject: str) -> list[str]:
        """永久删除一条无人看过的评测，返回需要随后清理的对象存储 key。"""

        with self.pool.connection() as c:
            return self._delete_experiment(c, run_id, subject)

    def _delete_experiment(self, c, run_id: str, subject: str) -> list[str]:
        run = c.execute(
            f"""SELECT e.id,e.space_id,e.status,{self.DELETABLE} AS deletable
                FROM eval_experiments e WHERE e.id=%s FOR UPDATE""", (run_id,),
        ).fetchone()
        if not run:
            raise NotFound("评测不存在")
        if not run["deletable"]:
            raise Conflict("只能删除已归档、已结束、没有人工复核、也不是重新判分来源的评测")
        keys = [row["object_key"] for row in c.execute(
            """SELECT object_key FROM eval_report_exports WHERE experiment_id=%s
               AND object_key IS NOT NULL AND object_purged_at IS NULL""", (run_id,),
        ).fetchall()]
        # 批量评测里引用这条评测的条目保留，标明评测已删除
        c.execute(
            """UPDATE eval_batch_items SET experiment_id=NULL,error_message='评测已被删除'
               WHERE experiment_id=%s""", (run_id,),
        )
        keys += [row["object_key"] for row in c.execute(
            """SELECT object_key FROM eval_turn_traces WHERE experiment_id=%s
               AND object_key IS NOT NULL AND object_purged_at IS NULL""", (run_id,),
        ).fetchall()]
        for table in ("eval_events", "eval_report_exports", "eval_blind_reviews",
                      "eval_turn_traces", "eval_jobs"):
            c.execute(f"DELETE FROM {table} WHERE experiment_id=%s", (run_id,))
        c.execute("DELETE FROM eval_experiments WHERE id=%s", (run_id,))
        self._audit(c, subject, run["space_id"], "experiment.deleted", run_id)
        return keys

    def set_batches_archived(self, batch_ids: list[str], archived: bool, subject: str) -> int:
        """批次与其中已结束的评测一起归档或恢复；还有空间在进行中的批次不处理。"""

        with self.pool.connection() as c:
            rows = c.execute(
                f"""UPDATE eval_batches b SET archived_at=CASE WHEN %s THEN now() END
                    WHERE id=ANY(%s) AND (archived_at IS NULL)=%s AND {self.BATCH_SETTLED}
                    RETURNING id""", (archived, batch_ids, archived),
            ).fetchall()
            changed = [row["id"] for row in rows]
            runs = c.execute(
                f"""UPDATE eval_experiments SET archived_at=CASE WHEN %s THEN now() END
                    WHERE id IN (SELECT experiment_id FROM eval_batch_items WHERE batch_id=ANY(%s))
                      AND (archived_at IS NULL)=%s AND status IN {FINISHED}
                    RETURNING id,space_id""", (archived, changed, archived),
            ).fetchall()
            action = "experiment.archived" if archived else "experiment.unarchived"
            for run in runs:
                self._audit(c, subject, run["space_id"], action, run["id"])
            for batch_id in changed:
                self._audit(c, subject, None,
                            "batch.archived" if archived else "batch.unarchived", batch_id)
            return len(changed)

    def delete_batch(self, batch_id: str, subject: str) -> list[str]:
        """永久删除已归档的批次及其评测；任一评测不满足删除条件则整批不删。

        各空间的采集记录与样本数据集属于空间，保留不删。
        """

        with self.pool.connection() as c:
            batch = c.execute("SELECT * FROM eval_batches WHERE id=%s FOR UPDATE",
                              (batch_id,)).fetchone()
            if not batch:
                raise NotFound("批次不存在")
            if not batch["archived_at"]:
                raise Conflict("请先归档批次再删除")
            runs = [row["id"] for row in c.execute(
                """SELECT experiment_id AS id FROM eval_batch_items
                   WHERE batch_id=%s AND experiment_id IS NOT NULL""", (batch_id,),
            ).fetchall()]
            try:
                keys = [key for run_id in runs
                        for key in self._delete_experiment(c, run_id, subject)]
            except Conflict as exc:
                raise Conflict("批次中有评测已有人工复核或被重新评分引用，不能删除；"
                               "保留归档即可") from exc
            c.execute("DELETE FROM eval_batch_items WHERE batch_id=%s", (batch_id,))
            c.execute("DELETE FROM eval_batches WHERE id=%s", (batch_id,))
            self._audit(c, subject, None, "batch.deleted", batch_id)
            return keys

    def review(self, run_id: str, job_id: str, body: dict, subject: str):
        with self.pool.connection() as c:
            job = c.execute(
                """SELECT j.*,e.space_id,e.evidence_purged_at FROM eval_jobs j
                   JOIN eval_experiments e ON e.id=j.experiment_id
                   WHERE j.id=%s FOR UPDATE OF j""", (job_id,),
            ).fetchone()
            if not job or not c.execute(
                f"WITH {GROUP_CTES} SELECT 1 FROM logical_jobs WHERE id=%s",
                ([run_id], job_id),
            ).fetchone():
                raise NotFound("评测结果不存在")
            if job["evidence_purged_at"]:
                raise Conflict("逐条证据已按保留期限清理，不能继续复核")
            if job["status"] not in ("succeeded", "failed", "uncertain"):
                raise Conflict("结果尚未结束，请刷新")
            result_hash = job["result_sha256"] or digest(
                {"status": job["status"], "error_code": job["error_code"]}
            )
            if result_hash != body["outputs_sha256"]:
                raise Conflict("评审证据已变化，请刷新")
            if job["status"] != "succeeded" and body["label"] != "unavailable":
                raise Conflict("执行或评分失败只能标记为不可判断")
            if job["result"] and job["result"].get("status") == "not_evaluated" and (
                body["label"] != "unavailable"
            ):
                raise Conflict("开发 Fixture 不生成质量判断")
            if body["issue_kind"] != "none" and not body["note"].strip():
                raise Conflict("记录问题时必须填写依据")
            last = c.execute(
                """SELECT coalesce(max(revision),0) AS n FROM eval_reviews
                   WHERE job_id=%s AND subject=%s""", (job_id, subject),
            ).fetchone()["n"]
            if last != body["revision"]:
                raise Conflict("复核版本冲突，请刷新")
            c.execute(
                """INSERT INTO eval_reviews
                   (job_id,subject,revision,label,issue_kind,note,outputs_sha256,actor_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (job_id, subject, last + 1, body["label"], body["issue_kind"],
                 body["note"], result_hash, body.get("actor_type", "human")),
            )
            c.execute(
                """UPDATE eval_experiments SET review_status='in_progress'
                   WHERE id=ANY(%s) AND review_status IN ('pending','completed')""",
                ([job["experiment_id"], run_id],),
            )
            self._audit(c, subject, job["space_id"], "review.created", job_id)
            return {"revision": last + 1}

    def blind_review(self, run_id: str):
        """尚未抽样时返回 None。"""

        rows = self.query("SELECT * FROM eval_blind_reviews WHERE experiment_id=%s", (run_id,))
        return rows[0] if rows else None

    def draw_blind_review(self, run_id: str, sample: list[dict]):
        """并发打开时只有第一次抽样生效，其余返回已抽的样本。"""

        with self.pool.connection() as c:
            c.execute(
                """INSERT INTO eval_blind_reviews(experiment_id,sample) VALUES (%s,%s)
                   ON CONFLICT(experiment_id) DO NOTHING""", (run_id, Jsonb(sample)),
            )
        return self.blind_review(run_id)

    def reveal_blind_review(self, run_id: str, subject: str, reason: str):
        """锁定盲评结论后揭晓；样本没评完时必须写明提前揭晓的原因，统计里单独标注。"""

        with self.pool.connection() as c:
            row = c.execute(
                "SELECT * FROM eval_blind_reviews WHERE experiment_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if not row:
                raise Conflict("评测结束后才会抽取盲评样本")
            if row["revealed_at"]:
                raise Conflict("盲评已揭晓")
            jobs = c.execute("SELECT * FROM eval_jobs WHERE experiment_id=%s",
                             (run_id,)).fetchall()
            reviews = c.execute(
                """SELECT r.* FROM eval_reviews r JOIN eval_jobs j ON j.id=r.job_id
                   WHERE j.experiment_id=%s ORDER BY r.created_at,r.id""", (run_id,),
            ).fetchall()
            locked = blind.snapshot(row["sample"], jobs, reviews)
            missing = len(row["sample"]) - len(locked)
            if missing and not reason.strip():
                raise Conflict(f"还有 {missing} 条样本未盲评；如需提前揭晓请写明原因")
            c.execute(
                """UPDATE eval_blind_reviews SET revealed_at=now(),revealed_by=%s,
                   skip_reason=%s,snapshot=%s WHERE experiment_id=%s""",
                (subject, reason.strip() if missing else None, Jsonb(locked), run_id),
            )
            run = c.execute("SELECT space_id,status FROM eval_experiments WHERE id=%s",
                            (run_id,)).fetchone()
            # 样本全部评完即完成复核并归档报告；提前揭晓的仍需之后补齐复核
            if not missing and run["status"] not in ("queued", "running"):
                c.execute(
                    """UPDATE eval_experiments SET review_status='completed',updated_at=now()
                       WHERE id=%s""", (run_id,),
                )
                self._queue_report(c, run_id)
            self._event(c, run_id, "blind_review.revealed",
                        {"subject": subject, "missing": missing})
            self._audit(c, subject, run["space_id"], "blind_review.revealed", run_id)

    def complete_review(self, run_id: str, subject: str):
        with self.pool.connection() as c:
            run = c.execute(
                "SELECT * FROM eval_experiments WHERE id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if not run:
                raise NotFound("评测不存在")
            if run["status"] not in ("completed", "failed", "uncertain"):
                raise Conflict("评测尚未结束")
            jobs = c.execute(f"WITH {GROUP_CTES} SELECT * FROM logical_jobs",
                             ([run_id],)).fetchall()
            if any(job["status"] in {"queued", "running"} for job in jobs):
                raise Conflict("评测尚未结束")
            reviews = c.execute(
                f"""WITH {GROUP_CTES} SELECT DISTINCT job_id,outputs_sha256 FROM eval_reviews r
                   JOIN logical_jobs j ON j.id=r.job_id
                   WHERE r.subject=%s AND r.actor_type='human'""",
                ([run_id], subject)
            ).fetchall()
            reviewed = {(r["job_id"], r["outputs_sha256"]) for r in reviews}
            judged = {(j["case_id"], j["repeat_index"]): j for j in jobs
                      if j["stage"] == "judge"}
            blind_row = c.execute(
                "SELECT sample FROM eval_blind_reviews WHERE experiment_id=%s", (run_id,)
            ).fetchone()
            # 开启盲评的评测只要求抽中的样本复核完，其余样本不强制人工逐条看
            sampled = {item["job_id"] for item in blind_row["sample"]} if blind_row else None
            required = [judged.get((j["case_id"], j["repeat_index"]), j)
                        for j in jobs if j["stage"] == "execute"
                        and (sampled is None or j["id"] in sampled)]
            missing = sum((j["id"], j["result_sha256"] or digest({
                "status": j["status"], "error_code": j["error_code"]
            })) not in reviewed for j in required)
            if missing:
                raise Conflict(f"还有 {missing} 个执行结果未复核")
            c.execute(
                """UPDATE eval_experiments SET review_status='completed',updated_at=now()
                   WHERE id=%s""", (run_id,),
            )
            self._event(c, run_id, "review.completed", {"subject": subject})
            self._queue_report(c, run_id)
            self._audit(c, subject, run["space_id"], "review.completed", run_id)

    @staticmethod
    def _event(c, run_id: str, kind: str, payload: dict):
        c.execute(
            "INSERT INTO eval_events(experiment_id,kind,payload) VALUES (%s,%s,%s)",
            (run_id, kind, Jsonb(payload)),
        )

    @staticmethod
    def _audit(c, subject: str, space_id: str | None, action: str, resource_id: str):
        c.execute(
            """INSERT INTO eval_audit(subject,space_id,action,resource_id)
               VALUES (%s,%s,%s,%s)""", (subject, space_id, action, resource_id),
        )

    @staticmethod
    def _settle(c, run_id: str):
        previous = c.execute(
            "SELECT status FROM eval_experiments WHERE id=%s", (run_id,)
        ).fetchone()["status"]
        rows = c.execute(
            "SELECT status FROM eval_jobs WHERE experiment_id=%s", (run_id,)
        ).fetchall()
        states = {row["status"] for row in rows}
        status = (
            "running" if states & {"running", "queued"} else
            "uncertain" if "uncertain" in states else
            "failed" if "failed" in states else "completed"
        )
        # 评测结束即清除候选模型 key，只在运行期间加密保存
        c.execute(
            """UPDATE eval_experiments SET status=%s,updated_at=now(),
               candidate_api_key=CASE WHEN %s='running' THEN candidate_api_key END
               WHERE id=%s AND status<>'cancelled'""", (status, status, run_id),
        )
        if status in ("completed", "failed", "uncertain") and previous not in (
            "completed", "failed", "uncertain", "cancelled"
        ):
            Store._queue_report(c, run_id)
            parent = c.execute(
                "SELECT p.id FROM eval_experiments e JOIN eval_experiments p ON p.id="
                "e.manifest#>>'{execution_continuation,parent_experiment_id}' WHERE e.id=%s "
                "AND p.status IN ('completed','failed','uncertain')", (run_id,),
            ).fetchone()
            if parent and not c.execute(
                f"WITH {GROUP_CTES} SELECT 1 FROM logical_jobs "
                "WHERE status IN ('queued','running') LIMIT 1", ([parent["id"]],),
            ).fetchone():
                Store._queue_report(c, parent["id"])

    @staticmethod
    def _queue_report(c, run_id: str):
        revision = c.execute(
            """UPDATE eval_experiments SET report_revision=report_revision+1
               WHERE id=%s RETURNING report_revision""", (run_id,),
        ).fetchone()["report_revision"]
        c.execute(
            """INSERT INTO eval_report_exports(id,experiment_id,revision)
               VALUES (%s,%s,%s)""", (uid("report"), run_id, revision),
        )
