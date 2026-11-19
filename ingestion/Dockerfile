FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir psycopg2-binary==2.9.10

# Build context is the repo root (compose.yaml), hence the ingestion/ prefixes:
# the contract package lives in contract/, outside ingestion/.
COPY ingestion/ingest.py ingestion/ingest_cadence.py ingestion/ingest_runs.py ingestion/ingest_discovery_errors.py ingestion/common.py ingestion/roles.py ingestion/schema_gate.py .

# vault-contract (contract/), copied next to ingest.py rather than pip-installed:
# /app is on sys.path because ingest.py runs from it, and the package has no
# dependency, so a plain copy is the whole install.
COPY contract/src/vault_contract ./vault_contract

CMD ["python", "ingest.py"]
