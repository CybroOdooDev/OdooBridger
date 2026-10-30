# A stand-in ZKTeco BioTime server. Standard library only; tzdata is the one
# package, because the slim image carries no timezone database.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MOCK_PORT=8100 \
    MOCK_DATA_DIR=/data

RUN pip install --no-cache-dir tzdata \
 && useradd --system --uid 10001 --no-create-home mock \
 && mkdir /data && chown mock /data

WORKDIR /app
COPY server.py .

USER mock
# History, employees and (above all) transaction ids live here: keep it on a
# volume so a rebuilt container carries on where the last one stopped.
VOLUME /data
EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('MOCK_PORT','8100'),timeout=2)"

ENTRYPOINT ["python", "server.py"]
