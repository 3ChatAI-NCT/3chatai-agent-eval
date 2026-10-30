"""EVAL 空间执行模型的变更记录：当前值以网关为准，这里只留谁在何时从什么改成了什么。"""

from alembic import op

revision = "0014_space_model_changes"
down_revision = "0013_data_archive"
branch_labels = None
depends_on = None

SCHEMA = """
CREATE TABLE eval_space_model_changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id text NOT NULL REFERENCES eval_spaces(id),
    tenant_id text NOT NULL,
    from_model text NOT NULL,
    previous_model text,
    model text,
    subject text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_space_model_change_space ON eval_space_model_changes(space_id,created_at DESC);
"""


def upgrade():
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade():
    op.get_bind().exec_driver_sql("DROP TABLE eval_space_model_changes")
