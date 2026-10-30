"""原子回放的候选模型 key：用户逐次填写，加密保存，评测结束即清除。"""

from alembic import op

revision = "0004_candidate_api_key"
down_revision = "0003_capture_error_message"
branch_labels = None
depends_on = None

SCHEMA = """
ALTER TABLE eval_experiments ADD COLUMN candidate_api_key text;
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
