FROM python:3.13-slim

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# vault-query: the shared read queries (query/), installed from the same checkout.
# Build context is the repo root (compose.yaml), hence the api/ prefixes.
COPY query /tmp/query
RUN pip install --no-cache-dir /tmp/query && rm -rf /tmp/query

COPY api/main.py api/db.py api/models.py ./

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
