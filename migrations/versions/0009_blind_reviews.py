"""人工盲评：评测结束后按比例抽样，揭晓前所有人看不到 AI 结论与模型身份。

每条开启盲评的评测一行：抽样结果（含每条样本的 A/B 顺序）、揭晓人与时间，
以及揭晓时锁定的盲评结论；揭晓后的修订仍写入 eval_reviews，与锁定结论分开统计。
"""

from alembic import op

revision = "0009_blind_reviews"
down_revision = "0008_batches"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_blind_reviews (
    experiment_id text PRIMARY KEY REFERENCES eval_experiments(id),
    sample jsonb NOT NULL,
    drawn_at timestamptz NOT NULL DEFAULT now(),
    revealed_at timestamptz,
    revealed_by text,
    skip_reason text,
    snapshot jsonb
);
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
