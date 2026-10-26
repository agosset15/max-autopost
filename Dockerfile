FROM ghcr.io/astral-sh/uv:python3.13-alpine AS builder

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN uv sync --locked --no-dev --no-cache --compile-bytecode \
    && find .venv -type d -name "__pycache__" -exec rm -rf {} + \
    && rm -rf .venv/lib/python3.13/site-packages/pip* \
    && rm -rf .venv/lib/python3.13/site-packages/setuptools* \
    && rm -rf .venv/lib/python3.13/site-packages/wheel*

FROM python:3.13-alpine AS final

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

COPY ./src ./src

CMD ["python", "-m", "src"]
