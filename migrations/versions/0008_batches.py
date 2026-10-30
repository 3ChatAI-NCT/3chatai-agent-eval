"""批量评测：一次对多个空间采集样本，采集成功后自动用同一套候选配置开跑评测。

每个空间一个条目，记录它的采集任务、自动创建的评测，以及未能开跑的原因。
"""

from alembic import op

revision = "0008_batches"
down_revision = "0007_candidate_presets"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_batches (
    id text PRIMARY KEY,
    name text NOT NULL,
    spec jsonb NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE eval_batch_items (
    id text PRIMARY KEY,
    batch_id text NOT NULL REFERENCES eval_batches(id),
    space_id text NOT NULL REFERENCES eval_spaces(id),
    capture_id text NOT NULL REFERENCES eval_capture_jobs(id),
    experiment_id text REFERENCES eval_experiments(id),
    error_message text,
    UNIQUE (batch_id, space_id)
);
CREATE INDEX eval_batch_items_pending ON eval_batch_items (capture_id)
    WHERE experiment_id IS NULL AND error_message IS NULL;
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
