# syntax=docker/dockerfile:1
# OdooBridger Agent: reaches an Odoo on your private network for OdooBridger.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /agent
COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --create-home --uid 10003 agent
COPY odoobridger_agent.py .
USER agent
ENTRYPOINT ["python", "odoobridger_agent.py"]
