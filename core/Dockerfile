# syntax=docker/dockerfile:1
# OdooBridger core: accounts, SSO, billing, leads, staff console, website.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY obcore ./obcore
COPY alembic ./alembic
COPY alembic.ini .
COPY tools ./tools
RUN useradd --create-home --uid 10002 obcore && chown -R obcore:obcore /app
USER obcore
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "obcore.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
