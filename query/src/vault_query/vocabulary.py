"""The corpus vocabularies (documents contract) and the doc_type-needs-corpus message."""
from psycopg2.extras import RealDictCursor

from .filters import DocTypeNeedsCorpus, InvalidQuery, check_int, check_text

# The `vocabulary.kind` values: the column each declared code is for.
VOCABULARY_KINDS = frozenset({"source_code", "doc_type", "provenance", "entity_scheme"})

# Upper bound of the code listing in the doc_type-needs-corpus message, every
# corpus together: a vocabulary holds tens of doc_types, so this only stops a
# pathological table from building a megabyte error message.
MAX_MESSAGE_CODES = 500

# Each label in that message is cut to this many characters (then "…"): a
# label is producer text of any length, so without it the message would be
# bounded in codes but not in bytes. 80 keeps every real label whole (the
# longest taxonomy labels are under 60) and the message under ~170 kB even at
# MAX_MESSAGE_CODES codes of four-byte characters.
MAX_LABEL_CHARS = 80

# The doc_type codes of every corpus: its declared vocabulary, or, for a corpus
# absent from `vocabulary` (none loaded yet), the codes its live
# documents use. A corpus with a vocabulary is described by it alone.
_CODES_SQL = """
SELECT corpus, code, label, TRUE AS declared
  FROM vocabulary WHERE kind = 'doc_type'
UNION ALL
SELECT DISTINCT d.corpus, d.doc_type, NULL, FALSE
  FROM documents d
 WHERE d.deleted_at IS NULL AND d.doc_type IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM vocabulary v WHERE v.corpus = d.corpus)
ORDER BY 1, 2
LIMIT %s
"""


def list_vocabulary(conn, corpus=None, *, kind=None, limit=1000):
    """The declared codes, as rows {corpus, kind, code, label, family}.

    Ordered by corpus, kind, code; at most `limit` rows. `kind` is one of
    VOCABULARY_KINDS (None: every kind). A corpus whose producer has not
    shipped a vocabulary.json yet has no rows.
    """
    check_text("corpus", corpus)
    if kind is not None and kind not in VOCABULARY_KINDS:
        raise InvalidQuery(f"kind must be one of {sorted(VOCABULARY_KINDS)}")
    check_int("limit", limit, 1)
    clauses, params = [], []
    if corpus is not None:
        clauses.append("corpus = %s")
        params.append(corpus)
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT corpus, kind, code, label, family FROM vocabulary{where} "
            f"ORDER BY corpus, kind, code LIMIT %s",
            params + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


def doc_type_needs_corpus(conn):
    """The DocTypeNeedsCorpus to raise, its message listing each corpus's codes.

    One bounded read on the caller's connection. Format:
    `doc_type needs corpus: <why>. Codes per corpus: central-bank: A1 = <label>,
    …; company: A1 = <label>, …`; a corpus without a vocabulary shows the codes
    in use, marked as such.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_CODES_SQL, (MAX_MESSAGE_CODES + 1,))
        rows = cur.fetchall()
    return DocTypeNeedsCorpus(_message(rows))


def _message(rows):
    head = ("doc_type needs corpus: a doc_type code means something only within its corpus "
            "(the same code can name different document types in two corpora); pass corpus "
            "with doc_type, asking the user when the corpus is not clear from context")
    if not rows:
        return head + "; no corpus declares or uses a doc_type yet."
    truncated = len(rows) > MAX_MESSAGE_CODES
    groups = {}
    for r in rows[:MAX_MESSAGE_CODES]:
        name = r["corpus"] if r["declared"] else \
            f"{r['corpus']} (no vocabulary loaded, codes in use)"
        label = r["label"]
        if label is not None and len(label) > MAX_LABEL_CHARS:
            label = label[:MAX_LABEL_CHARS] + "…"
        code = r["code"] if label is None else f"{r['code']} = {label}"
        groups.setdefault(name, []).append(code)
    listing = "; ".join(f"{name}: {', '.join(codes)}" for name, codes in groups.items())
    tail = " (list cut: see vault_query.list_vocabulary)" if truncated else ""
    return f"{head}. Codes per corpus: {listing}{tail}"
