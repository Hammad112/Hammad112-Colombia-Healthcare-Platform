FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# The package build needs the source tree. The "seed" extra adds Faker, which is
# imported only when synthetic data is seeded (APP_ENV local or ci).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[seed]"

COPY alembic.ini main.py ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8000"]
