# vault

Metadata management for a corpus of central bank documents (PDFs on an SMB
share, indexed by `.jsonl` manifests). Ingests manifest metadata into
PostgreSQL and exposes it for querying — without touching or duplicating
the source files.

Scope is metadata only: no full-text search, no OCR, no content indexing.

Once deployed, the endpoints are:

| Endpoints | Links |
| :--- | :--- |
| API documentation | `http://<your-host>:8000/docs` |
| Metabase | `http://<your-host>:3000` |

## Configuration

All deployment-specific values (credentials, host paths) live in a
gitignored `.env` file — never in committed files:

```bash
cp .env.example .env   # then fill in your values
docker compose up -d
```

## How it works

```
SMB share
├── manifest/   .jsonl files (metadata)
└── raw/        actual PDF files

manifest/ ──▶ ingestion ──▶ PostgreSQL ──▶ Metabase (browsing/charts)
                                       └──▶ API ──▶ CLI (vaultctl)
                                             │
                                            raw/ (file download)
```

- **ingestion** — script, not a service. Parses every `.jsonl` under
  `manifest/`, upserts each row into `documents` keyed on `doc_id`.
  Idempotent, safe to re-run. Runs via a **cron job on the host, every
  hour**, keeping the database in sync as manifests are updated.
  Also tracks disappearances: every ingested row gets a `last_seen_at`
  timestamp, and rows absent from **all** manifests are soft-deleted
  (`deleted_at` set, never hard-deleted). A document that reappears in a
  manifest is automatically resurrected. Two safety guards protect against
  torn/partial share syncs: the sweep is skipped when a run finds zero
  valid rows, and when more than `SWEEP_MAX_DELETE_FRACTION` (default
  0.05) of live rows would disappear at once — a partially synced share
  looks exactly like a mass deletion and must not be trusted.
  One ingestion service runs per corpus (its own manifest mount and
  `CORPUS` value). The sweep only ever touches rows of this service's corpus.
- **PostgreSQL** — source of truth for metadata. Relational, not NoSQL:
  every manifest record has the same shape, so a table fits naturally.
  Unexpected fields fall into an `extra` JSONB column instead of breaking
  ingestion. Not exposed outside the compose network.
- **Metabase** — point-and-click filtering/charting on top of Postgres.
  **Treated as a temporary/stopgap tool** to get querying and dashboards
  quickly; may be replaced by a custom dashboard later.
- **API** (FastAPI) — list/filter/sort documents, aggregate stats, and
  file download (only component with read access to `raw/`). No auth,
  internal network only. Soft-deleted documents are excluded from
  `/documents` listings by default (`include_deleted=true` to see them)
  and always excluded from `/stats/summary`; direct lookups by `doc_id`
  (metadata and file download) intentionally still work on soft-deleted
  documents, for audit purposes.
- **CLI (`vaultctl`)** — thin client for the API. No direct DB or file access.

## Data model

One manifest line = one document:

```json
{
  "bank_code": "us",
  "doc_type": "C1",
  "title": "...",
  "date": "2010-01-13",
  "year": 2010,
  "language": "en",
  "provenance": "bis_index",
  "local_path": "data/raw/us/C1/2010/c184d44f298ff622.pdf",
  "doc_id": "c184d44f298ff622"
}
```

Documents belong to a **corpus** (`central-bank`, later `company`, …):
the registry key is the couple `(corpus, source_code)`. Legacy manifests
emit `bank_code`, which maps to `source_code` at ingestion; new corpus
builders must emit `source_code` and a `corpus` field on every line from
day one. The ingestion service's `CORPUS` variable provides the default
for manifests without the field, and a line whose `corpus` contradicts
the service's value is rejected (never guessed) with a logged count.

Maps to a `documents` table (one column per known field, `extra` JSONB for
the rest, plus `last_seen_at`/`deleted_at` lifecycle timestamps). File
download resolves `local_path` against `raw/` at request time — the
database never stores file content.

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

Unit tests cover manifest parsing, corpus resolution, WHERE-clause
building, raw path resolution and the sweep-guard decision. A
real-PostgreSQL integration suite covers the schema migration, upsert,
soft-delete/resurrection, the partial-sync guard and corpus scoping; it
needs docker and is excluded from the default run:

```bash
.venv/bin/pytest -m integration
```

## Design notes

- Each component has access to only what it needs: `ingestion` → manifests
  only, `api` → raw files, `postgres`/`metabase` → no file access at all.
- The API is the single integration point; CLI and any future tool go
  through it rather than touching Postgres or the files directly.
- Documents are never hard-deleted: the manifests are the source of truth,
  and the DB mirrors them with an audit trail (`last_seen_at`, `deleted_at`).
