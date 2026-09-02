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

## Schema

### Table `documents`

Central registry of documents across all ingested corpora. One row per document sourced from the manifest `.jsonl` files. Surrogate primary key is `id` (SERIAL PRIMARY KEY); upsert key is `doc_id` (TEXT UNIQUE NOT NULL, the ON CONFLICT target). Columns: `id` (PK), `doc_id` (unique, upsert key), `corpus`, `source_code` (e.g. `ecb`, `fed`), `doc_type`, `title`, `pdf_url`, `source_url`, `date`, `year`, `language`, `provenance`, `mime_type`, `sha256`, `local_path`, plus timestamps (`created_at`, `updated_at`, `last_seen_at`) and deletion tracking (`deleted_at`). Unknown manifest fields fall into `extra` (JSONB).

Write contract (the ingestion service is the only writer):

Every manifest column (corpus, source_code, doc_type, title, pdf_url, source_url, date, year, language, provenance, mime_type, sha256, local_path) is overwritten from the manifest on upsert; deleted_at is cleared to NULL (row resurrection); id and created_at are never updated. The `extra` column (unknown manifest fields) is written at first insert only and not refreshed on later upserts.

Soft-delete semantics: rows absent from all manifests in a run are marked with `deleted_at`; rows that reappear are resurrected (`deleted_at` cleared). Hard deletes never happen. A sweep guard prevents mass-deletions from torn/partial share syncs.

### Table `rag_ingestions`

Current-state registry of what the RAG has ingested into which Qdrant collection: one row per `(doc_id, collection)`, upserted by `data-orchestrator` over plain SQL. Columns: `doc_id` (FK to `documents`), `collection`, `corpus`, `source_code`, `embedding_model`, `embedding_version`, `chunk_count`, `ingested_at`.

Write contract (`data-orchestrator` is the only writer):

    INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code,
        embedding_model, embedding_version, chunk_count)
    VALUES (...)
    ON CONFLICT (doc_id, collection) DO UPDATE SET
        corpus = EXCLUDED.corpus, source_code = EXCLUDED.source_code,
        embedding_model = EXCLUDED.embedding_model,
        embedding_version = EXCLUDED.embedding_version,
        chunk_count = EXCLUDED.chunk_count, ingested_at = now();

"Documents not yet in the RAG" is the anti-join on this table; drift between the vault and Qdrant becomes a SQL query. Cross-model history lives in the collection dimension: each re-embed campaign targets a fresh collection.

### Table `cadence`

Publication-cadence report, one row per `(corpus, source_code, doc_type)` series — the corpus producer's `data/cadence.jsonl` snapshot (a frozen 9-field contract) ingested by the service with full-replace semantics, scoped to the service's corpus. An empty snapshot never replaces existing rows (torn-input guard). `cadence_state.jsonl` is the producer's private state and is excluded from all vault ingestion, as is `cadence.jsonl` itself from the documents manifest scan.

### Table `runs`

Run telemetry for every stack tool: one row per run, append-only
(`INSERT ... ON CONFLICT (run_id) DO NOTHING` — producer-side file rotation is
always safe). Columns: `run_id` (PK), `tool`, `command`, `started_at`,
`finished_at`, `outcome` (`ok` | `degraded` | `failed`), `exit_code`,
`totals` (JSONB), `sources` (JSONB array of per-source stats incl. the
`truncated` flag), `extra`.

Producers: `central-bank-corpus` appends `data/runs.jsonl` (ingested by this
service, same handoff as `cadence.jsonl`); `data-orchestrator` writes rows
directly over SQL (`tool = "data-orchestrator"`; rows written under the
pre-rename identity `rag-orchestrator` are renamed by the DDL train). The
per-source `truncated` flag is the load-bearing signal: a discovery that
stopped on a fetch failure says so explicitly instead of looking like a
completed listing.

### Fact columns on `documents`

`has_text_layer` and `page_count` are nullable facts feeding the RAG's OCR policy. They are written by `data-orchestrator`'s probe pass only — manifests never carry them and the manifest upsert never touches them.

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
