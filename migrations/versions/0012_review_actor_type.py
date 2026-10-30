"""区分业务人员确认与 AI 辅助意见，辅助意见不计人工复核进度。"""

from alembic import op

revision = "0012_review_actor_type"
down_revision = "0011_turn_traces"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql("""
        ALTER TABLE eval_reviews ADD COLUMN actor_type text NOT NULL DEFAULT 'human'
            CHECK (actor_type IN ('human', 'ai_assisted'));
        UPDATE eval_reviews SET actor_type='ai_assisted'
        WHERE starts_with(note, '本轮校准复核（Codex，待业务负责人确认）：')
            AND issue_kind='judge_error';
    """)


def downgrade():
    raise RuntimeError("不支持丢失复核来源；请保留来源字段")
