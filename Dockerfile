# Estágio 1: resolve dependências exatas do lockfile num venv isolado.
FROM python:3.12-slim AS build
WORKDIR /src
COPY requirements.lock pyproject.toml ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock \
 && /opt/venv/bin/pip install --no-cache-dir --no-deps .

# Estágio 2: runtime sem pytest/ruff, sem compilador, usuário não-root.
FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    DB_PATH=/app/var/app.db
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY data ./data
RUN useradd --uid 10001 --create-home appuser && mkdir -p /app/var && chown appuser /app/var
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
