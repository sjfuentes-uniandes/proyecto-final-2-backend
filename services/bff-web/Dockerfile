# Construir desde la raíz del repo: docker build -f services/bff-web/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package bff-web

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PORT=8080 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --chmod=755 libs/solventa-common/healthcheck /app/healthcheck
RUN useradd --uid 10001 --create-home appuser
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 CMD ["/app/healthcheck"]
CMD ["uvicorn", "bff_web.main:app", "--host", "0.0.0.0", "--port", "8080"]
