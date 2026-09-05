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
| `postgres` | the vault itself (PostgreSQL 16) | internal only (publishable on one interface — see [Access & roles](#access--roles)) |
| `ingestion` | one-shot manifest → Postgres sync (run per corpus) | — |
| `api` | FastAPI read API | 8000 |
| `metabase` | dashboards over the vault | 3000 |

## Schema

### Table `documents`

Central registry of documents across all ingested corpora. One row per document sourced from the manifest `.jsonl` files. Surrogate primary key is `id` (SERIAL PRIMARY KEY); upsert key is `doc_id` (TEXT UNIQUE NOT NULL, the ON CONFLICT target). Columns: `id` (PK), `doc_id` (unique, upsert key), `corpus`, `source_code` (e.g. `ecb`, `fed`), `doc_type`, `title`, `pdf_url`, `source_url`, `date`, `year`, `language`, `provenance`, `mime_type`, `sha256`, `local_path`, plus timestamps (`created_at`, `updated_at`, `last_seen_at`) and deletion tracking (`deleted_at`). Unknown manifest fields fall into `extra` (JSONB).

Write contract (the ingestion service is the only writer):

Every manifest column (corpus, source_code, doc_type, title, pdf_url, source_url, date, year, language, provenance, mime_type, sha256, local_path) is overwritten from the manifest on upsert; deleted_at is cleared to NULL (row resurrection); id and created_at are never updated. The `extra` column (unknown manifest fields) is replaced from the manifest on every upsert, like every other manifest column: a key the producer stops emitting disappears from the vault on the next run, and a line with no unknown fields sets `extra` to NULL. The manifest is regenerated whole on every producer run, so this is convergence to the producer's current truth, not data loss — but it is a one-way door, decided 2026-09-04 (vault #8, #2).

Soft-delete semantics: rows absent from all manifests in a run are marked with `deleted_at`; rows that reappear are resurrected (`deleted_at` cleared). Hard deletes never happen. A sweep guard prevents mass-deletions from torn/partial share syncs. `last_seen_at` is monotone — it only ever advances (see Concurrency below) — so a slower concurrent run can never make a live row look stale to a newer run's sweep.

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

Run telemetry for every stack tool: one row per run, append-only for content
(`INSERT ... ON CONFLICT (run_id) DO UPDATE SET corpus = EXCLUDED.corpus WHERE
runs.corpus IS NULL` — no stored column is ever rewritten, so producer-side
file rotation is always safe; a stored row's `corpus` is rewritten only when
it is NULL, a one-time repair of the rows ingested before the column
existed). A `run_id` repeated
inside one file keeps its first occurrence. Columns: `run_id` (PK), `tool`, `command`, `started_at`,
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

One row per failure fingerprint `sha256(corpus|source_code|context|url|error_class)`, ingested from the producer's `data/discovery_errors.jsonl` by `ingest_discovery_errors.py`. The producer's file is append-only and never rotated, so it is a full-history snapshot: `occurrences` is the count in the file (assigned, never incremented — re-ingestion is idempotent), `first_seen_at` is the min event time; `last_seen_at` is the event time of the latest record (producer-stamped lines rank above ingestion-stamped ones, so it can move backwards once at the `ts` cutover). Until the producer emits `ts`, event time is ingestion time and `seen_at_is_ingestion_time` is TRUE; `resolved_at` is not populated today, and the ingester never clears it once a human sets it, so a fingerprint that recurs after being manually resolved stays marked resolved until someone clears it. Guards: a missing file is a no-op; zero valid rows leave the table untouched; a file holding fewer than `DISCOVERY_ERRORS_MIN_RETAIN_FRACTION` (default 0.5) of the rows stored for the corpus leaves it untouched (set 0.0 to accept a rotation). Messages are cut at `DISCOVERY_ERROR_MAX_CHARS` (default 2000).

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

### Concurrency

Each ingestion run holds two session-level Postgres advisory locks on its own connection: `vault-ddl` around the DDL train only (the `CREATE TABLE`/`ALTER`/view block above, serialized across every service and corpus), then `vault-ingest-<corpus>` for the documents, cadence, runs, and discovery-error passes, held through the end of the run. A run that finds a lock already held logs a WARNING naming the key and blocks until it is free — it never skips its work. Both locks are released on explicit unlock or when the connection closes, so a killed run never leaves one behind — that holds for a killed *process* (the OS closes the socket and Postgres releases the session's locks immediately); a vanished *host* keeps them until TCP keepalive notices the dead peer, which can take minutes. On an hourly cron, one `waiting for it` WARNING per overlap is the expected signal that the lock did its job, not an error to page on. Two overlapping runs of the *same* corpus serialize; different corpora run in parallel and never block each other. `documents.last_seen_at` is written as `GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at)` on upsert, so even a writer that bypassed the lock could not rewind a stamp a newer run already set. The orchestrator's own writes (`runs`, `rag_ingestions`, and the probe `UPDATE` of `has_text_layer`/`page_count` on `documents`) take neither lock — a separate writer outside the ingestion service's transaction. Calling `ingest_cadence.run()` / `ingest_runs.run()` / `ingest_discovery_errors.run()` directly, outside `ingest.py`'s `main()`, bypasses the DDL lock entirely (each issues its own `CREATE TABLE IF NOT EXISTS`).

### Migration (2026-09 substrate)

Additive. Run `docker compose run --rm ingestion` once after deploying: the train adds the indexes, `runs.corpus` (+ backfill), `discovery_errors` and the views, then the documents pass refreshes `extra` (on that first run `extra` is rewritten on every live row; `updated_at` moves as it does on every run; row counts do not change). Check `/stats/summary` total, `SELECT count(*) FROM source_health` (one row per cadence series) and `SELECT count(*) FROM discovery_errors` (non-zero only if the mount reaches the corpus `data/` root). Metabase needs Admin → Databases → Sync schema to see the views.

Runbook: if the producer legitimately rotates (truncates) `discovery_errors.jsonl`, the retain-fraction guard above will otherwise leave the table stuck on the pre-rotation snapshot — run `docker compose run --rm -e DISCOVERY_ERRORS_MIN_RETAIN_FRACTION=0.0 ingestion` once to accept the drop (or set the variable in `.env`), then revert to the default. The upsert never deletes: after such a run, fingerprints absent from the new file keep the `occurrences` they had before the rotation, and only the fingerprints present in it are reset to their post-rotation count.

### Migration (2026-09 access & roles)

Not additive: this one needs two variables in the deployment's `.env` **before** the new tree is
pulled, because the API's DSN now refuses to interpolate without one of them (`compose up` stops
with `required variable VAULT_READONLY_PASSWORD is missing a value`).

1. Add `VAULT_ORCHESTRATOR_PASSWORD` and `VAULT_READONLY_PASSWORD` to the deployment's `.env`
   (see `.env.example`) — first, before pulling.
2. `docker compose run --rm ingestion` — the DDL train creates the two roles and issues their
   grants. Nothing is run by hand against the cluster.
3. `docker compose up -d api` — the API is recreated (its environment changed). Until step 2 has
   run, it crash-loops loudly: one `ERROR` naming `vault_readonly` and the command that fixes it,
   then a non-zero exit, retried by `restart: unless-stopped`.

Then, as operator steps outside the repo: point Metabase's **analytics** connection at
`vault_readonly` in the UI (Admin → Databases → the vault connection → user/password; Metabase's
own *application* database keeps `docuser`, which needs DDL on its own schema), and — on the NAS
compose only — uncomment the `ports:` block and set `POSTGRES_BIND_ADDR` to the tailnet address if
the off-host orchestrator must reach Postgres. Rollback is `git checkout` of the previous tree:
the roles are extra objects, nothing the old services used was dropped or revoked.

### Fact columns on `documents`

`has_text_layer` and `page_count` are nullable facts feeding the RAG's OCR policy. They are written by `data-orchestrator`'s probe pass only — manifests never carry them and the manifest upsert never touches them.

## Access & roles

Every service used to connect as `docuser`, which in the `postgres:16` image is a full cluster
superuser. Two least-privilege roles now do the work instead. They are owned by the DDL train:
the ingestion service creates them and **re-issues their grants on every run** — the views are
dropped and recreated nightly, and a grant dies with its view.

| Object | `docuser` | `vault_orchestrator` | `vault_readonly` |
|---|---|---|---|
| `documents` | superuser (all) | `SELECT`; `UPDATE (has_text_layer, page_count)` only | `SELECT` |
| `runs` | all | `SELECT`, `INSERT` | `SELECT` |
| `rag_ingestions` | all | `SELECT`, `INSERT`, `UPDATE` | `SELECT` |
| `cadence`, `discovery_errors` | all | `SELECT` | `SELECT` |
| the five views | all | `SELECT` | `SELECT` |
| tables/views created later | all | `SELECT` (default privileges) | `SELECT` (default privileges) |
| schema `public` | owner | `USAGE`, no `CREATE` | `USAGE`, no `CREATE` |
| database `documents` | owner | `CONNECT` | `CONNECT` |
| DDL, `DELETE`, `TRUNCATE`, `DROP` | yes | denied | denied |

`vault_orchestrator` is for the off-host `data-orchestrator`; `vault_readonly` is what the API
connects as (read-only by code *and* by grant) and what Metabase's analytics connection should
use. `docuser` stays a superuser — demoting it means re-owning the schema — but after this
change it is used by the **ingestion service only**, plus Metabase's own application database
(`MB_DB_USER`), which legitimately needs DDL on its own schema.

Passwords come from `VAULT_ORCHESTRATOR_PASSWORD` and `VAULT_READONLY_PASSWORD` (see
`.env.example`). Unset or empty → that role is skipped with a WARNING and no grant is issued,
which is how the dev and CI clusters run with no roles at all. **Rotation**: change the value in
`.env`, run `docker compose run --rm ingestion` once (the train re-applies the password every
run), then update the consumer. Nothing is ever run by hand against the cluster.

**First boot ordering.** The roles do not exist until the first ingestion run of *this* tree, so
on any cluster that has not yet run it — a brand-new one, or an existing deployment being upgraded
— the API cannot connect. It says so and stops: one `ERROR` naming the role and the command
to run, then a non-zero exit, with `restart: unless-stopped` retrying — never a silent 500 behind
a green `/health`. On a new deployment, run the ingestion once before (or right after) starting
the API:

```bash
docker compose up -d postgres
docker compose run --rm ingestion     # creates the roles + the schema
docker compose up -d api metabase
```

**Publishing the database port.** `compose.yaml` publishes no Postgres port; the `ports:` block is
committed commented out. Uncomment it in the deployment's own compose (Dockge on the NAS) and set
`POSTGRES_BIND_ADDR=<tailnet-ip>` in that host's `.env` — the real address never enters this repo.
It must always be one specific interface address (`127.0.0.1` is the default), never the
all-zeroes wildcard and never a bind-address-less `5432:5432`: Docker publishes ports by DNAT
*ahead of* the host firewall, so that value is the whole access control. A unit test fails the
build if either form appears. Off-host clients land on the image's `scram-sha-256` rule (its
`pg_hba` trusts only the container's own loopback) and must connect as one of the two roles.

TLS is deliberately not configured: the only off-host transport is a tailnet, which is already
authenticated and encrypted. If Postgres ever has to be reachable outside it, `sslmode=require`
plus a certificate on the host is the next hardening step.

## API

`GET /health` · `GET /documents` (filters: corpus, source, type, dates, pagination) ·
`GET /documents/{doc_id}` · `GET /documents/{doc_id}/file` (serves the raw file from the
read-only mount) · `GET /stats/summary` (totals, by_corpus, by_source_code)

## Metabase

Three dashboards over the vault, defined as code in [`metabase/`](metabase/README.md)
(`cards.json` + `dashboards.json`, the source of truth, edited by hand):

- **Vault — Corpus** — what's in the corpus: volume, shape, coverage, document explorer.
- **Vault — Coverage & QC** — what's missing, late or anomalous.
- **Vault — RAG & Runs** — is the machine healthy.

```bash
METABASE_URL=… METABASE_API_KEY=… python metabase/apply.py [--dry-run] [--archive-examples]
```

(`METABASE_SESSION` works instead of the API key.) Idempotent by name inside the
"Vault" collection: a change made in the Metabase UI is overwritten on the next
apply. To actually remove a card or dashboard, delete it from the JSON — one
merely archived in Metabase but still listed in the JSON is recreated on the
next apply. CI (`tests/`) runs the definitions test (schema/tags/grid, no
network) plus every card's SQL executed against a throwaway Postgres built by
the real ingester. Not covered by any dashboard: live Qdrant health and the
PDF download button, both still behind the API/`vaultctl`.

## Quickstart

```bash
cp .env.example .env        # POSTGRES_PASSWORD, the two role passwords, host paths
docker compose up -d postgres
docker compose run --rm ingestion            # schema + roles, syncs the manifests, exits
docker compose up -d api metabase
```

Order matters on any cluster that has not yet run the new train: the API connects as
`vault_readonly`, a role the ingestion run creates (see [Access & roles](#access--roles)) — that
includes an existing deployment upgrading to this tree, not just a brand-new cluster (see
[Migration (2026-09 access & roles)](#migration-2026-09-access--roles)). Once the roles exist,
`docker compose up -d` brings everything back in any order, and ingestion is re-run on demand or
from cron.

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
.venv/bin/python -m pytest tests/ -q              # unit tests
.venv/bin/python -m pytest tests/ -q -m integration  # needs docker: throwaway Postgres
```

Counts change with every branch; CI is the truth. The integration suite starts a
throwaway `postgres:16` container per module, so it is skipped where docker is not
available.

## License

[MIT](LICENSE). The vault stores metadata about documents; the documents themselves
live with their corpora and keep their own terms.
