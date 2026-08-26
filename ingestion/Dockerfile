FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir psycopg2-binary

COPY ingest.py .

CMD ["python", "ingest.py"]
