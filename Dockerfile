# ============================================================================
# Stage 1: builder — install deps into a virtualenv
# ============================================================================
FROM python:3.14.5-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install deps first — separate layer for cache reuse
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY app/ ./app/
RUN uv sync --frozen --no-dev

# ============================================================================
# Stage 2: runtime — minimal image with just the venv and app source
# ============================================================================
FROM python:3.14.5-slim AS runtime

RUN groupadd --system --gid 1000 nonroot \
    && useradd --system --uid 1000 --gid nonroot --no-log-init --create-home nonroot

WORKDIR /app

COPY --from=builder --chown=nonroot:nonroot /app/.venv /app/.venv
COPY --from=builder --chown=nonroot:nonroot /app/app /app/app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

USER nonroot

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
