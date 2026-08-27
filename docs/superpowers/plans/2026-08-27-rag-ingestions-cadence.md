# rag_ingestions + cadence Tables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `rag_ingestions` and `cadence` tables plus the `has_text_layer`/`page_count` fact columns to the vault, per the validated spec `docs/superpowers/specs/2026-08-27-rag-ingestions-cadence-design.md`.

**Architecture:** All DDL stays in the vault ingestion service's idempotent migration train (executed on every run). A new flat module `ingestion/ingest_cadence.py` ingests `DATA_DIR/cadence.jsonl` with full-replace semantics; `ingest.py` chains it after the documents pass and excludes cadence files from the documents scan. `rag_ingestions` gets DDL + a tested write contract only — its writer (the orchestrator) arrives in RAG program step 3.

**Tech Stack:** Python 3.13, psycopg2-binary 2.9.10, PostgreSQL 16, pytest (unit + `-m integration` with throwaway docker Postgres).

## Global Constraints

- Everything in English: code, comments, docstrings, commit messages, docs.
- Follow `ingest.py` house conventions exactly: env-driven config, module-level SQL constants, `execute_values(page_size=500)`, `logging` with the existing format, idempotent DDL (`IF NOT EXISTS` / `DO $$` guards).
- Commit style: `feat:` / `docs:` prefixes, imperative, no co-author trailers.
- Never push: local commits on branch `feat/rag-ingestions-cadence` only; Marc validates the PR before anything reaches the MyOpenFund org.
- Unit tests must stay network-free and docker-free (`pytest` default run); DB behavior goes under `@pytest.mark.integration`.
- `has_text_layer` / `page_count` must NEVER enter `KNOWN_FIELDS` or the manifest upsert column list (nightly runs would null probed values).
- Working directory for all commands: `/Users/marc/Desktop/All CODING/MyOpenFund/vault` — run tests with `.venv/bin/python -m pytest`.

---

### Task 1: Exclude cadence files from the documents scan

**Files:**
- Modify: `ingestion/ingest.py` (function `find_jsonl_files`, ~line 186)
- Test: `tests/test_ingest.py` (append)

**Interfaces:**
- Produces: `ingest.EXCLUDED_BASENAMES: frozenset[str]`; `find_jsonl_files(root)` keeps its signature but never returns paths whose basename is in the set.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py`:

```python
# --- documents-scan exclusion of cadence files ---------------------------

def test_find_jsonl_files_excludes_cadence_files(tmp_path):
    from ingest import find_jsonl_files

    (tmp_path / "us.jsonl").write_text("{}\n")
    (tmp_path / "cadence.jsonl").write_text("{}\n")
    (tmp_path / "cadence_state.jsonl").write_text("{}\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "cadence.jsonl").write_text("{}\n")
    (tmp_path / "sub" / "fr.jsonl").write_text("{}\n")

    found = find_jsonl_files(str(tmp_path))
    basenames = sorted(p.split("/")[-1] for p in found)
    assert basenames == ["fr.jsonl", "us.jsonl"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ingest.py::test_find_jsonl_files_excludes_cadence_files -v`
Expected: FAIL — `cadence.jsonl` entries present in the result (assertion mismatch).

- [ ] **Step 3: Implement the exclusion**

In `ingestion/ingest.py`, replace:

```python
def find_jsonl_files(root):
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
```

with:

```python
# Files the documents scan must never treat as manifests: the cadence report
# (ingested by ingest_cadence.py into its own table) and the cadence watchdog's
# private state file (never enters the vault).
EXCLUDED_BASENAMES = frozenset({"cadence.jsonl", "cadence_state.jsonl"})


def find_jsonl_files(root):
    return sorted(
        p
        for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
        if os.path.basename(p) not in EXCLUDED_BASENAMES
    )
```

- [ ] **Step 4: Run the unit suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS (previous count + 1).

- [ ] **Step 5: Commit**

```bash
git add ingestion/ingest.py tests/test_ingest.py
git commit -m "feat: exclude cadence files from the documents manifest scan"
```

---

### Task 2: rag_ingestions DDL + fact columns in the migration train

**Files:**
- Modify: `ingestion/ingest.py` (constant `CREATE_TABLE_SQL`, end of the SQL string; comment near `UPSERT_SQL`)
- Test: `tests/integration/test_rag_ingestions.py` (create)

**Interfaces:**
- Produces: tables/columns only — `rag_ingestions (doc_id, collection, corpus, source_code, embedding_model, embedding_version, chunk_count, ingested_at)` PK `(doc_id, collection)`, FK `doc_id → documents(doc_id)`; `documents.has_text_layer BOOLEAN`, `documents.page_count INTEGER`. The write contract tested here (`INSERT ... ON CONFLICT (doc_id, collection) DO UPDATE`) is what the orchestrator will use in RAG step 3.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/integration/test_rag_ingestions.py`:

```python
import psycopg2
import pytest

from .conftest import fetch_all, make_doc, run_ingest, write_manifest

pytestmark = pytest.mark.integration


UPSERT_RAG_SQL = """
INSERT INTO rag_ingestions (
    doc_id, collection, corpus, source_code,
    embedding_model, embedding_version, chunk_count
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (doc_id, collection) DO UPDATE SET
    corpus = EXCLUDED.corpus,
    source_code = EXCLUDED.source_code,
    embedding_model = EXCLUDED.embedding_model,
    embedding_version = EXCLUDED.embedding_version,
    chunk_count = EXCLUDED.chunk_count,
    ingested_at = now();
"""


def _exec(pg_url, sql, params):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_rag_ingestions_upsert_contract(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    _exec(clean_db, UPSERT_RAG_SQL,
          ("d1", "cb_e5", "central-bank", "us",
           "intfloat/multilingual-e5-base", "v1", 12))
    # Re-ingest updates in place (current-state semantics).
    _exec(clean_db, UPSERT_RAG_SQL,
          ("d1", "cb_e5", "central-bank", "us",
           "intfloat/multilingual-e5-base", "v1", 15))

    rows = fetch_all(
        clean_db,
        "SELECT doc_id, collection, chunk_count FROM rag_ingestions",
    )
    assert rows == [("d1", "cb_e5", 15)]


def test_rag_ingestions_fk_rejects_unknown_doc(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _exec(clean_db, UPSERT_RAG_SQL,
              ("ghost", "cb_e5", "central-bank", "us",
               "intfloat/multilingual-e5-base", "v1", 3))


def test_fact_columns_exist_and_survive_manifest_reupsert(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path)

    _exec(clean_db,
          "UPDATE documents SET has_text_layer = %s, page_count = %s "
          "WHERE doc_id = %s", (True, 42, "d1"))

    # A nightly manifest re-upsert must not null the probed values.
    run_ingest(monkeypatch, clean_db, tmp_path)
    rows = fetch_all(
        clean_db,
        "SELECT has_text_layer, page_count FROM documents WHERE doc_id = 'd1'",
    )
    assert rows == [(True, 42)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -m integration tests/integration/test_rag_ingestions.py -v`
Expected: FAIL — `relation "rag_ingestions" does not exist` / `column "has_text_layer" does not exist`.
(If docker is unavailable the suite skips — then rely on Step 4 as the real check.)

- [ ] **Step 3: Extend the DDL train**

In `ingestion/ingest.py`, append inside the `CREATE_TABLE_SQL` string, after the existing index block (after the `idx_documents_deleted_at` line, before the closing `"""`):

```sql
-- Facts feeding the RAG OCR policy. Filled by the orchestrator's probe pass
-- (its UPDATE is their only writer). Deliberately absent from KNOWN_FIELDS and
-- from the manifest upsert: manifests never carry them, and upserting them
-- would null probed values on every nightly run.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS has_text_layer BOOLEAN;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS page_count INTEGER;

-- RAG ingestion state: current-state row per (doc_id, collection), upserted by
-- the RAG orchestrator over plain SQL (INSERT ... ON CONFLICT DO UPDATE).
-- Cross-model history lives in the collection dimension (one fresh collection
-- per re-embed campaign). The vault owns this DDL; the orchestrator only needs
-- INSERT/UPDATE/SELECT rights.
CREATE TABLE IF NOT EXISTS rag_ingestions (
    doc_id            TEXT NOT NULL REFERENCES documents(doc_id),
    collection        TEXT NOT NULL,
    corpus            TEXT NOT NULL,
    source_code       TEXT,
    embedding_model   TEXT NOT NULL,
    embedding_version TEXT,
    chunk_count       INTEGER NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, collection)
);

CREATE INDEX IF NOT EXISTS idx_rag_ingestions_collection
    ON rag_ingestions(collection);
CREATE INDEX IF NOT EXISTS idx_rag_ingestions_corpus_source
    ON rag_ingestions(corpus, source_code);
```

Note: `REFERENCES documents(doc_id)` targets the existing `UNIQUE NOT NULL` column; `documents` is created earlier in the same SQL string, so ordering is safe on fresh databases.

- [ ] **Step 4: Run integration tests to verify they pass**

Run: `.venv/bin/python -m pytest -m integration tests/integration/test_rag_ingestions.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Run the full unit suite (regression)**

Run: `.venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add ingestion/ingest.py tests/integration/test_rag_ingestions.py
git commit -m "feat: rag_ingestions table and documents fact columns in the DDL train"
```

---

### Task 3: Cadence line parsing (pure function, unit-tested)

**Files:**
- Create: `ingestion/ingest_cadence.py`
- Test: `tests/test_ingest_cadence.py` (create)

**Interfaces:**
- Produces: `ingest_cadence.parse_cadence_line(line: str, source_file: str, line_num: int, run_ts: datetime, default_corpus: str) -> tuple | None` — row tuple ordered `(corpus, source_code, doc_type, last, interval_days, next_expected, days_until, status, expected_per_year, n_3y, updated_at, extra_json_or_None)`; `ingest_cadence.KNOWN_FIELDS: set[str]`.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest_cadence.py`:

```python
import json
from datetime import datetime, timezone

from ingest_cadence import parse_cadence_line

RUN_TS = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
CORPUS = "central-bank"


def make_line(**overrides):
    obj = {
        "bank_code": "us",
        "doc_type": "C1",
        "last": "2026-08-01",
        "interval_days": 14,
        "next_expected": "2026-08-15",
        "days_until": -12,
        "status": "overdue",
        "expected_per_year": 26,
        "n_3y": 78,
    }
    obj.update(overrides)
    return json.dumps(obj)


def test_valid_line_parses_and_maps_bank_code_to_source_code():
    row = parse_cadence_line(make_line(), "cadence.jsonl", 1, RUN_TS, CORPUS)
    assert row == (
        "central-bank", "us", "C1", "2026-08-01", 14, "2026-08-15",
        -12, "overdue", 26, 78, RUN_TS, None,
    )


def test_source_code_field_takes_precedence_over_bank_code():
    row = parse_cadence_line(
        make_line(source_code="ecb"), "cadence.jsonl", 1, RUN_TS, CORPUS
    )
    assert row[1] == "ecb"


def test_unknown_fields_go_to_extra():
    row = parse_cadence_line(
        make_line(muted=True), "cadence.jsonl", 1, RUN_TS, CORPUS
    )
    assert json.loads(row[11]) == {"muted": True}


def test_missing_key_field_skips_line():
    line = make_line()
    obj = json.loads(line)
    del obj["doc_type"]
    assert parse_cadence_line(
        json.dumps(obj), "cadence.jsonl", 1, RUN_TS, CORPUS
    ) is None


def test_corrupt_json_line_skips():
    assert parse_cadence_line(
        "{not json", "cadence.jsonl", 3, RUN_TS, CORPUS
    ) is None


def test_blank_line_skips():
    assert parse_cadence_line("   ", "cadence.jsonl", 4, RUN_TS, CORPUS) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ingest_cadence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest_cadence'`.

- [ ] **Step 3: Implement the module with the parser**

Create `ingestion/ingest_cadence.py`:

```python
#!/usr/bin/env python3
"""
Ingest the cadence report (data/cadence.jsonl) into the `cadence` table.

The producer (central-bank-corpus's cadence watchdog) atomically regenerates
the file as a FULL snapshot: one line per (bank_code, doc_type) series with a
frozen field set. This ingester mirrors that semantic — each run replaces the
table's rows for its corpus in one transaction.

Guards: a missing file is a no-op (normal on deployments without a cadence
producer); an existing file yielding zero valid rows does NOT replace the
table (an implausible mass disappearance is treated as a torn input, not
truth — same philosophy as ingest.py's sweep guard).

The watchdog's private state file (cadence_state.jsonl) never enters the
vault; ingest.py excludes both basenames from the documents manifest scan.
"""

import json
import logging
import os
from datetime import datetime, timezone

from psycopg2.extras import execute_values

log = logging.getLogger("ingest_cadence")

# Producer-contract fields -> everything else goes into `extra` (JSONB).
# bank_code stays listed: the producer emits it (mapped to source_code),
# and it must not fall into extra.
KNOWN_FIELDS = {
    "bank_code", "source_code", "doc_type", "last", "interval_days",
    "next_expected", "days_until", "status", "expected_per_year", "n_3y",
}

CREATE_CADENCE_SQL = """
CREATE TABLE IF NOT EXISTS cadence (
    corpus            TEXT NOT NULL,
    source_code       TEXT NOT NULL,
    doc_type          TEXT NOT NULL,
    last              DATE,
    interval_days     INTEGER,
    next_expected     DATE,
    days_until        INTEGER,
    status            TEXT,
    expected_per_year INTEGER,
    n_3y              INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL,
    extra             JSONB,
    PRIMARY KEY (corpus, source_code, doc_type)
);
"""

INSERT_CADENCE_SQL = """
INSERT INTO cadence (
    corpus, source_code, doc_type, last, interval_days, next_expected,
    days_until, status, expected_per_year, n_3y, updated_at, extra
) VALUES %s
"""


def parse_cadence_line(line, source_file, line_num, run_ts, default_corpus):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        log.warning(f"Skipping invalid line ({source_file}:{line_num}): {e}")
        return None

    source_code = obj.get("source_code") or obj.get("bank_code")
    doc_type = obj.get("doc_type")
    if not source_code or not doc_type:
        log.warning(
            f"Skipping line without source_code/doc_type "
            f"({source_file}:{line_num})"
        )
        return None

    extra = {k: v for k, v in obj.items() if k not in KNOWN_FIELDS}

    return (
        default_corpus,
        source_code,
        doc_type,
        obj.get("last") or None,
        obj.get("interval_days"),
        obj.get("next_expected") or None,
        obj.get("days_until"),
        obj.get("status"),
        obj.get("expected_per_year"),
        obj.get("n_3y"),
        run_ts,
        json.dumps(extra) if extra else None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ingest_cadence.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add ingestion/ingest_cadence.py tests/test_ingest_cadence.py
git commit -m "feat: cadence line parser with producer-contract field mapping"
```

---

### Task 4: Cadence loading + transactional full replace

**Files:**
- Modify: `ingestion/ingest_cadence.py` (append after the parser)
- Test: `tests/test_ingest_cadence.py` (append unit tests), `tests/integration/test_cadence_roundtrip.py` (create)

**Interfaces:**
- Consumes: `parse_cadence_line` (Task 3).
- Produces: `ingest_cadence.load_cadence_rows(path: str, run_ts, default_corpus) -> list[tuple]`; `ingest_cadence.run(conn, data_dir: str, default_corpus: str, run_ts) -> int` (rows written; 0 = skipped, table untouched). `run` honors the `CADENCE_PATH` env override.

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/test_ingest_cadence.py`:

```python
# --- file loading --------------------------------------------------------

def test_load_cadence_rows_reads_all_valid_lines(tmp_path):
    from ingest_cadence import load_cadence_rows

    path = tmp_path / "cadence.jsonl"
    path.write_text(make_line() + "\n" + make_line(bank_code="fr") + "\n")
    rows = load_cadence_rows(str(path), RUN_TS, CORPUS)
    assert [r[1] for r in rows] == ["us", "fr"]


def test_load_cadence_rows_skips_bad_lines_keeps_good(tmp_path):
    from ingest_cadence import load_cadence_rows

    path = tmp_path / "cadence.jsonl"
    path.write_text("{corrupt\n" + make_line() + "\n\n")
    rows = load_cadence_rows(str(path), RUN_TS, CORPUS)
    assert len(rows) == 1
```

- [ ] **Step 2: Run unit tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ingest_cadence.py -v`
Expected: the two new tests FAIL — `ImportError: cannot import name 'load_cadence_rows'`.

- [ ] **Step 3: Write the failing integration tests**

Create `tests/integration/test_cadence_roundtrip.py`:

```python
import json

import psycopg2
import pytest

from .conftest import fetch_all

pytestmark = pytest.mark.integration


def make_entry(**overrides):
    obj = {
        "bank_code": "us",
        "doc_type": "C1",
        "last": "2026-08-01",
        "interval_days": 14,
        "next_expected": "2026-08-15",
        "days_until": -12,
        "status": "overdue",
        "expected_per_year": 26,
        "n_3y": 78,
    }
    obj.update(overrides)
    return obj


def write_cadence(directory, entries):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cadence.jsonl"
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )
    return path


def run_cadence(pg_url, data_dir, corpus="central-bank"):
    from datetime import datetime, timezone

    import ingest_cadence

    conn = psycopg2.connect(pg_url)
    try:
        return ingest_cadence.run(
            conn, str(data_dir), corpus, datetime.now(timezone.utc)
        )
    finally:
        conn.close()


@pytest.fixture()
def clean_cadence(pg_url):
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cadence")
    conn.close()
    return pg_url


def test_snapshot_fully_replaces_previous_rows(clean_cadence, tmp_path):
    write_cadence(tmp_path, [make_entry(), make_entry(bank_code="fr")])
    assert run_cadence(clean_cadence, tmp_path) == 2

    # Next snapshot drops fr and changes us's status.
    write_cadence(tmp_path, [make_entry(status="on-track")])
    assert run_cadence(clean_cadence, tmp_path) == 1

    rows = fetch_all(
        clean_cadence, "SELECT source_code, status FROM cadence"
    )
    assert rows == [("us", "on-track")]


def test_replace_is_scoped_to_the_service_corpus(clean_cadence, tmp_path):
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path, corpus="central-bank")

    other_dir = tmp_path / "company"
    write_cadence(other_dir, [make_entry(bank_code="edgar", doc_type="10-K")])
    run_cadence(clean_cadence, other_dir, corpus="company")

    # Re-running central-bank must not touch the company rows.
    write_cadence(tmp_path, [make_entry(status="soon")])
    run_cadence(clean_cadence, tmp_path, corpus="central-bank")

    rows = sorted(fetch_all(clean_cadence, "SELECT corpus, source_code FROM cadence"))
    assert rows == [("central-bank", "us"), ("company", "edgar")]


def test_missing_file_is_a_noop(clean_cadence, tmp_path):
    assert run_cadence(clean_cadence, tmp_path) == 0


def test_empty_snapshot_leaves_table_untouched(clean_cadence, tmp_path):
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path)

    write_cadence(tmp_path, [])  # torn/empty regeneration
    assert run_cadence(clean_cadence, tmp_path) == 0

    rows = fetch_all(clean_cadence, "SELECT source_code FROM cadence")
    assert rows == [("us",)]


def test_failed_replace_rolls_back_to_previous_snapshot(
    clean_cadence, tmp_path
):
    write_cadence(tmp_path, [make_entry()])
    run_cadence(clean_cadence, tmp_path)

    # A row with a malformed date makes the INSERT raise AFTER the DELETE
    # already ran — the rollback must restore the previous snapshot.
    write_cadence(
        tmp_path,
        [make_entry(bank_code="fr", last="not-a-date")],
    )
    with pytest.raises(psycopg2.DataError):
        run_cadence(clean_cadence, tmp_path)

    rows = fetch_all(clean_cadence, "SELECT source_code FROM cadence")
    assert rows == [("us",)]


def test_cadence_path_env_override(clean_cadence, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    write_cadence(elsewhere, [make_entry()])
    monkeypatch.setenv("CADENCE_PATH", str(elsewhere / "cadence.jsonl"))
    try:
        assert run_cadence(clean_cadence, tmp_path / "empty-dir") == 1
    finally:
        monkeypatch.delenv("CADENCE_PATH")
```

- [ ] **Step 4: Implement loading + replace**

Append to `ingestion/ingest_cadence.py`:

```python
def load_cadence_rows(path, run_ts, default_corpus):
    """Parse every line of a cadence snapshot file into insert-ready rows."""
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            row = parse_cadence_line(line, path, i, run_ts, default_corpus)
            if row:
                rows.append(row)
    return rows


def run(conn, data_dir, default_corpus, run_ts):
    """Replace this corpus's cadence rows with the current snapshot.

    Returns the number of rows written (0 when skipped). The DELETE and the
    INSERTs share one transaction: a failure mid-replace rolls back to the
    previous snapshot.
    """
    path = os.environ.get("CADENCE_PATH") or os.path.join(
        data_dir, "cadence.jsonl"
    )
    if not os.path.exists(path):
        log.info(f"No cadence snapshot at {path} — skipping")
        return 0

    rows = load_cadence_rows(path, run_ts, default_corpus)
    if not rows:
        log.warning(
            f"{path} yielded 0 valid rows — leaving the cadence table "
            "untouched (possible torn/partial regeneration)"
        )
        return 0

    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_CADENCE_SQL)
            cur.execute(
                "DELETE FROM cadence WHERE corpus = %s", (default_corpus,)
            )
            execute_values(cur, INSERT_CADENCE_SQL, rows, page_size=500)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log.info(f"cadence: replaced snapshot with {len(rows)} row(s)")
    return len(rows)
```

- [ ] **Step 5: Run unit then integration tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ingest_cadence.py -v`
Expected: 8 PASS.
Run: `.venv/bin/python -m pytest -m integration tests/integration/test_cadence_roundtrip.py -v`
Expected: 6 PASS.

- [ ] **Step 6: Commit**

```bash
git add ingestion/ingest_cadence.py tests/test_ingest_cadence.py tests/integration/test_cadence_roundtrip.py
git commit -m "feat: cadence snapshot ingestion with transactional full replace"
```

---

### Task 5: Chain cadence into the service run + Dockerfile

**Files:**
- Modify: `ingestion/ingest.py` (imports, end of `main()`'s `try` block)
- Modify: `ingestion/Dockerfile` (COPY line)
- Test: `tests/integration/test_postgres_roundtrip.py` (append)

**Interfaces:**
- Consumes: `ingest_cadence.run(conn, data_dir, default_corpus, run_ts)` (Task 4).
- Produces: `ingest.main()` now also ingests cadence in the same run; the container image ships both modules.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/integration/test_postgres_roundtrip.py`:

```python
def test_service_run_ingests_documents_and_cadence(
    clean_db, tmp_path, monkeypatch
):
    import json

    write_manifest(tmp_path, "us.jsonl", [make_doc("d1")])
    (tmp_path / "cadence.jsonl").write_text(
        json.dumps({
            "bank_code": "us", "doc_type": "C1", "last": "2026-08-01",
            "interval_days": 14, "next_expected": "2026-08-15",
            "days_until": -12, "status": "overdue",
            "expected_per_year": 26, "n_3y": 78,
        }) + "\n"
    )
    # The state file must be ignored by both passes.
    (tmp_path / "cadence_state.jsonl").write_text(
        '{"bank_code": "us", "doc_type": "C1", "overdue": true}\n'
    )

    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cadence")
    conn.close()

    run_ingest(monkeypatch, clean_db, tmp_path)

    docs = fetch_all(clean_db, "SELECT doc_id FROM documents")
    assert docs == [("d1",)]
    cadence = fetch_all(
        clean_db, "SELECT corpus, source_code, doc_type, status FROM cadence"
    )
    assert cadence == [("central-bank", "us", "C1", "overdue")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -m integration tests/integration/test_postgres_roundtrip.py::test_service_run_ingests_documents_and_cadence -v`
Expected: FAIL — `relation "cadence" does not exist` (nothing populated it).

- [ ] **Step 3: Chain cadence in `main()`**

In `ingestion/ingest.py`, add the import after `from psycopg2.extras import execute_values`:

```python
import ingest_cadence
```

Then in `main()`, inside the existing `try:` block, after the sweep section and just before `log.info(f"Done — processed {total_rows} rows in total")`, insert:

```python
        cadence_rows = ingest_cadence.run(conn, data_dir, default_corpus, run_ts)
```

and change the final log line to:

```python
        log.info(
            f"Done — processed {total_rows} document rows and "
            f"{cadence_rows} cadence rows"
        )
```

- [ ] **Step 4: Update the Dockerfile**

In `ingestion/Dockerfile`, replace:

```dockerfile
COPY ingest.py .
```

with:

```dockerfile
COPY ingest.py ingest_cadence.py .
```

- [ ] **Step 5: Run the full suites**

Run: `.venv/bin/python -m pytest -q`
Expected: all unit PASS.
Run: `.venv/bin/python -m pytest -m integration -q`
Expected: all integration PASS (previous + new).

- [ ] **Step 6: Commit**

```bash
git add ingestion/ingest.py ingestion/Dockerfile tests/integration/test_postgres_roundtrip.py
git commit -m "feat: chain cadence ingestion into the service run"
```

---

### Task 6: README documentation of the new tables and write contract

**Files:**
- Modify: `README.md` (schema section — locate the `documents` table description and add a sibling section after it)

**Interfaces:**
- Consumes: final DDL from Tasks 2–4.
- Produces: product documentation (stays on main — not a process artifact).

- [ ] **Step 1: Add the schema documentation**

In `README.md`, after the `documents` table/schema description, add (adapt heading levels to the file's existing structure):

```markdown
### Table `rag_ingestions`

Current-state registry of what the RAG has ingested into which Qdrant
collection: one row per `(doc_id, collection)`, upserted by the RAG
orchestrator over plain SQL. Columns: `doc_id` (FK to `documents`),
`collection`, `corpus`, `source_code`, `embedding_model`,
`embedding_version`, `chunk_count`, `ingested_at`.

Write contract (the orchestrator is the only writer):

    INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code,
        embedding_model, embedding_version, chunk_count)
    VALUES (...)
    ON CONFLICT (doc_id, collection) DO UPDATE SET
        corpus = EXCLUDED.corpus, source_code = EXCLUDED.source_code,
        embedding_model = EXCLUDED.embedding_model,
        embedding_version = EXCLUDED.embedding_version,
        chunk_count = EXCLUDED.chunk_count, ingested_at = now();

"Documents not yet in the RAG" is the anti-join on this table; drift between
the vault and Qdrant becomes a SQL query. Cross-model history lives in the
collection dimension: each re-embed campaign targets a fresh collection.

### Table `cadence`

Publication-cadence report, one row per `(corpus, source_code, doc_type)`
series — the corpus producer's `data/cadence.jsonl` snapshot (a frozen
9-field contract) ingested by the service with full-replace semantics,
scoped to the service's corpus. An empty snapshot never replaces existing
rows (torn-input guard). `cadence_state.jsonl` is the producer's private
state and is excluded from all vault ingestion, as is `cadence.jsonl`
itself from the documents manifest scan.

### Fact columns on `documents`

`has_text_layer` and `page_count` are nullable facts feeding the RAG's OCR
policy. They are written by the orchestrator's probe pass only — manifests
never carry them and the manifest upsert never touches them.
```

- [ ] **Step 2: Verify the README renders sanely**

Run: `grep -n "rag_ingestions\|### Table" README.md | head`
Expected: the new headings appear once each, placed after the `documents` schema description.

- [ ] **Step 3: Final full verification**

Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m pytest -m integration -q`
Expected: both suites fully PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document rag_ingestions, cadence and the documents fact columns"
```
