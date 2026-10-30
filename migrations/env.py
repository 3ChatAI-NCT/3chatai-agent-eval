import os

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

# API 与 Worker 进程启动时都会跑一次迁移。用 Postgres 会话级咨询锁串行化，
# 第一个拿到锁的建表，其余等锁释放后发现已是最新版本，直接跳过。
LOCK_ID = 3_200_817_001

url = (context.config.attributes.get("database_url") or os.environ["EVAL_DATABASE_URL"]).replace(
    "postgresql://", "postgresql+psycopg://", 1
)
engine = create_engine(url, poolclass=NullPool)
with engine.connect() as connection:
    connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": LOCK_ID})
    connection.commit()
    try:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
        connection.commit()
