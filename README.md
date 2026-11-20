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
  `(corpus, doc_id)` (a `doc_id` is unique within its corpus only) and carries its
  `source_code` (e.g. `ecb`), so a second corpus (companies, news, …) is one more
  ingestion service block, zero schema change.
- **Manifests are the source of truth**: ingestion is idempotent (upserts) and mirrors
  deletions as **soft-deletes** (`deleted_at`), guarded by `SWEEP_MAX_DELETE_FRACTION`
  so a torn/unmounted share can never mass-delete rows.
- **Read-only mounts**: the ingestion container sees the corpus data read-only;
  the vault can never corrupt a corpus.

## Contract

What a manifest row must hold is the **documents contract**, documented in
[`contract/README.md`](contract/README.md) (fields, refusal and notice reasons, the
`vocabulary.json` file, versioning) and shipped as the `vault_contract` package: the
ingestion checks every row with it, producers pin it to write and test their rows, and
`python -m vault_contract.check` runs it over a manifest directory. A document is
`(corpus, doc_id)`: a `doc_id` is unique within its corpus only. Hence, for the RAG: Qdrant
collections are per corpus today, so a point id derived from `doc_id` alone is unique, but **a
collection mixing corpora must put `corpus` in its point ids**.

## Services (`compose.yaml`)

| Service | Role | Port |
|---|---|---|
| `postgres` | the vault itself (PostgreSQL 16) | internal only (publishable on one interface — see [Access & roles](#access--roles)) |
| `ingestion` | one-shot manifest → Postgres sync, central-bank corpus | — |
| `ingestion-company` | the same image, `CORPUS=company`, over a copy of the company data root (profile `company`: `docker compose run --rm ingestion-company`, or `COMPOSE_PROFILES=company` — see [Deploy notes](#deploy-notes-2026-11-documents-contract)) | — |
| `api` | FastAPI read API | 8000 |
| `metabase` | dashboards over the vault | 3000 |

## Schema

### Table `documents`

Central registry of documents across all ingested corpora. One row per document sourced from the manifest `.jsonl` files. Surrogate primary key is `id` (SERIAL PRIMARY KEY); identity and upsert key is `(corpus, doc_id)` (`UNIQUE (corpus, doc_id)`, the ON CONFLICT target, since the documents contract: a `doc_id` is unique within its corpus only). Columns: `id` (PK), `doc_id`, `corpus`, `source_code` (e.g. `ecb`, `fed`), `doc_type`, `title`, `document_url`, `source_url`, `date`, `year`, `language`, `provenance`, `mime_type`, `sha256`, `local_path`, `date_precision`, `date_kind`, `entity_scheme`, `entity_value` (the 18 common fields of the [documents contract](contract/README.md)), plus timestamps (`created_at`, `updated_at`, `last_seen_at`) and deletion tracking (`deleted_at`). Unknown manifest fields fall into `extra` (JSONB). `document_url` is the document's own URL (a PDF or any other format); it was named `pdf_url` before 2026-11 (see [Migration (2026-11 document_url rename)](#migration-2026-11-document_url-rename)).

Write contract (the ingestion service is the only writer):

Every manifest column (corpus, source_code, doc_type, title, document_url, source_url, date, year, language, provenance, mime_type, sha256, local_path) is overwritten from the manifest on upsert; deleted_at is cleared to NULL (row resurrection); id and created_at are never updated. The `extra` column (unknown manifest fields) is replaced from the manifest on every upsert, like every other manifest column: a key the producer stops emitting disappears from the vault on the next run, and a line with no unknown fields sets `extra` to NULL. The manifest is regenerated whole on every producer run, so this is convergence to the producer's current truth, not data loss — but it is a one-way door, decided 2026-09-04 (vault #8, #2).

Soft-delete semantics: rows absent from all manifests in a run are marked with `deleted_at`; rows that reappear are resurrected (`deleted_at` cleared). Hard deletes never happen. A sweep guard prevents mass-deletions from torn/partial share syncs. `last_seen_at` is monotone — it only ever advances (see Concurrency below) — so a slower concurrent run can never make a live row look stale to a newer run's sweep.

Which files are manifests (the run logs the layout it picked):
- **data/ root layout** — `<DATA_DIR>/manifest/` is a directory (the repo's `compose.yaml`, where `DATA_DIR` is the corpus `data/` root): only `manifest/**/*.jsonl` and the legacy monolithic `manifest.jsonl` are read, minus the known producer file names should one land inside `manifest/`. `cadence.jsonl`, `runs.jsonl`, the producer's private and audit files, and any other root file are never treated as manifests; another `*.jsonl` at the root is named in a WARNING (it is not read).
- **manifest-directory layout** — no `manifest/` directory (e.g. a deployment that mounts `data/manifest` itself as `DATA_DIR` and points `CADENCE_PATH`/`RUNS_PATH`/`DISCOVERY_ERRORS_PATH` at the producer files): every `**/*.jsonl` under `DATA_DIR`, minus the known producer file names — the same set the scan has always read.
- **`MANIFEST_GLOBS`** (optional, comma-separated globs relative to `DATA_DIR`, `**` recursive) overrides both and is taken literally; an empty entry, an absolute pattern or a `..` component fails the run before it connects.

In every mode only a directory whose name matches is skipped (with a WARNING). A matching file that cannot be read — a dangling symlink, a permission or I/O error on the share — fails the run before the sweep, so its rows are never mistaken for deleted ones.

A `doc_id` repeated inside one manifest file keeps its **last valid line** (what two upserts in line order would store); the run logs a WARNING with the number of dropped lines and up to five of the repeated `doc_id`s (`DUPLICATE_SAMPLE_SIZE` in `ingest.py`). It used to abort the whole run (vault #6). `SWEEP_MAX_DELETE_FRACTION` unset or empty means the default `0.05`; any value that is not a number in `[0, 1]` fails the run before it connects, naming the value.

### Table `rag_ingestions`

Current-state registry of what the RAG has ingested into which Qdrant collection: one row per `(corpus, doc_id, collection)` (the primary key), upserted by `data-orchestrator` over plain SQL. Columns: `doc_id` and `corpus` (foreign key `(corpus, doc_id)` to `documents`), `collection`, `source_code`, `embedding_model`, `embedding_version`, `chunk_count`, `ingested_at`.

Write contract (`data-orchestrator` is the only writer):

    INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code,
        embedding_model, embedding_version, chunk_count)
    VALUES (...)
    ON CONFLICT (corpus, doc_id, collection) DO UPDATE SET
        corpus = EXCLUDED.corpus, source_code = EXCLUDED.source_code,
        embedding_model = EXCLUDED.embedding_model,
        embedding_version = EXCLUDED.embedding_version,
        chunk_count = EXCLUDED.chunk_count, ingested_at = now();

The pre-contract key `ON CONFLICT (doc_id, collection)` no longer works: the transitional
unique index `rag_ingestions_doc_collection_transition (doc_id, collection)` that served it
during the documents-contract deploy is dropped by the DDL train (see
[transition cleanup](#migration-2026-11-documents-contract-transition-cleanup)), and such a
statement now fails with `there is no unique or exclusion constraint matching the ON CONFLICT
specification`, writing nothing.

"Documents not yet in the RAG" is the anti-join on this table; drift between the vault and Qdrant becomes a SQL query. Cross-model history lives in the collection dimension: each re-embed campaign targets a fresh collection.

### Table `cadence`

Publication-cadence report, one row per `(corpus, source_code, doc_type)` series — the corpus producer's `data/cadence.jsonl` snapshot (a frozen 9-field contract) ingested by the service with full-replace semantics, scoped to the service's corpus. An empty snapshot never replaces existing rows (torn-input guard). `cadence_state.jsonl` is the producer's private state and is excluded from all vault ingestion, as is `cadence.jsonl` itself from the documents manifest scan. A snapshot line carrying a `corpus` field that contradicts the service's `CORPUS` is skipped with a WARNING (same rule as manifest lines): a foreign corpus's `cadence.jsonl` mounted by mistake is never relabeled as this one's, and a snapshot made only of such lines leaves the table untouched.

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

Dropped and recreated by the DDL train whenever it runs (see [Schema migrations](#schema-migrations): only when the train's SQL changed or a relation is missing, not every night), in dependency order, without CASCADE — nothing outside the ingester's DDL train may depend on them (Metabase and the agent reference them by name at query time, which is fine).

| view | grain | what it answers |
|---|---|---|
| `runs_sources` | one row per (run, source) | the base for every runs-shaped question; no time window; defensive casts (garbage, negative or out-of-range counters — more than 9 digits — → NULL, non-boolean `truncated` → FALSE, non-array `error_samples` → `[]`, non-array `sources` → no rows); `bad_content` is NULL when the producer did not report it |
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
9. `bad_content` (central-bank-corpus, since 2026-11-19) is a **sub-count of `docs_failed`**: documents whose every candidate URL served the wrong bytes (an HTML page for a PDF, a stub, a truncated PDF), as opposed to a dead URL. NULL means *not reported* (another producer, or a run before 2026-11-19), never 0 — but 0 does not always mean *checked*: a run that downloads nothing (e.g. a `discover` without download) reports 0 too: `runs_sources.bad_content`, `source_health.source_bad_content_7d` (sum over the 7-day window; NULL when no run of the window reported it, unlike its `coalesce(…, 0)` siblings) and `source_health.last_run_bad_content` (the value on the run behind the `last_run_*` columns) all keep that distinction. These three columns come last in their views.

### Concurrency

Each ingestion run holds two session-level Postgres advisory locks on its own connection: `vault-ddl` around the DDL train only (the `CREATE TABLE`/`ALTER`/view block above, serialized across every service and corpus), then `vault-ingest-<corpus>` for the documents, cadence, runs, and discovery-error passes, held through the end of the run. A run that finds a lock already held logs a WARNING naming the key and blocks until it is free — it never skips its work. Both locks are released on explicit unlock or when the connection closes, so a killed run never leaves one behind — that holds for a killed *process* (the OS closes the socket and Postgres releases the session's locks immediately); a vanished *host* keeps them until TCP keepalive notices the dead peer, which can take minutes. On an hourly cron, one `waiting for it` WARNING per overlap is the expected signal that the lock did its job, not an error to page on. Two overlapping runs of the *same* corpus serialize; different corpora run in parallel and never block each other. `documents.last_seen_at` is written as `GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at)` on upsert, so even a writer that bypassed the lock could not rewind a stamp a newer run already set. The orchestrator's own writes (`runs`, `rag_ingestions`, and the probe `UPDATE` of `has_text_layer`/`page_count` on `documents`) take neither lock — a separate writer outside the ingestion service's transaction. Calling `ingest_cadence.run()` / `ingest_runs.run()` / `ingest_discovery_errors.run()` directly, outside `ingest.py`'s `main()`, bypasses the DDL lock entirely (each issues its own `CREATE TABLE IF NOT EXISTS`).

### Schema migrations

The DDL train (the `CREATE TABLE`/`ALTER`/view block in `ingestion/ingest.py`) takes ACCESS
EXCLUSIVE locks: `DROP VIEW` and `ALTER TABLE` (even a no-op `ADD COLUMN IF NOT EXISTS`) wait
behind any open reader — Metabase, the API, the MCP — and every new reader then queues behind
them (#4). So the train is **gated** (`ingestion/schema_gate.py`):

- It runs only when the sha256 of its SQL differs from the one recorded in the one-row table
  `vault_schema`, when one of the tables or views it owns is missing, or when
  `VAULT_DDL_FORCE=1`. Otherwise the run logs `schema current (fingerprint …) — DDL train
  skipped` and issues no DDL against an existing table, index or view (see the per-pass list
  below). Editing the train (comments included) re-runs it once.
- When it runs, each lock wait is bounded by `VAULT_DDL_LOCK_TIMEOUT` (default `5s`; an
  integer with an optional unit `ms`, `s` or `min`, no unit = milliseconds). Below about `1s`
  the train can lose against autovacuum, which only yields to a waiting lock after
  `deadlock_timeout` (1 s by default). The transaction is retried after each delay of
  `VAULT_DDL_RETRY_DELAYS` (default `10,30` seconds = three attempts; `none` = one attempt).
  Each failed attempt logs one WARNING, prefixed `schema/roles transaction (DDL train + roles)`
  or `(roles only, DDL train skipped)`, listing the sessions holding locks on the relations it
  needs (pid, user, application, state, transaction age — never their query text).
- After the last attempt the run **fails** (non-zero exit, `TrainLockTimeout`) and ingests
  nothing: new code may need the new schema. Re-run when the listed sessions are gone, or with
  a longer `VAULT_DDL_LOCK_TIMEOUT`.
- `VAULT_DDL_FORCE=1` is for drift the gate cannot see (a column dropped or an index changed by
  hand): `docker compose run --rm -e VAULT_DDL_FORCE=1 ingestion`, once. Left set, it brings
  the nightly lock convoy back.
- What runs on every pass, train skipped or not: the roles and grants (see
  [Access & roles](#access--roles); `GRANT`/`REVOKE`/`ALTER ROLE`, inside the same bounded,
  retried transaction), `CREATE TABLE IF NOT EXISTS` for `cadence`, `runs` and
  `discovery_errors` (locks nothing when the table exists), and row-level `INSERT`/`UPDATE`/
  `DELETE`. Every `CREATE INDEX` and the `runs.corpus` `ADD COLUMN` of those passes sit behind
  a `to_regclass` / `information_schema` check, because even as no-ops they would take SHARE
  (waits behind an uncommitted writer) or ACCESS EXCLUSIVE (waits behind any reader). Tested:
  a skipped pass completes beside a reader holding every table and view, and beside
  uncommitted `INSERT`s into `runs` and `discovery_errors`.
- The `rag-orchestrator` → `data-orchestrator` relabel of `runs.tool` runs whenever the train
  runs and on every pass whose `runs.jsonl` offers rows.
- Legacy shapes converge in the train: each of `created_at`, `updated_at`, `last_seen_at`,
  `deleted_at` is upgraded from naive `TIMESTAMP` on its own (`AT TIME ZONE 'UTC'`);
  `bank_code` is renamed to `source_code` only when `source_code` is absent — when both exist,
  `source_code` is filled from `bank_code` where NULL, `bank_code` is **kept** (made nullable),
  and a WARNING on every pass asks for it to be dropped by hand. `pdf_url` is renamed to
  `document_url` the same way: only when `document_url` is absent; when both exist,
  `document_url` is filled from `pdf_url` where NULL, `pdf_url` is kept, and the same per-pass
  WARNING asks for it to be dropped by hand.

First run after deploying the gate: `vault_schema` does not exist yet, so that run applies the
train once (under the timeout) and records the fingerprint; later runs skip it.

**Rollback and mixed versions.** Code from before the gate ignores `vault_schema` and runs its
own train on every pass, so after a rollback to it — and once more after rolling forward again —
the recorded fingerprint can no longer be trusted to describe the schema: run the gated code once
with `VAULT_DDL_FORCE=1`. Two services on different code versions against the same database
(e.g. two corpora deployed at different times) see different fingerprints and re-run the train
alternately, each time the other one has recorded its own — the nightly lock convoy comes back
until both run the same tree.

### Migration (2026-11 documents contract transition cleanup)

Both corpora are migrated to the [documents contract](contract/README.md) and every reader is
updated (deploy of 2026-11-20), so the transition code goes. One DDL train re-run:

- `DROP INDEX IF EXISTS rag_ingestions_doc_collection_transition`: the unique index on
  `rag_ingestions (doc_id, collection)` that kept the pre-contract data-orchestrator's
  `ON CONFLICT (doc_id, collection)` working. Only the contract's key
  `(corpus, doc_id, collection)` serves an upsert now. No value is touched, no view changes
  (the train recreates them as on every train run), and a fresh database never gets the index.

**Rollback** of this cleanup: redeploy the previous tree (master `eff1aec`) and run its
ingestion once; its train text differs, so it re-runs and recreates the index. Nothing else
to undo.

### Migration (2026-11 documents contract)

The ingestion checks every manifest row against the [documents contract](contract/README.md)
(`vault_contract.check_row`) and the schema follows it. One DDL train re-run:

- `documents`: columns `date_precision`, `date_kind`, `entity_scheme`, `entity_value`; identity
  `UNIQUE (doc_id)` → `UNIQUE (corpus, doc_id)`; a plain index on `doc_id` for lookups by
  `doc_id` alone; an index on `(entity_scheme, entity_value)`.
- `rag_ingestions`: primary key `(corpus, doc_id, collection)`, foreign key `(corpus, doc_id)`;
  the backlog views join on both. **Transition:** a unique index
  `rag_ingestions_doc_collection_transition (doc_id, collection)` keeps the deployed
  data-orchestrator's `ON CONFLICT (doc_id, collection)` working until its update (valid while
  collections are per corpus; removed in code by the cleanup PR's train, never by hand — see
  [Table `rag_ingestions`](#table-rag_ingestions)).
- New tables `vocabulary` (the corpus vocabulary, full replace per corpus) and
  `ingestion_refusals` (the refused rows of each corpus's last run, replaced every run;
  `value` is the repr of the refused value cut at 200 characters).

Before deploying, check that no `rag_ingestions` row has another corpus than its document:
`SELECT count(*) FROM rag_ingestions r JOIN documents d USING (doc_id) WHERE r.corpus <> d.corpus;`
must be 0. Otherwise the train stops with that count (one transaction: nothing changes) and the
run fails. Measured: the train takes well under a second on 40k documents.

**Refused rows.** A row the contract refuses (unknown code, bad format, `corpus` of another
corpus, a line that is not a JSON object such as a torn write or a UTF-8 BOM on line 1, an
`extra` value JSONB cannot hold) is not written, is logged as `file:line reason field=value`
(the first 100 per run) and lands in `ingestion_refusals`. **Any refused row skips the
soft-delete sweep and makes the run exit 1** after the cadence, runs and discovery-error passes:
a refused row is a document "not seen", and a vocabulary mistake on a whole source must not
soft-delete it. Quality notices (`empty_title`, `precision_not_stated`, transition forms, …) are
counted in the run summary; the row is written. Central-bank `date_precision`/`date_kind`, kept
in `extra` until now, move to their columns on the first run.

**Vocabulary file.** The run reads `vocabulary.json` at `DATA_DIR`, or at `VOCABULARY_PATH`
when set. In the manifest-directory layout (the NAS: `DATA_DIR` is the manifest directory, e.g.
`/cbdata/manifest`) the file sits one level up, at the data root: set
`VOCABULARY_PATH=/cbdata/vocabulary.json`, as `CADENCE_PATH` points one level up too.
- Absent: code membership is not checked (transition); the run logs
  `vocabulary: missing (membership not checked)`.
- Present and valid: it replaces this corpus's `vocabulary` rows (its own transaction, before
  the documents pass) and every row's `source_code`, `doc_type`, `provenance` and
  `entity_scheme` are checked against it.
- Present but unreadable, invalid, of another corpus, or declaring a newer MINOR contract
  version than this vault reads: **the whole run stops before connecting (exit 1)**: no DDL
  train, no documents, no cadence, runs or discovery-error pass. Checking rows against a
  vocabulary the vault cannot read would refuse or accept them on the strength of a producer
  bug or of codes it does not know; nothing is better than a wrong partial run. Fix the file
  (or deploy the vault that reads the newer MINOR) and run again.

**Rollback to the 2d0457e schema: no longer applicable.** Until the
[transition cleanup](#migration-2026-11-documents-contract-transition-cleanup) this section
gave a SQL rollback to the pre-contract keys. It is withdrawn: every consumer has moved past
the contract (data-orchestrator `2695b26` refuses the old schema with `VaultSchemaTooOld`, the
MCP's vault-query 0.5 reads `vocabulary`, the producers' manifests are migrated and their old
code must not run on them), and with two corpora live nothing guarantees that
`UNIQUE (doc_id)` alone still holds. Going back means restoring a
database backup together with every repository's previous release, not a vault-side script.
The historical snippet and its preconditions are in the README of master `eff1aec`
(`git show eff1aec:README.md`).

#### Breaking changes (2026-11 documents contract)

For every consumer of the API, `vaultctl` and `vault-query`, in one place:

- HTTP `GET /documents?doc_type=X` without `corpus` → **400** (`doc_type needs corpus: …`, with
  each corpus's codes). Pass `corpus` with `doc_type`.
- HTTP `GET /documents/{doc_id}` without `corpus` → **a list** (one document per corpus holding
  the `doc_id`), not an object; with `corpus`, one object as before.
- An older `vaultctl` against the new API crashes on `get` (it expects an object): upgrade
  `vaultctl` with the API.
- `/stats/summary.by_doc_type` is now a **top 20 per corpus**, each item carrying its `corpus`
  (it was a top 20 over every corpus summed).
- `GET /documents/{doc_id}/file` reads `CORPUS_ROOTS` + `local_path`; `RAW_DATA_DIR` is gone (see
  the deploy notes for the NAS).
- vault-query 0.5.0 (`get_document` returns a list, `DocTypeNeedsCorpus`, the four new columns)
  needs the new schema (the `vocabulary` table, the new columns).

#### Deploy notes (2026-11 documents contract)

- **Order**: deploy the tree and run the ingestion first (it applies the train: `vocabulary`,
  the new columns and keys), then restart the API. vault-query 0.5.0 (the API's) needs the new
  schema: on the old one it fails on the missing table/columns.
- **MCP server**: vault-query 0.4.0, its current pin, keeps working on the new schema (it reads
  no new table or column), so its bump to 0.5.0 is not urgent; do it after the ingestion has
  run, never before.
- **data-orchestrator**: its writes keep working (the transitional index serves its
  `ON CONFLICT (doc_id, collection)`, see [Table `rag_ingestions`](#table-rag_ingestions)).
  The real constraint is its reads: after the first new ingestion, `date_precision` and
  `date_kind` leave `extra` for their columns and `local_path` loses its `data/` prefix, both of
  which the old orchestrator relies on. So **no old-orchestrator ingestion and no
  `sync-metadata`** between the vault deploy and the orchestrator's update.
- **NAS ingestion**: the ingestion imports the `vault_contract` package, which the NAS (code
  bind-mounted, no image build) must provide on both ingestion services: a read-only bind
  `${VAULT_SRC}/contract/src:/contract:ro` and `PYTHONPATH=/contract`. Extend the deploy's
  `cmp` check to `contract/src/vault_contract/*.py` and `ingestion/ingest_vocabulary.py`. If it
  is forgotten, the ingestion exits 1 at import (`ModuleNotFoundError: vault_contract`) and the
  database is untouched, but the Démarrer has also recreated the api on the new code, which then
  runs against the old schema: add the mount and the variable, then Démarrer again.
- **`.env` rename**: `RAW_DIR` (which pointed at `data/raw`) is replaced by
  `CENTRAL_BANK_DATA_DIR`, the central-bank **data root** (the directory holding `raw/`),
  required by compose. A stale `RAW_DIR` is ignored.
- **NAS api**: it needs `CORPUS_ROOTS=central-bank=/cbdata` (the compose default) **and** the
  data root mounted at `/cbdata` (`${CENTRAL_BANK_DATA_DIR}:/cbdata:ro`). Without the variable
  every download is a 404 naming `CORPUS_ROOTS` (WARNING at startup: downloads disabled);
  without the mount, a startup WARNING says the root is not an existing directory and every
  download is a 404.
- **Company ingestion** (`ingestion-company`, profile `company`): both ingestion services build
  the same image tag (`vault-ingestion:local`), so one `docker compose build` refreshes both.
  Dockge only runs `up`, which skips a profile. Two ways to run it on the NAS:
  - `COMPOSE_PROFILES=company` in the stack's environment: every Démarrer then starts both
    ingestions. They serialise only on the DDL train: each run takes the global `vault-ddl`
    advisory lock around the train alone, releases it, then takes its own `vault-ingest-<corpus>`
    lock, so the two documents passes run in parallel (`ingestion/ingest.py`, `main`).
  - Or a one-shot from a shell: `docker compose run --rm ingestion-company`.
- **`COMPANY_MANIFEST_DIR`** is optional: compose commands work without it. Unset, the company
  service mounts the empty `/var/empty` and, with `REQUIRE_MANIFESTS=1` (set on that service),
  exits 1 before connecting with `No .jsonl manifest files found under /data and
  REQUIRE_MANIFESTS is set` instead of a successful run of 0 rows. `REQUIRE_MANIFESTS` (1/true/
  yes, 0/false/no; default off) works for any ingestion service; the central-bank one keeps the
  historical behaviour (no manifest = a WARNING, the train and producer-file passes still run).

### Migration (2026-11 Metabase runs guards)

No table, no `.env` change, nothing to backfill. A malformed `runs` row (a counter past the int
range, a float, a string, a negative; an object `sources`; a string `truncated`) no longer makes
a card raise:

- **Cards**: R3 and R5 read a `totals` counter as NULL unless it is 1–9 digits; R3 also reads
  a non-array `sources` as empty (`n_sources` 0) and a non-boolean `truncated` as false (R5
  reads neither). Where a card shows `coalesce(docs_ingested, docs_new)` (R3, R5), a malformed
  `docs_ingested` is NULL and so falls back to `docs_new`. R6 now selects from `runs_sources`
  (same columns; a non-array `error_samples` shows as `[]`). R3's `bad_content` no longer
  admits a negative. After merging, re-apply the cards: `python metabase/apply.py`.
- **View**: `runs_sources` reads a negative counter as NULL (it accepted `-?`), so a negative
  `bad_content` can no longer offset a positive one and hide a bank from Q6. Side effect in
  `source_health`: a negative `docs_seen` is now NULL, which `coalesce(docs_seen, 0) = 0`
  counts as a **zero-yield run** (`source_zero_yield_runs_7d`), as a garbage `docs_seen`
  already was. Only the view's regex changes, but the train's text changed, so **the first
  ingestion run after deploying re-runs the DDL train once** (views only: dropped and
  recreated, grants re-issued, the new fingerprint recorded; no table altered), under the
  usual bounded lock wait (see [Schema migrations](#schema-migrations)). Later runs skip it.

### Migration (2026-11 bad_content per source)

Additive. `runs_sources` gains `bad_content`; `source_health` gains `source_bad_content_7d` and
`last_run_bad_content` (contract item 9 under [Views](#views)), all appended after the existing
columns. No table, no `.env` change, nothing to backfill (older runs carry no key: NULL by design).

- The train's text changed, so **the first ingestion run after deploying re-runs the DDL train
  once** (`DDL train running: train changed (recorded …)`): the five views are dropped and
  recreated, no table is altered, grants re-issued, the new fingerprint is recorded in
  `vault_schema`. Readers holding the views (Metabase, the API, the MCP) delay it by at most
  `VAULT_DDL_LOCK_TIMEOUT` per attempt, as for any train (see
  [Schema migrations](#schema-migrations)). The next runs skip it. The follow-up that bounds the
  four older `runs_sources` counters to 9 digits changes the train again: shipped together with
  this one, the train still re-runs only once; shipped later, once more (views only).

Everything below needs the new columns, which exist only **once that first ingestion run has
re-run the train** (the `DDL train running: train changed` line, or `vault_schema.applied_at`
moved), not as soon as the files are deployed:

- Metabase: Admin → Databases → Sync schema (new view columns), then re-apply the cards
  (`python metabase/apply.py`): new card **Q6 · Bad content per bank** on *Coverage & QC*, and
  R3 gains a `bad_content` column.
- `vault-query` 0.4.0 (see [Read queries](#read-queries-vault-query)): `runs_status` rows gain
  `bad_content`. `source_health` returns the view as is, so the MCP's `vault_runs_status` shows
  the two new `source_health` columns as soon as that run is done, before any pin bump (two
  more keys per row: a call whose result sat just under the MCP's size cap may now be refused —
  ask for a smaller `limit`); its `source_runs[].bad_content` needs the pin bump, **after** that
  run (0.4.0 selects a column an older schema does not have). `runs[].totals.bad_content` was
  already visible.

### Migration (2026-11 document_url rename)

`documents.pdf_url` is now `documents.document_url`, and so is the column of the `rag_backlog`
and `rag_backlog_any` views. Nothing to set in `.env`; the first ingestion run after deploying
applies it:

- The train's text changed, so that run re-runs the DDL train once (`DDL train running: train
  changed (recorded …)`), under the usual bounded lock wait: views dropped, `ALTER TABLE
  documents RENAME COLUMN pdf_url TO document_url` (a catalog change — no table rewrite, no
  value touched), views recreated on the new name, grants re-issued in the same transaction.
  Readers holding the tables (Metabase, the API, the MCP) delay it by at most
  `VAULT_DDL_LOCK_TIMEOUT` per attempt; if they never let go, the run fails and ingests nothing
  (see [Schema migrations](#schema-migrations)). Later runs skip the train.
- Manifests: a line carrying `document_url` fills the column; a line still carrying the legacy
  `pdf_url` (a manifest not migrated yet) fills it too, `document_url` winning when both are
  present. `pdf_url` never lands in `extra`. Transition only: the `pdf_url` fallback in
  `ingestion/ingest.py` (`KNOWN_FIELDS`, `parse_line`) is removed once every producer writes
  `document_url`.
- Readers that name the column must move with it: the API's `Document` model and `vault-query`
  0.3.0 (this release) return `document_url`; a consumer pinned to an older `vault-query` (the
  MCP) selects a column that no longer exists and fails until its pin is bumped. Re-apply the
  Metabase cards (`python metabase/apply.py`): card C4's SQL reads `d.document_url`. Questions
  built by hand in the Metabase UI are not in `cards.json`: fix any that name `pdf_url` there.
- **Deploy this before the producers migrate their manifests.** The previous ingestion code does
  not know `document_url`: run on a migrated manifest, it would put the URL in `extra` and set
  the column to NULL on every row until the new code runs. This version accepts both keys, so
  the order is: vault, then the MCP pin bump, then the producers' manifest migration.

### Migration (2026-09 substrate)

Additive. Run `docker compose run --rm ingestion` once after deploying: the train adds the indexes, `runs.corpus` (+ backfill), `discovery_errors` and the views, then the documents pass refreshes `extra` (on that first run `extra` is rewritten on every live row; `updated_at` moves as it does on every run; row counts do not change). Check `/stats/summary` total, `SELECT count(*) FROM source_health` (one row per cadence series) and `SELECT count(*) FROM discovery_errors` (non-zero only if the mount reaches the corpus `data/` root). Metabase needs Admin → Databases → Sync schema to see the views.

Runbook: if the producer legitimately rotates (truncates) `discovery_errors.jsonl`, the retain-fraction guard above will otherwise leave the table stuck on the pre-rotation snapshot — run `docker compose run --rm -e DISCOVERY_ERRORS_MIN_RETAIN_FRACTION=0.0 ingestion` once to accept the drop (or set the variable in `.env`), then revert to the default. The upsert never deletes: after such a run, fingerprints absent from the new file keep the `occurrences` they had before the rotation, and only the fingerprints present in it are reset to their post-rotation count.

### Migration (2026-09 access & roles)

Not additive: this one needs two variables in the deployment's `.env` **before** the new tree is
pulled, because the API's DSN now refuses to interpolate without one of them (`compose up` stops
with `required variable VAULT_READONLY_PASSWORD is missing a value`).

1. Add `VAULT_ORCHESTRATOR_PASSWORD` and `VAULT_READONLY_PASSWORD` to the deployment's `.env`
   (see `.env.example`) — first, before pulling. Generate each with `openssl rand -hex 24`: the
   value must be URL-safe (no `$ / @ : # % ?`), because compose interpolates it and it is
   embedded in a `postgresql://` URL.
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

Follow-up, one-off and outside the train (those databases do not exist on a dev or CI cluster, so
the train cannot converge them): on the NAS, run `REVOKE CONNECT ON DATABASE metabase, postgres
FROM PUBLIC` once, so the two new roles cannot open a session on the databases they have no
business in.

### Fact columns on `documents`

`has_text_layer` and `page_count` are nullable facts feeding the RAG's OCR policy. They are written by `data-orchestrator`'s probe pass only — manifests never carry them and the manifest upsert never touches them.

## Access & roles

Every service used to connect as `docuser`, which in the `postgres:16` image is a full cluster
superuser. Two least-privilege roles now do the work instead. They are owned by the DDL train:
the ingestion service creates them and **re-issues their grants on every run**, whether the
DDL train ran or was skipped — a grant dies with its view whenever the train recreates the views,
and re-issuing it was measured not to wait behind readers.

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
which is how the dev and CI clusters run with no roles at all.

**Password charset**: generate with `openssl rand -hex 24`. The value **must be URL-safe — no
`$ / @ : # % ?`** — because compose interpolates it and it is embedded in a `postgresql://` URL.
`openssl rand -base64` is not safe here: the `ALTER ROLE` accepts it (the train passes it as a
SQL literal), but the resulting DSN for the API and the orchestrator is malformed, and they
crash-loop on a libpq error that does not say "bad password".

**Rotation**: change the value in `.env` — same charset rule — run
`docker compose run --rm ingestion` once (the password is re-applied on every run, DDL train skipped or not), then
update the consumer. Nothing is ever run by hand against the cluster.

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
`GET /documents/{doc_id}?corpus=` · `GET /documents/{doc_id}/file?corpus=` · `GET /stats/summary`
(totals, by_corpus, by_source_code, by_doc_type per corpus)

Identity is `(corpus, doc_id)` (documents contract): a `doc_id` is unique within its corpus only.

- `GET /documents/{doc_id}?corpus=X` returns one document (404 if X has none);
  without `corpus` it returns **a list**, one document per corpus holding that `doc_id`
  (404 when there is none). Rows carry `date_precision`, `date_kind`, `entity_scheme`,
  `entity_value`.
- A `doc_type` filter without `corpus` is a **400** whose detail starts `doc_type needs corpus`
  and lists each corpus's codes with their labels (vault-query's message): central-bank `A1`
  and company `A1` share a name, not a meaning.
- `GET /documents/{doc_id}/file` serves `<corpus root>/<local_path>`, one root per corpus from
  `CORPUS_ROOTS` (`central-bank=/cbdata,company=/companydata`; compose default
  `central-bank=/cbdata`, the mount of `CENTRAL_BANK_DATA_DIR`). The value is checked at startup:
  corpus names are contract codes, each corpus once, roots absolute, not `/`, without `..`,
  whitespace, control characters or `;`; anything else stops the API. A root that is not an
  existing directory is only a startup WARNING (a mount may come later): its downloads are 404
  until it is there. The resolved file must stay under its root (`..`, an absolute path or a
  symlink leading out: 404 `Invalid file path`, as is a path no file can have: a component past
  255 bytes, the whole past 4096); a corpus without a root is a 404 naming `CORPUS_ROOTS`;
  `corpus` is required (400) when the `doc_id` exists in several corpora. `RAW_DATA_DIR` and
  the split on the `raw` path component are gone.
- **Transition note.** The ingestion now stores `local_path` without the old `data/` prefix
  (`raw/...`). Rows ingested before the deploy still hold `data/raw/...` and are a 404 (`File
  not found on disk`) until the first post-deploy ingestion of their corpus rewrites every live
  row; no compatibility code exists for them, so run the ingestion right after deploying.

The document and stats routes read through `vault-query` (below); the image installs it from
the same checkout, which is why `api`'s build context is the repo root (`api/Dockerfile`).

## Read queries (`vault-query`)

[`query/`](query/) is a small Python package holding the vault's read queries once, shared by
the API and the MyOpenFund MCP server. Install it pinned to a commit of this repo:

```bash
pip install "vault-query @ git+https://github.com/MyOpenFund/vault@<sha>#subdirectory=query"
```

`<sha>` is the commit to pin (a merge commit on `master`); pin a SHA rather than a branch so a
consumer's queries only change when it bumps the pin.

- Vocabulary: `list_vocabulary(conn, corpus=None, *, kind=None, limit=1000)` — the codes each
  corpus declares (`vocabulary` table: corpus, kind, code, label, family).
- Documents: `search_documents` (filters, sort, paging, total; optional column projection —
  `SEARCH_COLUMNS` for result lists), `get_document`.
- Counts: `corpus_stats` (live total plus per-field groups, one statement).
- Run health: `recent_runs` (one row per run), `runs_status` (one row per run × source),
  `source_health`, `open_discovery_errors`, `freshness`.
- Guards: `FILTERABLE_FIELDS`, `SORTABLE_FIELDS`, `GROUPABLE_FIELDS`, `ALL_COLUMNS`, and
  `InvalidQuery`, raised before any SQL runs on a bad argument or filter value (the API maps it
  to a 400).

Guarantees: read-only — no function writes or commits the caller's transaction; every function
takes an open psycopg2 connection that the caller owns (opens, closes, and chooses the role —
`vault_readonly`). Use an autocommit connection (or roll back after each call): a read left
"idle in transaction" holds locks that block the ingester's DDL train.

Changes in 0.5.1: `search_documents` breaks sort ties by `doc_id, corpus` (was `doc_id`): two
rows of different corpora sharing a `doc_id` and the sort value now page exactly once each.

Changes in 0.5.0 (breaking — bump the pin together with the vault deploy of the documents
contract; the MCP server's tools must follow):

- `get_document(conn, doc_id, corpus=None)` returns a **list**: with `corpus`, zero or one row;
  without, one row per corpus holding the `doc_id`, ordered by corpus. Unknown: `[]` (was `None`).
- A `doc_type` filter without `corpus` (`search_documents`, `build_where_clause`) and
  `corpus_stats(group_by="doc_type")` without `corpus` raise `DocTypeNeedsCorpus` (an
  `InvalidQuery`, so a `ValueError`); the message starts `doc_type needs corpus` and lists,
  per corpus, `code = label` from `vocabulary`, or the codes in use for a corpus with no
  vocabulary loaded. Value checks (NUL, dates, bounds) still come first, without SQL.
- Rows carry the four contract columns `date_precision`, `date_kind`, `entity_scheme`,
  `entity_value` (in `ALL_COLUMNS`; not in `SEARCH_COLUMNS`). Needs the 2026-11 schema
  (`vocabulary` table, new columns).
- New `list_vocabulary`, `VOCABULARY_KINDS`, `DocTypeNeedsCorpus`.

Changes in 0.4.0 (additive): `RUN_COLUMNS` gains `bad_content`, so every `runs_status` row
carries it (an integer, NULL when the producer did not report it). Needs a vault deployed with
the 2026-11 `bad_content` views: against an older schema `runs_status` fails on the missing
column, so bump a consumer's pin after the first ingestion run that follows the vault deploy. Every other function is unchanged.

Changes in 0.3.0 (breaking — bump the pin together with the vault deploy that renames the
column): `pdf_url` is now `document_url` everywhere — in `ALL_COLUMNS`, in `SEARCH_COLUMNS`, and
so as a key of every row `search_documents` and `get_document` return. 0.2.x selects `pdf_url`
and fails against a renamed vault; 0.3.0 fails against a vault that has not been renamed yet.

Behaviour notes (0.2.0):

- `q` (document search) is a **literal**, case-insensitive substring of the title: `%`, `_` and
  `\` match themselves, so `q="50%"` finds "50%" and no longer acts as a wildcard. (The
  pattern is escaped with `!`, so it reads the same whatever `standard_conforming_strings` is.)
- A string argument holding a NUL character (`q`, a filter value, `corpus`, `source_code`,
  `doc_id`) is `InvalidQuery` before SQL: Postgres text cannot hold one.
- `since` (`runs_status`, `recent_runs`) is a `date`, a `datetime`, or a string in the grammar
  `YYYY-MM-DD[THH:MM[:SS[.ffffff]][Z|+HH:MM|-HH:MM]]` (ASCII digits, upper-case `T`/`Z`, a real
  instant, UTC offset at most ±15:59 — Postgres's limit). Anything else (a week date, a space
  separator, `+16:00`, a number) is `InvalidQuery` before SQL, never a Postgres `DataError`. A
  naive value is read in the session time zone; the caller's value reaches SQL unchanged.
- `search_documents` runs its COUNT and its page in one `REPEATABLE READ, READ ONLY` transaction
  on an idle autocommit connection, so the total and the page agree under a concurrent ingest.
  On a connection already in a transaction (or not in autocommit) it sends no BEGIN, COMMIT or
  ROLLBACK and the caller's isolation level applies. After an error inside its snapshot the
  connection is rolled back and left usable. One connection per concurrent call: do not share
  a connection across threads. The API opens its connections in autocommit, so `/documents`
  gets the snapshot.

## Metabase

Three dashboards over the vault, defined as code in [`metabase/`](metabase/README.md)
(`cards.json` + `dashboards.json`, the source of truth, edited by hand):

- **Vault — Corpus** — what's in the corpus: volume, shape, coverage, document explorer.
- **Vault — Coverage & QC** — what's missing, late or anomalous, and (Q7) the manifest rows
  the last ingestion of each corpus refused.
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
vaultctl list --corpus central-bank --source-code ecb --doc-type D1  # doc_type needs corpus
vaultctl get <doc_id>                         # metadata, one per corpus holding the doc_id
vaultctl get <doc_id> --corpus company        # one document
vaultctl download <doc_id> --corpus central-bank  # the file (corpus needed if the id is in several)
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
