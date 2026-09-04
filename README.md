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

Every manifest column (corpus, source_code, doc_type, title, pdf_url, source_url, date, year, language, provenance, mime_type, sha256, local_path) is overwritten from the manifest on upsert; deleted_at is cleared to NULL (row resurrection); id and created_at are never updated. The `extra` column (unknown manifest fields) is replaced from the manifest on every upsert, like every other manifest column: a key the producer stops emitting disappears from the vault on the next run, and a line with no unknown fields sets `extra` to NULL. The manifest is regenerated whole on every producer run, so this is convergence to the producer's current truth, not data loss — but it is a one-way door, decided 2026-09-04 (vault #8, #2).

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
`truncated` flag), `corpus`, `extra`. `corpus` is the ingesting service's
`CORPUS` for rows arriving through `runs.jsonl` (a line carrying a
contradicting `corpus` is rejected, not guessed); the corpus-agnostic
`data-orchestrator` leaves it NULL. `source_health` only sees runs with a
corpus.

Producers: `central-bank-corpus` appends `data/runs.jsonl` (ingested by this
service, same handoff as `cadence.jsonl`); `data-orchestrator` writes rows
directly over SQL (`tool = "data-orchestrator"`; rows written under the
pre-rename identity `rag-orchestrator` are renamed by the DDL train). The
per-source `truncated` flag is the load-bearing signal: a discovery that
stopped on a fetch failure says so explicitly instead of looking like a
completed listing.

### Table `discovery_errors`

One row per failure fingerprint `sha256(corpus|source_code|context|url|error_class)`, ingested from the producer's `data/discovery_errors.jsonl` by `ingest_discovery_errors.py`. The producer's file is append-only and never rotated, so it is a full-history snapshot: `occurrences` is the count in the file (assigned, never incremented — re-ingestion is idempotent), `first_seen_at`/`last_seen_at` are the min/max event time. Until the producer emits `ts`, event time is ingestion time and `seen_at_is_ingestion_time` is TRUE; `resolved_at` is not populated today, and the ingester never clears it once a human sets it, so a fingerprint that recurs after being manually resolved stays marked resolved until someone clears it. Guards: a missing file is a no-op; zero valid rows leave the table untouched; a file holding fewer than `DISCOVERY_ERRORS_MIN_RETAIN_FRACTION` (default 0.5) of the rows stored for the corpus leaves it untouched (set 0.0 to accept a rotation). Messages are cut at `DISCOVERY_ERROR_MAX_CHARS` (default 2000).

### Views

Dropped and recreated by every ingestion run, in dependency order, without CASCADE — nothing outside the ingester's DDL train may depend on them (Metabase and the agent reference them by name at query time, which is fine).

| view | grain | what it answers |
|---|---|---|
| `runs_sources` | one row per (run, source) | the base for every runs-shaped question; no time window; defensive casts (garbage counters → NULL, non-boolean `truncated` → FALSE, non-array `error_samples` → `[]`, non-array `sources` → no rows) |
| `rag_backlog` | (document, collection) | documents missing from each collection seen in `rag_ingestions` — the campaign resume query. **Empty when `rag_ingestions` has no rows** |
| `rag_backlog_any` | document | live documents in no collection at all — correct on a fresh deployment |
| `source_health` | (corpus, source_code, doc_type) | expected (cadence) × observed (runs) per series |
| `sources_without_cadence` | (corpus, source_code) | sources that run but have no cadence row; empty is healthy |

A `runs.sources` element with a NULL `source_code` surfaces in `sources_without_cadence` as a `(corpus, NULL)` row — deliberately loud rather than silently dropped.

Consumer contract:

1. Lookups into `documents.extra` use `@>` containment (`extra @> '{"entity_key": "e42"}'`), not `->>` equality — only the former uses `idx_documents_extra_gin`.
2. Any `doc_type` query carries `corpus`: `doc_type` is free text and collides across corpora.
3. `source_*` columns of `source_health` are source-grain (`runs.sources` has no `doc_type`) and repeat identically across a source's doc_type rows.
4. `last_run_outcome` is run-grain (a run is degraded if any source failed); source-grain health is `source_truncated_runs_7d`, `source_fetch_errors_7d`, `source_zero_yield_runs_7d`. The `last_run_*` columns come from the run with the latest `finished_at` for that source, ties broken by `run_id`.
5. Pair every backlog card with a live-document count: backlog 0 with documents 0 means "no data", not "done".
6. `discovery_errors.last_seen_at` is ingestion time while `seen_at_is_ingestion_time` is TRUE.
7. The 7-day / 90-day windows of `source_health` are choices, named in the columns; use `runs_sources` for any other window. No anomaly threshold lives in a view — the detector owns it as a documented config value.
8. Indexes: `idx_documents_live_agg` serves the per-source drill-down cards (not the whole-corpus rollup, which correctly seq-scans); `idx_documents_corpus_doc_type` is used once a second corpus exists.

### Migration (2026-09 substrate)

Additive. Run `docker compose run --rm ingestion` once after deploying: the train adds the indexes, `runs.corpus` (+ backfill), `discovery_errors` and the views, then the documents pass refreshes `extra` (on that first run `extra` is rewritten on every live row; `updated_at` moves as it does on every run; row counts do not change). Check `/stats/summary` total, `SELECT count(*) FROM source_health` (one row per cadence series) and `SELECT count(*) FROM discovery_errors` (non-zero only if the mount reaches the corpus `data/` root). Metabase needs Admin → Databases → Sync schema to see the views.

Runbook: if the producer legitimately rotates (truncates) `discovery_errors.jsonl`, the retain-fraction guard above will otherwise leave the table stuck on the pre-rotation snapshot — run `docker compose run --rm -e DISCOVERY_ERRORS_MIN_RETAIN_FRACTION=0.0 ingestion` once to accept the drop (or set the variable in `.env`), then revert to the default. The upsert never deletes: after such a run, fingerprints absent from the new file keep the `occurrences` they had before the rotation, and only the fingerprints present in it are reset to their post-rotation count.

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
.venv/bin/python -m pytest tests/ -q          # 73 unit tests
# integration tests (59) need a live Postgres — CI runs them against a service container
```

## License

[MIT](LICENSE). The vault stores metadata about documents; the documents themselves
live with their corpora and keep their own terms.
