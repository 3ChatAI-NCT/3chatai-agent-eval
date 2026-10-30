"""启动时建表：在真实 PostgreSQL 上验证全新库能建好表、重复执行不出错。"""

import os
import uuid

import psycopg
import pytest
from pydantic import SecretStr

from agent_eval.cli import migrate
from agent_eval.config import Settings
from agent_eval.store import Store

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="需要 TEST_DATABASE_URL"
)


@pytest.fixture()
def fresh_database():
    admin = os.environ["TEST_DATABASE_URL"]
    name = f"migrate_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0")
    base, _, _ = admin.rpartition("/")
    yield f"{base}/{name}"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f"DROP DATABASE {name} WITH (FORCE)")


def settings(dsn: str) -> Settings:
    return Settings(database_url=SecretStr(dsn), environment="development", dev_identity=True)


def test_fresh_database_gets_every_table_and_becomes_ready(fresh_database):
    store = Store(fresh_database)
    store.open()
    try:
        assert not store.ready()
        migrate(settings(fresh_database))
        assert store.ready()
    finally:
        store.close()
    with psycopg.connect(fresh_database) as c:
        tables = c.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'eval_%'"
        ).fetchone()[0]
    assert tables == 19


def test_running_again_is_a_no_op(fresh_database):
    migrate(settings(fresh_database))
    migrate(settings(fresh_database))
    with psycopg.connect(fresh_database) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            "0015_annotation_error_detail")
