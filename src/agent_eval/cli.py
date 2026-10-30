import argparse
import json
from pathlib import Path

from .config import Settings
from .store import Store

ROOT = Path(__file__).resolve().parents[2]


def migrate(settings: Settings) -> None:
    """API 与 Worker 启动时升级表结构，以数据库咨询锁串行化并发迁移。"""
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url.get_secret_value()
    command.upgrade(config, "head")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["api", "worker", "retention", "sync-registry"])
    parser.add_argument("--apply", action="store_true",
                        help="apply eligible retention deletions; default is dry run")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.command == "api":
        import uvicorn

        migrate(Settings())
        uvicorn.run("agent_eval.api:create_app", factory=True, host=args.host, port=args.port)
    else:
        from .worker import serve
        settings = Settings()
        if args.command == "worker":
            migrate(settings)
        store = Store(settings.database_url.get_secret_value())
        store.open()
        try:
            if not store.ready():
                raise RuntimeError("Run database migrations before starting worker")
            if args.command == "worker":
                serve(store, settings)
            elif args.command == "retention":
                from .objects import create_objects
                from .retention import sweep

                print(json.dumps(sweep(store, create_objects(settings), apply=args.apply)))
            else:
                from .registry import sync

                print(json.dumps(sync(store, settings, "registry-cli")))
        finally:
            store.close()


if __name__ == "__main__":
    main()
