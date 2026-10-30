"""参考数据与采集记录支持可恢复归档。"""
from alembic import op

revision = "0013_data_archive"
down_revision = "0012_review_actor_type"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("eval_capture_jobs", "eval_source_snapshots", "eval_dataset_drafts",
                  "eval_dataset_versions"):
        op.get_bind().exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN archived_at timestamptz")


def downgrade():
    for table in ("eval_dataset_versions", "eval_dataset_drafts", "eval_source_snapshots",
                  "eval_capture_jobs"):
        op.get_bind().exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN archived_at")
