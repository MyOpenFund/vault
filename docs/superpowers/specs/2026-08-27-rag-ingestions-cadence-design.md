# Vault groundwork for the RAG chain: `rag_ingestions` + `cadence` tables

**Date:** 2026-08-27
**Status:** validated by Marc (brainstorm 2026-08-27), ready for implementation planning
**Scope:** step 2 of the RAG session program — everything the vault needs before the
RAG orchestrator is rewired onto the eigenmind engine (step 3).

## Context

The vault is the stack's central metadata database. Two pieces of RAG-related state
currently live outside it:

- **RAG ingestion state**: RAGDataOrchestrator tracks what it has ingested into
  Qdrant in a JSONL file ledger (`{doc_id, chunks, ts}`). This is promoted to a
  Postgres table so that "what is in the RAG" becomes a SQL query (drift detection,
  Metabase views, resume logic).
- **Cadence report**: central-bank-corpus's cadence watchdog (its PR #12) writes
  `data/cadence.jsonl` — a full snapshot, regenerated weekly, of one row per
  `(bank_code, doc_type)` series with publication-cadence fields. The orchestrator
  dashboard's "Upcoming & overdue" screen was meant to load this file; instead the
  file is ingested into a vault table and the screen becomes a Metabase view.

## Decisions (with the reasoning that settled them)

1. **Direct SQL, no write API.** The orchestrator writes `rag_ingestions` by
   connecting to Postgres directly. The vault repo owns the DDL and documents the
   write contract. Rationale: every table has exactly one writer (documents → vault
   ingestion service, rag_ingestions → orchestrator); an HTTP write path would move
   the same schema coupling into JSON while adding auth, versioning and a network
   dependency mid-ingestion; Metabase already reads directly. Escalation path if
   writers multiply: shared client package first, API only if third-party writers
   ever appear (rule of three).
2. **The vault ingestion service ingests cadence.jsonl.** central-bank-corpus keeps
   its pure JSONL handoff (no DB coupling in a public corpus repo); the orchestrator
   stays out of corpus infrastructure. The vault service already walks every
   `*.jsonl` under `DATA_DIR` and currently skips cadence lines one by one with
   warnings — the mandatory special-casing becomes the feature.
3. **`rag_ingestions` is current-state, upserted.** Keyed `(doc_id, collection)`; a
   re-ingest updates the row. Cross-model history is naturally captured because each
   re-embed campaign targets a fresh Qdrant collection. A `rag_runs` campaign-journal
   table was considered and deferred (tracked in the Obsidian Open Questions).
4. **Fact columns now, probe later.** `documents` gains nullable `has_text_layer`
   and `page_count` columns in this step; the probe pass that fills them ships with
   step 3 (the OCR policy that consumes them). The target schema is complete in one
   migration train and step 3 no longer touches the vault.
5. **No ledger migration.** The current MiniLM 384-d Qdrant collection is disposable
   (full re-embed planned at engine switch). `rag_ingestions` starts empty with the
   new chain. The old file ledger stays as an archive in the orchestrator repo until
   that repo's cleanup (program step 7).

## Design

### 1. Table `rag_ingestions`

```sql
CREATE TABLE IF NOT EXISTS rag_ingestions (
    doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
    collection        TEXT NOT NULL,
    corpus            TEXT NOT NULL,
    source_code       TEXT,
    embedding_model   TEXT NOT NULL,      -- e.g. 'intfloat/multilingual-e5-base'
    embedding_version TEXT,               -- our policy versioning (E5 prefixes, chunking params...)
    chunk_count       INTEGER NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, collection)
);
CREATE INDEX IF NOT EXISTS idx_rag_ingestions_collection ON rag_ingestions(collection);
CREATE INDEX IF NOT EXISTS idx_rag_ingestions_corpus_source ON rag_ingestions(corpus, source_code);
```

- **FK to `documents(doc_id)`**: the orchestrator only ever ingests documents it
  selected *from* the vault, so the parent row exists by construction; the FK turns
  any drift into an immediate write error. `documents` rows are never hard-deleted
  (soft-delete only), so the FK never blocks legitimate operations.
- **`corpus` / `source_code` are denormalized** (derivable by joining `documents`):
  accepted for Metabase view simplicity and parity with the Qdrant payload.
- **Write contract** (documented in the README for the orchestrator):
  `INSERT ... ON CONFLICT (doc_id, collection) DO UPDATE SET` every non-key column.
  Resume logic on the orchestrator side is the anti-join
  `documents ⟕ rag_ingestions WHERE rag_ingestions.doc_id IS NULL AND collection = X`.
- **DDL execution**: the vault ingestion service runs the DDL at startup, in the
  same idempotent migration train as the existing `documents` DDL. The orchestrator
  needs INSERT/UPDATE/SELECT rights only, never DDL.

### 2. Table `cadence`

```sql
CREATE TABLE IF NOT EXISTS cadence (
    corpus            TEXT NOT NULL,
    source_code       TEXT NOT NULL,      -- mapped from the file's bank_code
    doc_type          TEXT NOT NULL,
    last              DATE,
    interval_days     INTEGER,
    next_expected     DATE,
    days_until        INTEGER,
    status            TEXT,               -- 'on-track' | 'soon' | 'overdue'
    expected_per_year INTEGER,
    n_3y              INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL,
    extra             JSONB,              -- unknown future fields, same pattern as documents
    PRIMARY KEY (corpus, source_code, doc_type)
);
```

- Mirrors the frozen producer contract (`compute_series` in central-bank-corpus:
  `{bank_code, doc_type, last, interval_days, next_expected, days_until, status,
  expected_per_year, n_3y}`) with two vault-vocabulary adaptations: `bank_code` →
  `source_code` (same mapping rule as the documents ingester) and a `corpus` column
  (service default) so a future company-corpus cadence coexists.
- **Full-replace semantics**: each run executes `DELETE FROM cadence WHERE corpus = %s`
  followed by the snapshot's inserts, in one transaction — faithful to the file,
  which is itself atomically regenerated in full each time.
- **Empty-snapshot guard**: an existing but empty `cadence.jsonl` does NOT replace
  the table (warning + skip), mirroring the documents sweep guard philosophy: an
  implausible mass disappearance is treated as a torn input, not truth.
- Unknown fields on a line go to `extra` (JSONB); lines missing any of the three
  key fields are skipped with a warning.

### 3. Cadence ingester + documents-scan exclusion

- New module `ingestion/ingest_cadence.py` in the vault repo, following
  `ingest.py`'s conventions: env-driven (`DATABASE_URL`, `DATA_DIR`, `CORPUS`),
  idempotent, same logging format. It looks for `DATA_DIR/cadence.jsonl`
  (non-recursive: the producer writes the file at the corpus data root), with a
  `CADENCE_PATH` env override for deployments where the mount point differs.
  It parses, maps, and replaces; `updated_at` is stamped with the run timestamp
  (UTC), like the documents ingester's `last_seen_at`.
- The service run chains documents ingestion then cadence ingestion (same container,
  same cron entry).
- `ingest.py`'s recursive scan gains a **basename exclusion list**
  `{cadence.jsonl, cadence_state.jsonl}` — `cadence_state.jsonl` is the watchdog's
  private state file and never enters the vault. This also silences today's
  per-line "no doc_id" warnings.

### 4. Fact columns on `documents`

```sql
ALTER TABLE documents ADD COLUMN IF NOT EXISTS has_text_layer BOOLEAN;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS page_count INTEGER;
```

- Nullable; filled by the step-3 probe pass, which is their **only writer** (via
  `UPDATE`).
- **Deliberately absent** from `KNOWN_FIELDS` and from the manifest upsert column
  list: if they were upserted from manifests, every nightly run would overwrite
  probed values with NULL. A code comment at the upsert site records this invariant.

### 5. Testing

In the existing vault suite (unit + throwaway-Postgres integration CI):

- Unit: cadence line parsing, `bank_code` → `source_code` mapping, unknown fields →
  `extra`, missing key fields → skip, empty-snapshot guard decision, documents-scan
  exclusion list.
- Integration: full-replace is transactional (failure mid-replace leaves the
  previous snapshot intact); `rag_ingestions` upsert contract (insert then
  conflicting insert updates); FK rejects an unknown `doc_id`; fact columns survive
  a manifest re-upsert untouched.
- Adversarial fixtures per house rules: corrupt JSON line, contradicting corpus,
  empty file, singleton series — perfect fixtures hide seam bugs.

### 6. Out of scope for this step

- The probe pass filling `has_text_layer`/`page_count` (step 3, orchestrator).
- The orchestrator's actual writes to `rag_ingestions` (step 3 rewiring).
- Metabase views over the new tables (step 6 dashboard sunset; Charles's
  Metabase re-pointing is a separate follow-up).
- `rag_runs` campaign journal (deferred, in Obsidian Open Questions).
- NAS deployment/re-pointing of the ingestion service (infra follow-up).

## Process

Feature branch `feat/rag-ingestions-cadence` in the vault repo; local commits only —
nothing is pushed to the MyOpenFund org until Marc validates the PR. This spec lives
on the branch under `docs/superpowers/specs/` and moves to the `documentation`
branch at the end of the chantier (house rule: process artifacts never reach main).
