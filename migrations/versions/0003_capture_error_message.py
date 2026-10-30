"""采集任务记录可展示的失败原因：只保存平台自己抛出的校验与配置错误。"""

from alembic import op

revision = "0003_capture_error_message"
down_revision = "0002_drop_connections_and_fde"
branch_labels = None
depends_on = None

SCHEMA = """
ALTER TABLE eval_capture_jobs ADD COLUMN error_message text;
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
