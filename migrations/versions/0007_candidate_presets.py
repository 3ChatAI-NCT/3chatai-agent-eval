"""候选模型接口预设：地址、模型与加密的 key，供新建评测直接选用。

key 只以密文保存、接口永不返回；删除为软删除，评测记录里引用的预设仍可追溯。
"""

from alembic import op

revision = "0007_candidate_presets"
down_revision = "0006_setting_revisions"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_candidate_presets (
    id text PRIMARY KEY,
    name text NOT NULL,
    base_url text NOT NULL,
    model text NOT NULL,
    sealed_key text NOT NULL,
    key_hint text NOT NULL,
    note text NOT NULL DEFAULT '',
    revision integer NOT NULL DEFAULT 1,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);
CREATE UNIQUE INDEX eval_candidate_presets_name ON eval_candidate_presets (name)
    WHERE deleted_at IS NULL;
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    raise RuntimeError("不支持破坏性回退；请用已验证的备份恢复")
