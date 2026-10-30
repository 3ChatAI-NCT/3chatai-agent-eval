FROM python:3.12-slim-bookworm

ARG PIP_INDEX_URL

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir --disable-pip-version-check '.[cloud]' \
    && groupadd --system --gid 10001 eval \
    && useradd --system --uid 10001 --gid eval --home /app eval \
    && chown -R eval:eval /app

USER 10001
EXPOSE 8080
CMD ["python", "-m", "agent_eval.cli", "api", "--host", "0.0.0.0"]
