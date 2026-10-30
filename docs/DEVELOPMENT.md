# Development

Install the project using the [quick start](../README.md#quick-start). Run commands from the repository root with the Python virtual environment activated.

## Unit tests

```bash
python -m pytest -m 'not postgres' --ignore=tests/test_startup_migration.py
```

This runs the unit suite without the PostgreSQL integration and startup-migration tests.

## PostgreSQL integration tests

Use a dedicated PostgreSQL test database; do not connect the suite to production. The migration tests also create and drop temporary databases, so the test role needs those permissions.

```bash
createdb agent_eval_test
export TEST_DATABASE_URL=postgresql://localhost/agent_eval_test
EVAL_DATABASE_URL="$TEST_DATABASE_URL" python -m alembic upgrade head
python -m pytest
```

Adjust the local connection string for your PostgreSQL user, host, and database. Tests use synthetic fixtures and mocked external services; live integration verification is a separate step.

## Lint and build

```bash
ruff check src tests migrations
npm --prefix frontend run build
```

## Project layout

| Path | Purpose |
| --- | --- |
| `frontend/` | React, TypeScript, Vite, and HeroUI workbench |
| `src/agent_eval/` | FastAPI, PostgreSQL job queue, workers, registry, and reports |
| `src/atomic_eval/` | Call Replay constraints, pairwise judging, and aggregation |
| `src/golden_eval/` | Turn Replay contracts, execution, profiles, and scoring |
| `prompts/` | Versioned judge and annotation prompts |
| `migrations/` | Alembic schema migrations |
| `tests/` | Unit and PostgreSQL integration tests |

For settings and deployment requirements, see [configuration](OPERATIONS.md). For configuration changes, synthetic data, and documentation maintenance, see [contributing](../CONTRIBUTING.md).
