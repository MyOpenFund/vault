FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir psycopg2-binary==2.9.10

COPY ingest.py ingest_cadence.py ingest_runs.py ingest_discovery_errors.py common.py .

CMD ["python", "ingest.py"]
