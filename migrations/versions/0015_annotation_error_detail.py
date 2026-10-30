"""保留 AI 起草失败的安全阶段及校验详情。"""
from alembic import op

revision = "0015_annotation_error_detail"
down_revision = "0014_space_model_changes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE eval_annotation_jobs ADD COLUMN error_detail jsonb")


def downgrade():
    op.execute("ALTER TABLE eval_annotation_jobs DROP COLUMN error_detail")
