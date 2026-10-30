"""建库：全部表、约束与索引。

SQL 直接写在这里，不引用外部文件——已经执行过的迁移必须一字不变，
以后改表结构请新增 0002 迁移，不要修改本文件。
"""

from alembic import op

revision = "0001_platform"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_spaces (
    id text PRIMARY KEY,
    name text NOT NULL,
    source_workspace_id text NOT NULL,
    target_workspace_id text NOT NULL,
    source_connection text NOT NULL,
    target_connection text NOT NULL,
    fde_subject text,
    registry_record_id text UNIQUE,
    registry_updated_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','needs_check'))
);

CREATE TABLE eval_source_snapshots (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES eval_spaces(id),
    mode text NOT NULL CHECK (mode IN ('atomic_replay','golden_regression')),
    object_key text NOT NULL UNIQUE,
    content_sha256 text NOT NULL,
    source_hash text NOT NULL,
    summary jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    object_purged_at timestamptz
);
CREATE INDEX eval_snapshot_space ON eval_source_snapshots(space_id,created_at DESC);

CREATE TABLE eval_capture_jobs (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES eval_spaces(id),
    mode text NOT NULL CHECK (mode IN ('atomic_replay','golden_regression')),
    name text NOT NULL,
    spec jsonb NOT NULL,
    request_sha256 text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
    snapshot_id text REFERENCES eval_source_snapshots(id),
    dataset_version_id text,
    error_code text,
    worker_id text,
    lease_token text,
    lease_until timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE(space_id,idempotency_key)
);
CREATE INDEX eval_capture_claim ON eval_capture_jobs(created_at,id) WHERE status='queued';

CREATE TABLE eval_dataset_drafts (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES eval_spaces(id),
    source_snapshot_id text REFERENCES eval_source_snapshots(id),
    derived_from_version_id text,
    name text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('atomic_replay','golden_regression')),
    content jsonb NOT NULL,
    review_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    revision integer NOT NULL DEFAULT 1,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
    created_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_drafts_space ON eval_dataset_drafts(space_id,updated_at DESC);

CREATE TABLE eval_annotation_jobs (
    id text PRIMARY KEY,
    draft_id text NOT NULL REFERENCES eval_dataset_drafts(id),
    session_id text NOT NULL,
    case_sha256 text NOT NULL,
    shared_sha256 text NOT NULL,
    profile_prompt jsonb NOT NULL,
    rubric_prompt jsonb NOT NULL,
    model text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
    error_code text,
    worker_id text,
    lease_token text,
    lease_until timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE(draft_id,idempotency_key)
);
CREATE INDEX eval_annotation_claim ON eval_annotation_jobs(created_at,id)
    WHERE status='queued';
CREATE INDEX eval_annotation_draft ON eval_annotation_jobs(draft_id,created_at DESC);

CREATE TABLE eval_dataset_versions (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES eval_spaces(id),
    source_draft_id text REFERENCES eval_dataset_drafts(id),
    source_snapshot_id text REFERENCES eval_source_snapshots(id),
    derived_from_version_id text REFERENCES eval_dataset_versions(id),
    name text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('atomic_replay','golden_regression')),
    content jsonb NOT NULL,
    content_sha256 text NOT NULL,
    review_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    content_purged_at timestamptz
);
CREATE INDEX eval_dataset_space ON eval_dataset_versions(space_id,created_at DESC);

CREATE TABLE eval_experiments (
    id text PRIMARY KEY,
    space_id text NOT NULL REFERENCES eval_spaces(id),
    dataset_version_id text NOT NULL REFERENCES eval_dataset_versions(id),
    source_experiment_id text REFERENCES eval_experiments(id),
    name text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('atomic_replay','golden_regression')),
    status text NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','completed','failed','cancelled','uncertain')),
    manifest jsonb NOT NULL,
    request_sha256 text NOT NULL,
    idempotency_key text NOT NULL,
    fde_subject text,
    review_status text NOT NULL DEFAULT 'pending'
      CHECK (review_status IN ('pending','in_progress','completed')),
    report_revision integer NOT NULL DEFAULT 0,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz,
    evidence_purged_at timestamptz,
    UNIQUE(space_id,idempotency_key)
);
CREATE INDEX eval_experiment_space ON eval_experiments(space_id,created_at DESC);
CREATE INDEX eval_experiment_active ON eval_experiments(created_at)
  WHERE status IN ('queued','running');
CREATE INDEX eval_experiment_review ON eval_experiments(fde_subject,review_status)
    WHERE mode='golden_regression' AND archived_at IS NULL;

CREATE TABLE eval_jobs (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES eval_experiments(id),
    source_job_id text REFERENCES eval_jobs(id),
    case_id text NOT NULL,
    repeat_index integer NOT NULL CHECK(repeat_index >= 1),
    stage text NOT NULL CHECK(stage IN ('execute','judge')),
    status text NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed','cancelled','uncertain')),
    input jsonb NOT NULL,
    result jsonb,
    result_sha256 text,
    runtime_engine_sha256 text,
    error_code text,
    worker_id text,
    lease_token text,
    lease_until timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE(experiment_id,case_id,repeat_index,stage)
);
CREATE INDEX eval_job_claim ON eval_jobs(created_at,id) WHERE status='queued';
CREATE INDEX eval_job_experiment ON eval_jobs(experiment_id,status);
CREATE INDEX eval_job_lease ON eval_jobs(lease_until) WHERE status='running';

CREATE TABLE eval_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES eval_experiments(id),
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_event_replay ON eval_events(experiment_id,id);

CREATE TABLE eval_reviews (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id text NOT NULL REFERENCES eval_jobs(id),
    subject text NOT NULL,
    revision integer NOT NULL CHECK(revision>0),
    label text NOT NULL,
    issue_kind text NOT NULL DEFAULT 'none'
      CHECK (issue_kind IN ('none','gold_error','judge_error','agent_issue','environment_issue')),
    note text NOT NULL,
    outputs_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id,subject,revision)
);
CREATE INDEX eval_review_job ON eval_reviews(job_id,created_at DESC);

CREATE TABLE eval_report_exports (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES eval_experiments(id),
    revision integer NOT NULL CHECK (revision > 0),
    status text NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
    object_key text,
    content_sha256 text,
    error_code text,
    worker_id text,
    lease_token text,
    lease_until timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    object_purged_at timestamptz,
    UNIQUE(experiment_id,revision)
);
CREATE INDEX eval_report_export_claim ON eval_report_exports(created_at,id)
    WHERE status='queued';

CREATE TABLE eval_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject text NOT NULL,
    space_id text,
    action text NOT NULL,
    resource_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
