"""评测空间记录飞书台账中的源空间名称，便于内部成员识别客户。"""

from alembic import op

revision = "0005_space_source_name"
down_revision = "0004_candidate_api_key"
branch_labels = None
depends_on = None

SCHEMA = """
ALTER TABLE eval_spaces ADD COLUMN source_name text NOT NULL DEFAULT '';
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
