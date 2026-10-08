# Construir desde la raíz del repo: docker build -f services/clientes/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --package clientes

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=build /app /app
RUN useradd --uid 10001 --create-home appuser
USER 10001
EXPOSE 8080
CMD ["uvicorn", "clientes.main:app", "--host", "0.0.0.0", "--port", "8080"]
