"""删除连接配置与主责 FDE 字段：token 改为按需从飞书台账读取，报告复核对内部成员开放。"""

from alembic import op

revision = "0002_drop_connections_and_fde"
down_revision = "0001_platform"
branch_labels = None
depends_on = None

SCHEMA = """
DROP INDEX eval_experiment_review;
ALTER TABLE eval_experiments DROP COLUMN fde_subject;
ALTER TABLE eval_spaces
    DROP COLUMN source_connection,
    DROP COLUMN target_connection,
    DROP COLUMN fde_subject;
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
