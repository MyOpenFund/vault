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
ON CONFLICT (doc_id, collection) DO NOTHING;
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
    discovery-errors fixture, and one rag_ingestions row. Every table and view
    the 18 cards read is populated, so an empty result means the SQL is wrong,
    not that the fixture is thin."""
    data = tmp_path_factory.mktemp("metabase-cards")

    write_manifest(data, "us.jsonl", [
        make_doc("us-a1-2019", source="us", doc_type="A1",
                 date="2019-03-01", year=2019,
                 pdf_url="https://example.invalid/us/2019.pdf"),
        make_doc("us-a1-2020", source="us", doc_type="A1",
                 date="2020-05-04", year=2020,
                 pdf_url="https://example.invalid/us/2020.pdf"),
    ])
    # An undated document: C0 counts it, C1/C3 drop it, C4 must keep it.
    write_manifest(data, "ecb.jsonl", [
        make_doc("ecb-e4-undated", source="ecb", doc_type="E4",
                 date=None, year=None, pdf_url=None,
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
                   "docs_path_metadata": 1},
        "sources": [
            {"source_code": "us", "docs_seen": 2, "docs_new": 2, "docs_failed": 0,
             "fetch_errors": 0, "truncated": False, "error_samples": []},
            {"source_code": "ecb", "docs_seen": 1, "docs_new": 1, "docs_failed": 1,
             "fetch_errors": 2, "truncated": True,
             "error_samples": ["ReadTimeout on listing page 3"]},
        ],
    }) + "\n", encoding="utf-8")

    shutil.copy(FIXTURE_ERRORS, data / "discovery_errors.jsonl")

    monkeypatch = pytest.MonkeyPatch()
    try:
        run_ingest(monkeypatch, pg_url, data)
    finally:
        monkeypatch.undo()

    # data-orchestrator's writes, which the vault's ingester never makes.
    _exec(pg_url, UPSERT_RAG_SQL,
          ("us-a1-2019", "cb_corpus_v2", "central-bank", "us",
           "intfloat/multilingual-e5-base", "v1", 42))
    _exec(pg_url, PROBE_DOC_SQL, (True, 17, "us-a1-2019"))
    _exec(pg_url, PROBE_DOC_SQL, (False, 3, "us-a1-2020"))
    return pg_url


@pytest.mark.parametrize("fill_optional", [False, True],
                         ids=["card-defaults", "every-filter-set"])
@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_card_sql_executes(seeded_db, card, fill_optional):
    sql = _render_for_test(card["sql"], card["tags"], fill_optional=fill_optional)
    assert "{{" not in sql and "[[" not in sql, card["code"]
    rows = fetch_all(seeded_db, sql)
    assert isinstance(rows, list)


# The cards below read a table the fixture populates on purpose; an empty
# result from them is a broken predicate, not a thin fixture. (The rest can
# legitimately be empty here — no anomaly, no backlog gap for every series.)
NON_EMPTY = ["C0", "C1", "C2", "C3", "C4", "C5", "C6",
             "Q2", "Q3", "Q4", "Q5", "R1", "R2", "R3", "R4", "R5", "R6"]


@pytest.mark.parametrize("code", NON_EMPTY)
def test_card_returns_rows_on_the_fixture(seeded_db, code):
    card = next(c for c in CARDS if c["code"] == code)
    sql = _render_for_test(card["sql"], card["tags"])
    assert fetch_all(seeded_db, sql), code


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
