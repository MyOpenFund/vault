"""M5 guard: every Metabase card's SQL still runs against the real schema.

The cards live in `metabase/cards.json` with Metabase's `{{tag}}` / `[[ ]]`
template syntax, which Postgres cannot parse. `_render_for_test` does what
Metabase does to a native query before sending it — drop an optional block
whose variables have no value, substitute the rest — so what reaches the
database here is the statement the card actually issues.

Each card is executed twice: once with the card defaults only (every optional
`[[ ]]` block elided) and once with every filter supplied (every block kept).
Those are the two branches Metabase compiles, and a card can be valid in one
and broken in the other — the catalogue's own validation ran both ways.
"""
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

from .conftest import fetch_all, make_doc, make_entry, run_ingest, write_cadence, write_manifest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent.parent
CARDS = json.loads((REPO / "metabase" / "cards.json").read_text())
FIXTURE_ERRORS = REPO / "tests" / "fixtures" / "discovery_errors_real.jsonl"

TAG_RE = re.compile(r"\{\{(\w+)\}\}")
OPTIONAL_RE = re.compile(r"\[\[(.*?)\]\]", re.S)

# Values for the optional filters, chosen to match the fixture tree below so
# the "all filters supplied" branch selects rows instead of trivially nothing.
PROBES = {
    "bank": "'us'",
    "doc_type": "'A1'",
    "status": "'overdue'",
    "as_of": "'2026-12-31'::date",
    # C4's rag_state is optional (unset = no restriction); the filled branch has
    # to bind one of the three words the predicate knows, not a stray string.
    "rag_state": "'any'",
}


def _literal(spec):
    """A default rendered as the SQL literal Metabase binds for it."""
    value = spec["default"]
    if value is None:
        return None
    if spec["type"] == "number":
        return str(value)
    if spec["type"] == "date":
        return "'{}'::date".format(value)
    return "'{}'".format(str(value).replace("'", "''"))


def _probe(name, spec):
    if name in PROBES:
        return PROBES[name]
    return {"number": "1", "date": "'2026-12-31'::date"}.get(spec["type"], "'probe'")


def _render_for_test(sql, tags, fill_optional=False):
    values = {}
    for name, spec in tags.items():
        literal = _literal(spec)
        if literal is None and fill_optional:
            literal = _probe(name, spec)
        values[name] = literal

    def render_block(match):
        inner = match.group(1)
        if any(values.get(tag) is None for tag in TAG_RE.findall(inner)):
            return ""
        return inner

    rendered = OPTIONAL_RE.sub(render_block, sql)

    def render_tag(match):
        name = match.group(1)
        literal = values.get(name)
        if literal is None:
            raise AssertionError(
                "{} is substituted outside an optional block but has no value".format(name))
        return literal

    return TAG_RE.sub(render_tag, rendered)


UPSERT_RAG_SQL = """
INSERT INTO rag_ingestions (
    doc_id, collection, corpus, source_code,
    embedding_model, embedding_version, chunk_count
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (corpus, doc_id, collection) DO NOTHING;
"""

COMPANY_DOC_SQL = """
INSERT INTO documents (doc_id, corpus, source_code, doc_type, title, date, year,
                       date_precision, date_kind, entity_scheme, entity_value, last_seen_at)
VALUES (%s, 'company', 'sec', '10-K', 'Annual report', '2020-02-01', 2020,
        'day', 'publication', 'cik', '0000320193', now());
"""

PROBE_DOC_SQL = """
UPDATE documents SET has_text_layer = %s, page_count = %s WHERE doc_id = %s;
"""


def _exec(pg_url, sql, params):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded_db(pg_url, tmp_path_factory):
    """One small but complete corpus: documents over two sources (one of them
    undated), a cadence row, a run report carrying a truncated source, the real
    discovery-errors fixture, two refused manifest lines, and one
    rag_ingestions row. A `company` document shares a central-bank doc_id and
    is in RAG, so a card joining on doc_id alone would show the central-bank one
    as ingested. Every table and view the 20 cards read is populated, so an
    empty result means the SQL is wrong, not that the fixture is thin."""
    data = tmp_path_factory.mktemp("metabase-cards")

    write_manifest(data, "us.jsonl", [
        make_doc("us-a1-2019", source="us", doc_type="A1",
                 date="2019-03-01", year=2019, date_precision="day", date_kind="publication",
                 document_url="https://example.invalid/us/2019.pdf"),
        make_doc("us-a1-2020", source="us", doc_type="A1",
                 date="2020-05-04", year=2020,
                 document_url="https://example.invalid/us/2020.pdf"),
        # Refused (Q7): a precision outside the contract's values.
        make_doc("us-a1-bad", source="us", doc_type="A1", date_precision="Day"),
    ])
    with (data / "us.jsonl").open("a", encoding="utf-8") as f:
        f.write("{torn\n")                     # refused (Q7): not_json
    # An undated document: C0 counts it, C1/C3 drop it, C4 must keep it.
    write_manifest(data, "ecb.jsonl", [
        make_doc("ecb-e4-undated", source="ecb", doc_type="E4",
                 date=None, year=None, document_url=None,
                 source_url="https://example.invalid/ecb/report"),
    ])
    write_cadence(data, [make_entry(bank_code="us", doc_type="A1")])

    finished = datetime.now(timezone.utc) - timedelta(days=1)
    started = finished - timedelta(minutes=12)
    (data / "runs.jsonl").write_text(json.dumps({
        "run_id": "metabase-card-guard-1",
        "tool": "central-bank-corpus",
        "command": "discover",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "outcome": "degraded",
        "exit_code": 3,
        "totals": {"docs_seen": 3, "docs_new": 3, "docs_failed": 1,
                   "docs_path_metadata": 1, "bad_content": 1},
        "sources": [
            {"source_code": "us", "docs_seen": 2, "docs_new": 2, "docs_failed": 0,
             "fetch_errors": 0, "truncated": False, "error_samples": []},
            {"source_code": "ecb", "docs_seen": 1, "docs_new": 1, "docs_failed": 1,
             "fetch_errors": 2, "truncated": True, "bad_content": 1,
             "error_samples": ["ReadTimeout on listing page 3"]},
        ],
    }) + "\n", encoding="utf-8")

    shutil.copy(FIXTURE_ERRORS, data / "discovery_errors.jsonl")

    monkeypatch = pytest.MonkeyPatch()
    try:
        with pytest.raises(SystemExit) as refused:      # the two refused lines
            run_ingest(monkeypatch, pg_url, data)
        assert refused.value.code == 1
    finally:
        monkeypatch.undo()

    # Another corpus's document under a central-bank doc_id, in RAG (company
    # rows are written by their own ingestion service; inserted directly here).
    _exec(pg_url, COMPANY_DOC_SQL, ("us-a1-2020",))
    _exec(pg_url, UPSERT_RAG_SQL,
          ("us-a1-2020", "cb_corpus_v2", "company", "sec",
           "intfloat/multilingual-e5-base", "v1", 7))

    # data-orchestrator's writes, which the vault's ingester never makes.
    _exec(pg_url, UPSERT_RAG_SQL,
          ("us-a1-2019", "cb_corpus_v2", "central-bank", "us",
           "intfloat/multilingual-e5-base", "v1", 42))
    _exec(pg_url, PROBE_DOC_SQL, (True, 17, "us-a1-2019"))
    _exec(pg_url, PROBE_DOC_SQL, (False, 3, "us-a1-2020"))

    # Malformed run reports, older than the good one so neither is the latest of
    # its tool, inside the R5/R6 lookback. The ingester checks only container
    # types and the orchestrator role can INSERT into runs directly, so such
    # rows can exist; one of them must not make R3, R5 or R6 raise.
    for run_id, days, totals, sources in MALFORMED_RUNS:
        when = datetime.now(timezone.utc) - timedelta(days=days)
        _exec(pg_url, "INSERT INTO runs (run_id, tool, command, corpus, started_at, finished_at, "
                      "outcome, exit_code, totals, sources) VALUES (%s, 'central-bank-corpus', "
                      "'discover', 'central-bank', %s, %s, 'ok', 0, %s, %s)",
              (run_id, when, when, json.dumps(totals), json.dumps(sources)))
    return pg_url


HUGE = 10000000000          # past the int range: a bare ::int raises
MALFORMED_COUNTERS = {"docs_seen": HUGE, "docs_ingested": 1.5, "docs_new": "yes please",
                      "docs_failed": -5, "docs_path_metadata": HUGE, "bad_content": 1.5}
MALFORMED_RUNS = [  # (run_id, days ago, totals, sources)
    # `sources` an object, not an array: jsonb_array_length / _elements raise.
    ("metabase-card-malformed-object", 10, MALFORMED_COUNTERS,
     {"source_code": "us", "truncated": "yes please"}),
    # An array whose entries carry huge, float and string counters and a
    # string `truncated`; the truncated one has a float counter, so R6 lists it.
    ("metabase-card-malformed-array", 9, MALFORMED_COUNTERS, [
        {"source_code": "us", "docs_seen": HUGE, "docs_new": 1.5, "docs_failed": "yes please",
         "fetch_errors": HUGE, "truncated": "yes please", "error_samples": []},
        {"source_code": "ecb", "docs_seen": 1.5, "docs_new": HUGE, "docs_failed": "yes please",
         "fetch_errors": -1, "truncated": True, "error_samples": "not a list"},
    ]),
]


@pytest.mark.parametrize("fill_optional", [False, True],
                         ids=["card-defaults", "every-filter-set"])
@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_card_sql_executes(seeded_db, card, fill_optional):
    sql = _render_for_test(card["sql"], card["tags"], fill_optional=fill_optional)
    assert "{{" not in sql and "[[" not in sql, card["code"]
    rows = fetch_all(seeded_db, sql)
    assert isinstance(rows, list)


# Every card but Q1 reads a table the fixture populates on purpose; an empty
# result from one of them is a broken predicate, not a thin fixture. Q1 is the
# only exclusion: it reports *publication anomalies*, and a fixture with none is
# a healthy corpus rather than a broken card.
NON_EMPTY = ["C0", "C1", "C2", "C3", "C4", "C5", "C6",
             "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "R1", "R2", "R3", "R4", "R5", "R6"]


@pytest.mark.parametrize("code", NON_EMPTY)
def test_card_returns_rows_on_the_fixture(seeded_db, code):
    card = next(c for c in CARDS if c["code"] == code)
    sql = _render_for_test(card["sql"], card["tags"])
    assert fetch_all(seeded_db, sql), code


def _card_dicts(seeded_db, code, fill_optional=False, corpus=None):
    card = next(c for c in CARDS if c["code"] == code)
    tags = json.loads(json.dumps(card["tags"]))
    if corpus is not None:
        tags["corpus"]["default"] = corpus
    conn = psycopg2.connect(seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute(_render_for_test(card["sql"], tags, fill_optional=fill_optional))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def test_q6_lists_only_banks_with_bad_content(seeded_db):
    # ecb reported 1 bad-content document; us reported none (no key): only ecb is listed.
    # The malformed run's ecb entry counts as a run but reports no bad_content.
    rows = _card_dicts(seeded_db, "Q6")
    assert [(r["bank"], r["bad_content"], r["docs_failed"], r["runs"], r["runs_reporting"])
            for r in rows] == [("ecb", 1, 1, 2, 1)]
    assert rows[0]["last_bad_content_run_id"] == "metabase-card-guard-1"
    assert _card_dicts(seeded_db, "Q6", fill_optional=True) == []  # bank = 'us'


def test_q6_counts_reporting_runs_windows_and_picks_the_newest_bad_run(seeded_db):
    # Its own corpus (and tool, outcome ok, nothing truncated), so the shared
    # fixture's other cards are unaffected; removed again in `finally`.
    now = datetime.now(timezone.utc)
    runs = [  # (run_id, days ago, sources)
        ("q6-new", 1, [{"source_code": "fr", "docs_failed": 4, "bad_content": 3},
                       {"source_code": "it", "docs_failed": 1, "bad_content": 0}]),
        ("q6-nokey", 2, [{"source_code": "fr", "docs_failed": 9}]),
        ("q6-mid", 5, [{"source_code": "fr", "docs_failed": 2, "bad_content": 2}]),
        ("q6-old", 120, [{"source_code": "fr", "docs_failed": 50, "bad_content": 50}]),
    ]
    for run_id, days, sources in runs:
        finished = now - timedelta(days=days)
        _exec(seeded_db, "INSERT INTO runs (run_id, tool, command, corpus, started_at, finished_at, "
                         "outcome, exit_code, sources) VALUES (%s, 'q6-probe', 'x', 'q6-probe', %s, %s, "
                         "'ok', 0, %s)", (run_id, finished, finished, json.dumps(sources)))
    try:
        card = next(c for c in CARDS if c["code"] == "Q6")
        tags = json.loads(json.dumps(card["tags"]))
        tags["corpus"]["default"] = "q6-probe"
        sql = _render_for_test(card["sql"], tags)
        conn = psycopg2.connect(seeded_db)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                names = [d[0] for d in cur.description]
                rows = [dict(zip(names, r)) for r in cur.fetchall()]
        finally:
            conn.close()
        # it reported 0 only: not listed. fr: 3 in-window runs, 2 reporting; the
        # 120-day-old run is outside the 90-day default; docs_failed over reporting runs.
        assert [(r["bank"], r["runs"], r["runs_reporting"], r["docs_failed"], r["bad_content"])
                for r in rows] == [("fr", 3, 2, 6, 5)]
        assert rows[0]["last_bad_content_run_id"] == "q6-new"
    finally:
        _exec(seeded_db, "DELETE FROM runs WHERE corpus = 'q6-probe'", None)


def test_c4_joins_rag_on_corpus_and_shows_the_date_and_entity_facts(seeded_db):
    rows = {r["doc_id"]: r for r in _card_dicts(seeded_db, "C4")}
    assert set(rows) == {"us-a1-2019", "us-a1-2020", "ecb-e4-undated"}
    # The company document of the same doc_id is in RAG; this one is not.
    assert rows["us-a1-2020"]["in_rag"] is False and rows["us-a1-2020"]["n_chunks"] is None
    assert rows["us-a1-2019"]["in_rag"] is True and rows["us-a1-2019"]["n_chunks"] == 42
    assert (rows["us-a1-2019"]["date_precision"], rows["us-a1-2019"]["date_kind"]) == (
        "day", "publication")
    assert rows["us-a1-2020"]["date_precision"] is None
    (company,) = _card_dicts(seeded_db, "C4", corpus="company")
    assert {k: company[k] for k in ("doc_id", "bank", "type", "date_precision", "date_kind",
                                    "entity_value", "in_rag", "n_chunks")} == {
        "doc_id": "us-a1-2020", "bank": "sec", "type": "10-K", "date_precision": "day",
        "date_kind": "publication", "entity_value": "0000320193", "in_rag": True,
        "n_chunks": 7}


def test_r2_counts_only_its_corpus_rag_rows(seeded_db):
    rows = {(r["bank"], r["type"]): r for r in _card_dicts(seeded_db, "R2")}
    assert (rows[("us", "A1")]["documents"], rows[("us", "A1")]["in_rag"],
            rows[("us", "A1")]["chunks"]) == (2, 1, 42)


def test_q7_counts_refused_rows_by_corpus_and_reason(seeded_db):
    rows = _card_dicts(seeded_db, "Q7")
    assert [(r["corpus"], r["reason"], r["refusals"], r["refused_lines"], r["files"])
            for r in rows] == [("central-bank", "bad_date_precision", 1, 1, 1),
                               ("central-bank", "not_json", 1, 1, 1)]
    (run_ts,) = {r["last_run_ts"] for r in rows}
    assert run_ts is not None
    assert rows[0]["sample"] == "us.jsonl:3 date_precision='Day'"
    assert rows[1]["sample"] == "us.jsonl:4={torn"


def test_r3_shows_the_run_bad_content_total(seeded_db):
    (row,) = [r for r in _card_dicts(seeded_db, "R3") if r["tool"] == "central-bank-corpus"]
    assert row["bad_content"] == 1


def test_r5_reads_malformed_counters_as_null(seeded_db):
    days = {(datetime.now(timezone.utc) - timedelta(days=d)).date() for _, d, _, _ in MALFORMED_RUNS}
    rows = [r for r in _card_dicts(seeded_db, "R5") if r["day"] in days]
    assert len(rows) == len(days)
    assert {(r["runs"], r["docs_path_metadata"], r["docs_ingested"], r["pct_path_metadata"])
            for r in rows} == {(1, None, None, None)}



def test_renderer_drops_blocks_without_values_and_keeps_the_rest():
    tags = {"corpus": {"type": "text", "required": True, "default": "central-bank",
                       "display_name": "Corpus"},
            "bank": {"type": "text", "required": False, "default": None,
                     "display_name": "Bank"},
            "n": {"type": "number", "required": True, "default": 7,
                  "display_name": "N"},
            "d": {"type": "date", "required": True, "default": "2026-09-05",
                  "display_name": "D"}}
    sql = "WHERE c = {{corpus}} [[AND b = {{bank}}]] AND n = {{n}} AND d = {{d}}"
    assert _render_for_test(sql, tags) == (
        "WHERE c = 'central-bank'  AND n = 7 AND d = '2026-09-05'::date")
    assert _render_for_test(sql, tags, fill_optional=True) == (
        "WHERE c = 'central-bank' AND b = 'us' AND n = 7 AND d = '2026-09-05'::date")
