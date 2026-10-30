"""全局设置按版本追加保存：最新版本即当前生效值，历史版本保留谁在何时改成了什么。"""

from alembic import op

revision = "0006_setting_revisions"
down_revision = "0005_space_source_name"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_setting_revisions (
    key text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    value jsonb NOT NULL,
    subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (key, revision)
);
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
