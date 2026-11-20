"""The documents contract against a real Postgres (chantier 4, plan V4 + V5).

V4, the DDL train: a database built and fingerprinted by master 2d0457e (frozen
in tests/fixtures/pre_contract_train.sql), roles provisioned, central-bank
rows whose date facts sit in `extra`, two rag_ingestions rows. The first run
of this code re-runs the train once: four new columns, identity (corpus,
doc_id) on documents and in rag_ingestions' keys, the views joining on both,
the `vocabulary` and `ingestion_refusals` tables; no value touched. The next
run skips the train; a forced one is a no-op.

V5, the ingestion: refused rows are not written, are stored in
ingestion_refusals and make the run skip its sweep and exit 1; a vocabulary
file is validated and mirrored into `vocabulary`.
"""

import json
import logging
from pathlib import Path

import psycopg2
import pytest
from vault_contract import COLUMNS, golden

from .conftest import (
    drop_vault_roles, fetch_all, make_doc, role_conn, run_ingest, write_manifest,
)

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures" / "pre_contract_train.sql"
_HEADER_LINES = 4
PRE_CONTRACT_TRAIN = "".join(
    FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)[_HEADER_LINES:])
# sha256 of master 2d0457e's CREATE_TABLE_SQL: what the deployed vault_schema holds.
PRE_CONTRACT_FINGERPRINT = "b5461bbaff0fc866c569abaa0c988d39823473f87265794d01d87dcbdc8ba07e"

ORCH_PW = "contract-orch-pw"
RO_PW = "contract-ro-pw"
CB = "central-bank"


@pytest.fixture(scope="module", autouse=True)
def drop_the_roles_afterwards(pg_url):
    yield
    drop_vault_roles(pg_url)


def _exec(pg_url, sql, params=None):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _rows(pg_url, sql, params=None):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _columns(pg_url, relation):
    return {c for (c,) in fetch_all(
        pg_url,
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = current_schema() AND table_name = '{relation}'")}


CONSTRAINTS_SQL = """
SELECT c.conrelid::regclass::text, c.contype::text,
       ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY AS k(n, i)
               JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n
              ORDER BY k.i) AS cols
FROM pg_constraint c
WHERE c.conrelid IN ('documents'::regclass, 'rag_ingestions'::regclass)
  AND c.contype IN ('p', 'u', 'f')
ORDER BY 1, 2, 3
"""

CONTRACT_KEYS = [
    ("documents", "p", ["id"]),
    ("documents", "u", ["corpus", "doc_id"]),
    ("rag_ingestions", "f", ["corpus", "doc_id"]),
    ("rag_ingestions", "p", ["corpus", "doc_id", "collection"]),
]


def _build_pre_contract_database(pg_url):
    """Schema, fingerprint, roles and data exactly as master 2d0457e left them."""
    import roles
    import schema_gate

    assert schema_gate.fingerprint(PRE_CONTRACT_TRAIN) == PRE_CONTRACT_FINGERPRINT, \
        "the fixture is no longer a verbatim copy of the 2d0457e train"
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(PRE_CONTRACT_TRAIN)
            cur.execute("INSERT INTO vault_schema (singleton, fingerprint) VALUES (TRUE, %s)",
                        (PRE_CONTRACT_FINGERPRINT,))
            roles.ensure_roles(cur, {"vault_orchestrator": ORCH_PW, "vault_readonly": RO_PW})
            cur.execute(
                "INSERT INTO documents (doc_id, corpus, source_code, doc_type, title, date, year, "
                "language, provenance, local_path, last_seen_at, page_count, extra) VALUES "
                "('cb-1', 'central-bank', 'ecb', 'A1', 't1', '2022-07-21', 2022, 'en', 'bank_site', "
                " 'data/raw/ecb/A1/2022/cb-1.pdf', now(), 3, "
                " '{\"date_precision\": \"day\", \"date_kind\": \"meeting\"}'), "
                "('cb-2', 'central-bank', 'jp', 'D1', 't2', '2026-08-01', 2026, 'en', "
                " 'repec_discovery', 'data/raw/jp/D1/2026/cb-2.pdf', now(), NULL, "
                " '{\"date_precision\": \"month\", \"date_source\": \"repec\"}'), "
                "('cb-3', 'central-bank', 'us', 'C1', 't3', NULL, NULL, 'en', 'bis_index', "
                " NULL, now(), NULL, NULL)")
            cur.execute(
                "INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code, "
                "embedding_model, chunk_count) VALUES "
                "('cb-1', 'c1', 'central-bank', 'ecb', 'e5', 3), "
                "('cb-2', 'c1', 'central-bank', 'jp', 'e5', 7)")
        conn.commit()
    finally:
        conn.close()


def _data(pg_url):
    return (
        fetch_all(pg_url, "SELECT id, doc_id, corpus, source_code, doc_type, title, date, year, "
                          "language, provenance, local_path, created_at, updated_at, "
                          "last_seen_at, deleted_at, page_count, extra "
                          "FROM documents ORDER BY doc_id"),
        fetch_all(pg_url, "SELECT doc_id, collection, corpus, source_code, chunk_count, "
                          "ingested_at FROM rag_ingestions ORDER BY doc_id"),
    )


def _set_roles(monkeypatch):
    monkeypatch.setenv("VAULT_ORCHESTRATOR_PASSWORD", ORCH_PW)
    monkeypatch.setenv("VAULT_READONLY_PASSWORD", RO_PW)


# --- V4: the DDL train on a 2d0457e database -------------------------------------


def test_a_2d0457e_database_gets_the_contract_schema_once_with_data_intact(
    clean_db, tmp_path, monkeypatch, caplog
):
    import ingest

    _build_pre_contract_database(clean_db)
    before = _data(clean_db)
    assert ("documents", "u", ["doc_id"]) in [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)]

    _set_roles(monkeypatch)
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)          # empty data dir: no upsert, no sweep
    assert "DDL train running: missing relation(s): vocabulary, ingestion_refusals" in caplog.text
    assert fetch_all(clean_db, "SELECT fingerprint FROM vault_schema") == [
        (ingest.TRAIN_FINGERPRINT,)]

    assert {"date_precision", "date_kind", "entity_scheme", "entity_value"} <= \
        _columns(clean_db, "documents")
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)] == CONTRACT_KEYS
    indexes = {name for (name,) in fetch_all(
        clean_db, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    assert {"idx_documents_entity", "idx_documents_doc_id",
            "idx_ingestion_refusals_corpus_reason",
            "rag_ingestions_doc_collection_transition"} <= indexes
    assert _columns(clean_db, "vocabulary") == {"corpus", "kind", "code", "label", "family"}
    assert _columns(clean_db, "ingestion_refusals") == {
        "corpus", "run_ts", "source_file", "line", "doc_id", "reason", "field", "value"}
    assert _data(clean_db) == before, "the train touched a value"
    # the backlog views join on both keys: cb-3 is the only document in no collection
    assert fetch_all(clean_db, "SELECT doc_id FROM rag_backlog_any") == [("cb-3",)]
    assert fetch_all(clean_db, "SELECT collection, doc_id FROM rag_backlog") == [("c1", "cb-3")]

    # Grants follow: both roles read the new tables; the orchestrator keeps its
    # column UPDATE on documents and its upsert on rag_ingestions (new key).
    for role, pw in (("vault_orchestrator", ORCH_PW), ("vault_readonly", RO_PW)):
        c = role_conn(clean_db, role, pw)
        try:
            with c.cursor() as cur:
                for table in ("vocabulary", "ingestion_refusals"):
                    cur.execute(f"SELECT count(*) FROM {table}")
                    assert cur.fetchone() == (0,)
                if role == "vault_orchestrator":
                    cur.execute("UPDATE documents SET page_count = 4 WHERE doc_id = 'cb-1'")
                    assert cur.rowcount == 1
                    cur.execute(
                        "INSERT INTO rag_ingestions (doc_id, collection, corpus, source_code, "
                        "embedding_model, chunk_count) VALUES ('cb-1', 'c1', 'central-bank', "
                        "'ecb', 'e5', 9) ON CONFLICT (corpus, doc_id, collection) "
                        "DO UPDATE SET chunk_count = EXCLUDED.chunk_count")
            c.rollback()
        finally:
            c.close()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "DDL train skipped" in caplog.text
    assert _data(clean_db) == before

    monkeypatch.setenv("VAULT_DDL_FORCE", "1")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "DDL train running: forced" in caplog.text
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)] == CONTRACT_KEYS
    assert _data(clean_db) == before


def test_central_bank_date_facts_move_from_extra_to_their_columns(
    clean_db, tmp_path, monkeypatch
):
    """The deploy-day ingestion: the same three documents, rows in the
    contract form (date facts beside the other fields)."""
    _build_pre_contract_database(clean_db)
    write_manifest(tmp_path, "m.jsonl", [
        make_doc("cb-1", source="ecb", doc_type="A1", date="2022-07-21", year=2022,
                 provenance="bank_site", date_precision="day", date_kind="meeting"),
        make_doc("cb-2", source="jp", doc_type="D1", date="2026-08-01", year=2026,
                 provenance="repec_discovery", date_precision="month", date_source="repec"),
        make_doc("cb-3", date=None, year=None),
    ])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT doc_id, date_precision, date_kind, local_path, extra "
                               "FROM documents ORDER BY doc_id") == [
        ("cb-1", "day", "meeting", "raw/ecb/C1/2010/cb-1.pdf", None),
        ("cb-2", "month", None, "raw/jp/C1/2010/cb-2.pdf", {"date_source": "repec"}),
        ("cb-3", None, None, "raw/us/C1/2010/cb-3.pdf", None),
    ]


def test_the_golden_legacy_form_row_is_refused_and_blocks_the_sweep(
    clean_db, tmp_path, monkeypatch, caplog
):
    """Transition over (PR 6): a producer still writing the pre-contract form
    (no corpus, bank_code, a data/ path) gets its row refused, and the sweep
    is skipped so the live rows it no longer lists survive."""
    write_vocabulary(tmp_path, golden.vocabulary(CB))
    write_manifest(tmp_path / "manifest", "jp.jsonl",
                   [make_doc("d1", source="jp"), make_doc("gone", source="jp")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert _live(clean_db) == {"d1", "gone"}

    legacy = golden.accepted()["cb_legacy_form"]["row"]
    write_manifest(tmp_path / "manifest", "jp.jsonl", [make_doc("d1", source="jp"), legacy])
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1

    assert _live(clean_db) == {"d1", "gone"}                          # nothing soft-deleted
    assert _rows(clean_db, "SELECT count(*) FROM documents WHERE doc_id = %s",
                 (legacy["doc_id"],)) == [(0,)]
    assert fetch_all(clean_db, "SELECT source_file, line, doc_id, reason, field FROM "
                               "ingestion_refusals ORDER BY reason") == [
        ("manifest/jp.jsonl", 2, legacy["doc_id"], "bad_local_path", "local_path"),
        ("manifest/jp.jsonl", 2, legacy["doc_id"], "missing_corpus", "corpus"),
        ("manifest/jp.jsonl", 2, legacy["doc_id"], "missing_source_code", "source_code"),
    ]
    assert "sweep skipped: 1 refused rows" in caplog.text
    for notice in ("corpus_missing", "legacy_alias"):
        assert notice not in caplog.text


def test_a_rag_row_whose_corpus_is_not_its_documents_stops_the_train(
    clean_db, tmp_path, monkeypatch
):
    """M0.4 broken: the foreign key (corpus, doc_id) cannot be added. The train
    fails as a whole with a message saying what to fix; nothing changes."""
    _build_pre_contract_database(clean_db)
    _exec(clean_db, "UPDATE rag_ingestions SET corpus = 'company' WHERE doc_id = 'cb-2'")
    before = _data(clean_db)
    with pytest.raises(psycopg2.Error) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert "rag_ingestions has 1 row(s)" in str(exc.value)
    assert fetch_all(clean_db, "SELECT fingerprint FROM vault_schema") == [
        (PRE_CONTRACT_FINGERPRINT,)]
    assert ("documents", "u", ["doc_id"]) in [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)]
    assert _data(clean_db) == before


def test_a_fresh_database_has_the_contract_keys(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)] == CONTRACT_KEYS
    monkeypatch.setenv("VAULT_DDL_FORCE", "1")
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)] == CONTRACT_KEYS


def test_the_same_doc_id_under_two_corpora_is_two_rows(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path / "cb", "m.jsonl", [make_doc("x1", title="cb")])
    write_manifest(tmp_path / "co", "m.jsonl", [make_doc("x1", title="co", corpus="company")])
    run_ingest(monkeypatch, clean_db, tmp_path / "cb", corpus=CB)
    run_ingest(monkeypatch, clean_db, tmp_path / "co", corpus="company")
    run_ingest(monkeypatch, clean_db, tmp_path / "cb", corpus=CB)      # re-run: no overwrite
    assert fetch_all(clean_db, "SELECT corpus, doc_id, title, deleted_at FROM documents "
                               "ORDER BY corpus") == [
        ("central-bank", "x1", "cb", None), ("company", "x1", "co", None)]

    # rag_ingestions and the views keep the two apart
    _exec(clean_db, "INSERT INTO rag_ingestions (doc_id, collection, corpus, embedding_model, "
                    "chunk_count) VALUES ('x1', 'c1', 'central-bank', 'e5', 2)")
    assert fetch_all(clean_db, "SELECT corpus, doc_id FROM rag_backlog_any") == [("company", "x1")]
    assert fetch_all(clean_db, "SELECT corpus, doc_id FROM rag_backlog") == [("company", "x1")]


def test_the_foreign_key_refuses_a_rag_row_of_an_absent_corpus_doc_id(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "m.jsonl", [make_doc("x1")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _exec(clean_db, "INSERT INTO rag_ingestions (doc_id, collection, corpus, "
                        "embedding_model, chunk_count) VALUES ('x1', 'c1', 'company', 'e5', 1)")


# --- V5: refusals, sweep, exit code, vocabulary ------------------------------------


def write_vocabulary(directory, doc):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vocabulary.json").write_text(json.dumps(doc), encoding="utf-8")


CB_VOCABULARY = golden.vocabulary("central-bank")    # us, ecb, ..., C1, bis_index, ...


def _live(pg_url, corpus=CB):
    return {d for (d,) in _rows(pg_url, "SELECT doc_id FROM documents WHERE deleted_at IS NULL "
                                        "AND corpus = %s", (corpus,))}


def test_a_9000_character_doc_id_is_refused_not_a_crash(clean_db, tmp_path, monkeypatch, caplog):
    """Before the too_long cap, an incompressible doc_id that long failed the
    upsert ("index row requires ... bytes"), hence the whole run."""
    import hashlib
    huge = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(141))[:9000]
    write_manifest(tmp_path / "manifest", "us.jsonl",
                   [make_doc("d1"), make_doc(huge, title="t", local_path="raw/x.pdf"),
                    make_doc("d2")])
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1
    assert _live(clean_db) == {"d1", "d2"}
    assert fetch_all(clean_db, "SELECT line, reason, field, length(value) FROM "
                               "ingestion_refusals") == [(2, "too_long", "doc_id", 201)]
    assert max(len(r.getMessage()) for r in caplog.records) < 1000     # the log line is capped


def test_one_bad_doc_type_among_ten_is_refused_and_blocks_the_sweep(
    clean_db, tmp_path, monkeypatch, caplog
):
    write_vocabulary(tmp_path, CB_VOCABULARY)
    write_manifest(tmp_path / "manifest", "us.jsonl",
                   [make_doc(f"d{i}") for i in range(9)] + [make_doc("gone")])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert len(_live(clean_db)) == 10

    # "gone" left the manifest and one row has a doc_type the vocabulary lacks.
    rows = [make_doc(f"d{i}") for i in range(9)] + [make_doc("bad", doc_type="10-K")]
    write_manifest(tmp_path / "manifest", "us.jsonl", rows)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1

    assert _live(clean_db) == {f"d{i}" for i in range(9)} | {"gone"}   # nothing soft-deleted
    assert fetch_all(clean_db, "SELECT count(*) FROM documents WHERE doc_id = 'bad'") == [(0,)]
    assert fetch_all(clean_db, "SELECT corpus, source_file, line, doc_id, reason, field, value "
                               "FROM ingestion_refusals") == [
        (CB, "manifest/us.jsonl", 10, "bad", "unknown_doc_type", "doc_type", "'10-K'")]
    assert "manifest/us.jsonl:10 unknown_doc_type doc_type='10-K'" in caplog.text
    assert "sweep skipped: 1 refused rows" in caplog.text
    assert "10 rows read, 9 upserted, 1 refused (unknown_doc_type=1)" in caplog.text
    # The cadence-style passes still ran (a vocabulary was loaded, too).
    assert fetch_all(clean_db, "SELECT count(*) FROM vocabulary WHERE corpus = %s"
                     % "'central-bank'") == [(len(CB_VOCABULARY["source_codes"])
                                              + len(CB_VOCABULARY["doc_types"])
                                              + len(CB_VOCABULARY["provenances"])
                                              + len(CB_VOCABULARY["entity_schemes"]),)]

    # A clean re-run empties this corpus's refusals, sweeps, exits normally.
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc(f"d{i}") for i in range(9)])
    run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT count(*) FROM ingestion_refusals") == [(0,)]
    assert _live(clean_db) == {f"d{i}" for i in range(9)}


def test_a_clean_run_keeps_another_corpus_refusals(clean_db, tmp_path, monkeypatch):
    write_manifest(tmp_path / "co", "m.jsonl", [make_doc("c1", corpus="company", language="EN")])
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path / "co", corpus="company")
    write_manifest(tmp_path / "cb", "m.jsonl", [make_doc("d1")])
    run_ingest(monkeypatch, clean_db, tmp_path / "cb", corpus=CB)
    assert fetch_all(clean_db, "SELECT corpus, reason FROM ingestion_refusals") == [
        ("company", "bad_language")]


def test_every_row_of_one_source_refused_soft_deletes_nothing(clean_db, tmp_path, monkeypatch):
    """The adversarial case of spec §4.3: a vocabulary mistake on a whole
    source. The fraction guard is off (run_ingest sets 1.0): only the
    refusal rule stands between this run and soft-deleting all of ecb."""
    write_vocabulary(tmp_path, CB_VOCABULARY)
    us = [make_doc(f"us{i}") for i in range(5)]
    ecb = [make_doc(f"ecb{i}", source="ecb") for i in range(5)]
    write_manifest(tmp_path / "manifest", "us.jsonl", us)
    write_manifest(tmp_path / "manifest", "ecb.jsonl", ecb)
    run_ingest(monkeypatch, clean_db, tmp_path)

    write_manifest(tmp_path / "manifest", "ecb.jsonl",
                   [make_doc(f"ecb{i}", source="ecb", doc_type="ZZ") for i in range(5)])
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert len(_live(clean_db)) == 10
    assert fetch_all(clean_db, "SELECT count(*) FROM documents WHERE deleted_at IS NOT NULL") == [(0,)]
    assert fetch_all(clean_db, "SELECT count(*) FROM ingestion_refusals") == [(5,)]


def test_the_company_golden_row_fills_all_eighteen_columns(clean_db, tmp_path, monkeypatch):
    case = golden.accepted()["sec_10k"]
    write_vocabulary(tmp_path, golden.vocabulary("company"))
    write_manifest(tmp_path / "manifest", "0000320193.jsonl", [case["row"]])
    run_ingest(monkeypatch, clean_db, tmp_path, corpus="company")
    (row,) = _rows(clean_db, f"SELECT {', '.join(COLUMNS)}, extra FROM documents")
    got = dict(zip(COLUMNS + ("extra",), row))
    assert all(got[name] is not None for name in COLUMNS)
    assert got["entity_scheme"] == "cik" and got["entity_value"] == "0000320193"
    assert str(got["date"]) == "2024-11-01"
    assert got["extra"]["sec_form"] == "10-K"
    assert not set(got["extra"]) & set(COLUMNS)


def test_the_company_golden_row_under_central_bank_is_a_corpus_conflict(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "m.jsonl", [golden.accepted()["sec_10k"]["row"]])
    with pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path, corpus=CB)
    assert exc.value.code == 1
    assert fetch_all(clean_db, "SELECT count(*) FROM documents") == [(0,)]
    assert fetch_all(clean_db, "SELECT reason, field, value FROM ingestion_refusals") == [
        ("corpus_conflict", "corpus", "'company'")]


def test_an_extra_jsonb_cannot_hold_is_refused_not_a_crash(clean_db, tmp_path, monkeypatch):
    # Before: Postgres raised on the whole page ("unsupported Unicode escape
    # sequence") and the run died before its sweep and cadence passes.
    write_manifest(tmp_path, "m.jsonl", [make_doc("ok"), make_doc("nul", note="a\u0000b")])
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT doc_id FROM documents") == [("ok",)]
    assert fetch_all(clean_db, "SELECT doc_id, reason, field FROM ingestion_refusals") == [
        ("nul", "bad_extra", "note")]


def test_an_invalid_vocabulary_fails_the_run_and_writes_nothing(clean_db, tmp_path, monkeypatch):
    run_ingest(monkeypatch, clean_db, tmp_path)                   # schema only
    write_manifest(tmp_path / "manifest", "us.jsonl", [make_doc("d1")])
    write_vocabulary(tmp_path, dict(CB_VOCABULARY, doc_types=[]))
    with pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1
    assert fetch_all(clean_db, "SELECT count(*) FROM documents") == [(0,)]

    write_vocabulary(tmp_path, golden.vocabulary("company"))     # another corpus's
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT count(*) FROM documents") == [(0,)]


def test_the_vocabulary_is_replaced_per_corpus(clean_db, tmp_path, monkeypatch):
    co = golden.vocabulary("company")
    write_vocabulary(tmp_path / "co", co)
    run_ingest(monkeypatch, clean_db, tmp_path / "co", corpus="company")
    write_vocabulary(tmp_path / "cb", CB_VOCABULARY)
    run_ingest(monkeypatch, clean_db, tmp_path / "cb", corpus=CB)

    smaller = dict(CB_VOCABULARY, doc_types=[{"code": "C1", "label": "Speech", "family": "C"}])
    write_vocabulary(tmp_path / "cb", smaller)
    run_ingest(monkeypatch, clean_db, tmp_path / "cb", corpus=CB)
    assert fetch_all(clean_db, "SELECT code, label, family FROM vocabulary "
                               "WHERE corpus = 'central-bank' AND kind = 'doc_type'") == [
        ("C1", "Speech", "C")]
    assert fetch_all(clean_db, "SELECT count(*) FROM vocabulary WHERE corpus = 'company' "
                               "AND kind = 'doc_type'") == [(len(co["doc_types"]),)]

    # A vocabulary file that disappears leaves the table as it was.
    (tmp_path / "cb" / "vocabulary.json").unlink()
    run_ingest(monkeypatch, clean_db, tmp_path / "cb", corpus=CB)
    assert fetch_all(clean_db, "SELECT count(*) FROM vocabulary WHERE corpus = 'central-bank' "
                               "AND kind = 'doc_type'") == [(1,)]


# --- fix wave after review -------------------------------------------------------


def test_a_blank_corpus_is_refused_not_written_under_the_service_corpus(
    clean_db, tmp_path, monkeypatch
):
    write_manifest(tmp_path, "m.jsonl", [make_doc("d0"), make_doc("d1", corpus="")])
    with pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1
    assert fetch_all(clean_db, "SELECT corpus, doc_id FROM documents") == [(CB, "d0")]
    assert fetch_all(clean_db, "SELECT doc_id, reason, field FROM ingestion_refusals") == [
        ("d1", "missing_corpus", "corpus")]


@pytest.mark.parametrize("key", ["k\u0000", "\ud800"])
def test_a_bad_extra_key_is_stored_as_its_repr_not_a_crash(clean_db, tmp_path, monkeypatch, key):
    write_manifest(tmp_path, "m.jsonl", [make_doc("ok"), make_doc("bad", **{key: 1})])
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT doc_id FROM documents") == [("ok",)]
    assert fetch_all(clean_db, "SELECT doc_id, reason, field FROM ingestion_refusals") == [
        ("bad", "bad_extra", repr(key))]


def test_one_torn_line_among_ten_soft_deletes_nothing(clean_db, tmp_path, monkeypatch):
    docs = [make_doc(f"d{i}") for i in range(10)]
    write_manifest(tmp_path / "manifest", "us.jsonl", docs)
    run_ingest(monkeypatch, clean_db, tmp_path)

    lines = [json.dumps(d) for d in docs[:9]] + [json.dumps(docs[9])[:25]]   # torn write
    (tmp_path / "manifest" / "us.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert exc.value.code == 1
    assert len(_live(clean_db)) == 10
    assert fetch_all(clean_db, "SELECT line, doc_id, reason, field, value FROM ingestion_refusals") == [
        (10, None, "not_json", None, json.dumps(docs[9])[:25])]


def test_a_bom_on_the_first_line_is_refused_and_soft_deletes_nothing(
    clean_db, tmp_path, monkeypatch
):
    docs = [make_doc(f"d{i}") for i in range(3)]
    write_manifest(tmp_path / "manifest", "us.jsonl", docs)
    run_ingest(monkeypatch, clean_db, tmp_path)
    path = tmp_path / "manifest" / "us.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert len(_live(clean_db)) == 3
    assert fetch_all(clean_db, "SELECT line, reason FROM ingestion_refusals") == [(1, "not_json")]


# The deployed data-orchestrator's statement (master 81ead10, data_orchestrator/vault.py),
# kept working by the transitional unique index until its own update.
OLD_ORCHESTRATOR_UPSERT = """
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


def test_the_deployed_orchestrator_upsert_still_works_after_the_train(
    clean_db, tmp_path, monkeypatch
):
    _build_pre_contract_database(clean_db)
    run_ingest(monkeypatch, clean_db, tmp_path)
    for chunks in (11, 12):            # insert branch, then the DO UPDATE branch
        _exec(clean_db, OLD_ORCHESTRATOR_UPSERT,
              ("cb-3", "c1", CB, "us", "e5", "v1", chunks))
    _exec(clean_db, OLD_ORCHESTRATOR_UPSERT, ("cb-1", "c1", CB, "ecb", "e5", "v1", 5))
    assert fetch_all(clean_db, "SELECT doc_id, chunk_count FROM rag_ingestions "
                               "ORDER BY doc_id") == [("cb-1", 5), ("cb-2", 7), ("cb-3", 12)]


def _readme_rollback_sql():
    readme = (Path(__file__).parent.parent.parent / "README.md").read_text(encoding="utf-8")
    start = readme.index("-- rollback: documents contract -> 2d0457e")
    return readme[start:readme.index("```", start)]


def test_the_readme_rollback_returns_to_the_2d0457e_keys(clean_db, tmp_path, monkeypatch):
    from psycopg2.extras import execute_values

    _build_pre_contract_database(clean_db)
    run_ingest(monkeypatch, clean_db, tmp_path)                    # contract schema
    _exec(clean_db, _readme_rollback_sql())
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)] == [
        ("documents", "p", ["id"]),
        ("documents", "u", ["doc_id"]),
        ("rag_ingestions", "f", ["doc_id"]),
        ("rag_ingestions", "p", ["doc_id", "collection"]),
    ]
    # No fingerprint: the 2d0457e code re-runs its own train once (views back
    # on doc_id). That train succeeds on the rolled-back schema, twice.
    assert fetch_all(clean_db, "SELECT count(*) FROM vault_schema") == [(0,)]
    _exec(clean_db, PRE_CONTRACT_TRAIN)
    _exec(clean_db, PRE_CONTRACT_TRAIN)
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)][1] == ("documents", "u", ["doc_id"])
    # The 2d0457e upsert and the deployed orchestrator's run again.
    conn = psycopg2.connect(clean_db)
    try:
        with conn.cursor() as cur:
            execute_values(cur, PRE_CONTRACT_UPSERT, [(
                "cb-1", CB, "ecb", "A1", "t1 again", None, None, "2022-07-21", 2022, "en",
                "bank_site", None, None, "raw/x.pdf", None, None, None)])
        conn.commit()
    finally:
        conn.close()
    assert fetch_all(clean_db, "SELECT title FROM documents WHERE doc_id = 'cb-1'") == [
        ("t1 again",)]
    _exec(clean_db, OLD_ORCHESTRATOR_UPSERT, ("cb-3", "c1", CB, "us", "e5", "v1", 2))



# The updated data-orchestrator's statement (PR 4): the contract's key.
NEW_ORCHESTRATOR_UPSERT = OLD_ORCHESTRATOR_UPSERT.replace(
    "ON CONFLICT (doc_id, collection)", "ON CONFLICT (corpus, doc_id, collection)")


def test_the_readme_rollback_after_an_ingestion_under_the_new_code(
    clean_db, tmp_path, monkeypatch
):
    """The realistic case: the new code has ingested (date facts moved from
    `extra` to their columns, a refusal stored, a new document) and the
    updated orchestrator has written under the new key before the rollback."""
    from psycopg2.extras import execute_values

    _build_pre_contract_database(clean_db)
    write_manifest(tmp_path / "manifest", "cb.jsonl", [
        dict(contract_cb_row("cb-1", "ecb", "A1"), date="2022-07-21", year=2022,
             date_precision="day", date_kind="meeting"),
        contract_cb_row("cb-3", "us", "C1"),
        contract_cb_row("cb-4", "us", "C1"),                 # new under the new code
        dict(contract_cb_row("cb-5", "us", "C1"), language="english"),   # refused
    ])
    with pytest.raises(SystemExit):
        run_ingest(monkeypatch, clean_db, tmp_path)
    assert fetch_all(clean_db, "SELECT doc_id, date_precision, extra FROM documents "
                               "WHERE doc_id IN ('cb-1', 'cb-2') ORDER BY doc_id") == [
        ("cb-1", "day", None),                               # left `extra` for its column
        ("cb-2", None, {"date_precision": "month", "date_source": "repec"}),  # not in a manifest
    ]
    for chunks in (4, 5):              # insert branch, then the DO UPDATE branch
        _exec(clean_db, NEW_ORCHESTRATOR_UPSERT, ("cb-4", "c1", CB, "us", "e5", "v1", chunks))
    assert fetch_all(clean_db, "SELECT count(*) FROM ingestion_refusals") == [(1,)]

    rollback = _readme_rollback_sql()
    # The wait for the table locks is bounded before any other statement.
    assert rollback.splitlines()[1] == "SET LOCAL lock_timeout = '5s';"
    _exec(clean_db, rollback)
    assert [tuple(r) for r in fetch_all(clean_db, CONSTRAINTS_SQL)] == [
        ("documents", "p", ["id"]),
        ("documents", "u", ["doc_id"]),
        ("rag_ingestions", "f", ["doc_id"]),
        ("rag_ingestions", "p", ["doc_id", "collection"]),
    ]
    # An orchestrator already updated to the contract's key fails on the
    # rolled-back schema (README precondition: roll it back too).
    with pytest.raises(psycopg2.errors.InvalidColumnReference):
        _exec(clean_db, NEW_ORCHESTRATOR_UPSERT, ("cb-4", "c1", CB, "us", "e5", "v1", 9))
    _exec(clean_db, PRE_CONTRACT_TRAIN)
    assert fetch_all(clean_db, "SELECT doc_id, chunk_count FROM rag_ingestions "
                               "ORDER BY doc_id") == [("cb-1", 3), ("cb-2", 7), ("cb-4", 5)]
    # Left behind, ignored by the old code: the columns keep their values,
    # the new tables their rows.
    assert fetch_all(clean_db, "SELECT count(*) FROM ingestion_refusals") == [(1,)]
    # cb-1's date facts are back in `extra` only once the 2d0457e ingestion
    # writes the row again (its extra carried them).
    conn = psycopg2.connect(clean_db)
    try:
        with conn.cursor() as cur:
            execute_values(cur, PRE_CONTRACT_UPSERT, [(
                "cb-1", CB, "ecb", "A1", "t1 again", None, None, "2022-07-21", 2022, "en",
                "bank_site", None, None, "raw/x.pdf", None, None,
                json.dumps({"date_precision": "day", "date_kind": "meeting"}))])
        conn.commit()
    finally:
        conn.close()
    assert fetch_all(clean_db, "SELECT extra FROM documents WHERE doc_id = 'cb-1'") == [
        ({"date_precision": "day", "date_kind": "meeting"},)]
    _exec(clean_db, OLD_ORCHESTRATOR_UPSERT, ("cb-4", "c1", CB, "us", "e5", "v1", 6))
    assert fetch_all(clean_db, "SELECT chunk_count FROM rag_ingestions "
                               "WHERE doc_id = 'cb-4'") == [(6,)]


def contract_cb_row(doc_id, source, doc_type):
    """A contract row of the central-bank corpus (columns, not `extra`)."""
    return {"doc_id": doc_id, "corpus": CB, "source_code": source, "doc_type": doc_type,
            "title": f"t {doc_id}", "language": "en", "provenance": "bis_index"}

# master 2d0457e's ingest.UPSERT_SQL, verbatim.
PRE_CONTRACT_UPSERT = """
INSERT INTO documents (
    doc_id, corpus, source_code, doc_type, title, document_url, source_url,
    date, year, language, provenance, mime_type, sha256, local_path,
    updated_at, last_seen_at, extra
) VALUES %s
ON CONFLICT (doc_id) DO UPDATE SET
    corpus = EXCLUDED.corpus,
    source_code = EXCLUDED.source_code,
    doc_type = EXCLUDED.doc_type,
    title = EXCLUDED.title,
    document_url = EXCLUDED.document_url,
    source_url = EXCLUDED.source_url,
    date = EXCLUDED.date,
    year = EXCLUDED.year,
    language = EXCLUDED.language,
    provenance = EXCLUDED.provenance,
    mime_type = EXCLUDED.mime_type,
    sha256 = EXCLUDED.sha256,
    local_path = EXCLUDED.local_path,
    extra = EXCLUDED.extra,
    updated_at = EXCLUDED.updated_at,
    -- never rewind: a slower run carrying an older run timestamp must not make a live row look stale to a newer run's sweep (vault #3)
    last_seen_at = GREATEST(documents.last_seen_at, EXCLUDED.last_seen_at),
    deleted_at = NULL;
"""
