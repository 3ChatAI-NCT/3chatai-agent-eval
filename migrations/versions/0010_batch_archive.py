"""批量评测可归档：归档只从列表隐藏，批次与各空间评测一起归档、一起恢复。"""

from alembic import op

revision = "0010_batch_archive"
down_revision = "0009_blind_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("ALTER TABLE eval_batches ADD COLUMN archived_at timestamptz")


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
