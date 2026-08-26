# vault

Central metadata database for [MyOpenFund](https://github.com/MyOpenFund) corpora —
a PostgreSQL vault that indexes what each corpus holds, without touching or
duplicating the documents themselves.

Corpus builders (e.g. [central-bank-corpus](https://github.com/MyOpenFund/central-bank-corpus))
produce `.jsonl` manifests next to their raw files; the vault ingests those manifests
into Postgres and exposes them for querying. **Metadata only**: no full-text search,
no OCR, no content indexing — titles, dates, document types, provenance, file paths.

## Design

- **Multi-corpus by construction**: every document is identified by
  `(corpus, source_code)` — e.g. `(central-bank, ecb)` — so a second corpus
  (companies, news, …) is one more ingestion service block, zero schema change.
- **Manifests are the source of truth**: ingestion is idempotent (upserts) and mirrors
  deletions as **soft-deletes** (`deleted_at`), guarded by `SWEEP_MAX_DELETE_FRACTION`
  so a torn/unmounted share can never mass-delete rows.
- **Read-only mounts**: the ingestion container sees the corpus data read-only;
  the vault can never corrupt a corpus.

## Services (`compose.yaml`)

| Service | Role | Port |
|---|---|---|
| `postgres` | the vault itself (PostgreSQL 16) | internal only |
| `ingestion` | one-shot manifest → Postgres sync (run per corpus) | — |
| `api` | FastAPI read API | 8000 |
| `metabase` | dashboards over the vault | 3000 |

## API

`GET /health` · `GET /documents` (filters: corpus, source, type, dates, pagination) ·
`GET /documents/{doc_id}` · `GET /documents/{doc_id}/file` (serves the raw file from the
read-only mount) · `GET /stats/summary` (totals, by_corpus, by_source_code)

## Quickstart

```bash
cp .env.example .env        # set POSTGRES_PASSWORD + host paths (never committed)
docker compose up -d postgres metabase api
docker compose run --rm ingestion            # sync the manifests, then exits
```

## CLI

```bash
pip install -e cli/
vaultctl stats                                # corpus totals from the API
vaultctl list --source ecb --type D1          # query documents
vaultctl get <doc_id>                         # one document's metadata
vaultctl download <doc_id>                    # fetch the raw file via the API
```

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q          # 32 unit tests
# integration tests (6) need a live Postgres — CI runs them against a service container
```

## License

[MIT](LICENSE). The vault stores metadata about documents; the documents themselves
live with their corpora and keep their own terms.
