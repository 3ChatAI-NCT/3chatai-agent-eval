"""逐轮回放的模型调用链路：每次对话请求的回放请求 ID 在发送前登记，执行后据此到网关日志采集。

一行对应一个 execute 任务的一次尝试。完整链路正文存 OSS（保留 60 天），这里只留索引与状态。
"""

from alembic import op

revision = "0011_turn_traces"
down_revision = "0010_batch_archive"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_turn_traces (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES eval_experiments(id),
    job_id text NOT NULL REFERENCES eval_jobs(id),
    attempt integer NOT NULL,
    request_id text NOT NULL UNIQUE,
    request_id_echoed text,
    echo_verified boolean,
    workspace_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','complete','summary_only','partial','not_found','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    due_at timestamptz,
    deadline_at timestamptz,
    poll_count integer NOT NULL DEFAULT 0,
    lease_owner text,
    lease_until timestamptz,
    expected_calls integer,
    call_count integer,
    summary jsonb,
    object_key text,
    content_sha256 text,
    collected_at timestamptz,
    object_purged_at timestamptz,
    error_code text,
    UNIQUE(job_id, attempt)
);
CREATE INDEX eval_turn_trace_due ON eval_turn_traces(status, due_at);
CREATE INDEX eval_turn_trace_experiment ON eval_turn_traces(experiment_id);
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
