# Only used to deploy (Fly.io runs images). Locally, `uv run` needs no container.

FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes do not re-download them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    RATE_DESK_DB=/data/rate-desk.db
# The SQLite file lives on a mounted volume at /data, so it survives deploys.
VOLUME /data
EXPOSE 8080
CMD ["rate-desk", "serve", "--host", "0.0.0.0", "--port", "8080"]
