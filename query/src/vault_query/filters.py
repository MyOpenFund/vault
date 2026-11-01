"""The WHERE-clause builder every document read shares, and the fields it accepts."""

# Largest value a bound may take: Postgres reads an int parameter wider than
# int4 as bigint/numeric, and no page or text bound needs more.
MAX_BOUND = 2**31 - 1


# Fields allowing an exact-match filter (?source_code=us for example)
FILTERABLE_FIELDS = {
    "corpus", "source_code", "doc_type", "language", "provenance",
    "year", "mime_type",
}

SORTABLE_FIELDS = {
    "date", "year", "corpus", "source_code", "doc_type", "title", "created_at",
}


def build_where_clause(filters: dict, include_deleted: bool = False) -> tuple[str, list]:
    """Build a parameterized WHERE clause from a dict of filters.

    - simple values -> equality
    - `date_from` / `date_to` -> bounds on the `date` column
    - `q` -> free-text search on `title` (ILIKE)
    - by default, excludes soft-deleted documents (`deleted_at IS NULL`)
    """
    clauses = []
    params = []

    if not include_deleted:
        clauses.append("deleted_at IS NULL")

    for field in FILTERABLE_FIELDS:
        value = filters.get(field)
        if value is not None:
            clauses.append(f"{field} = %s")
            params.append(value)

    date_from = filters.get("date_from")
    if date_from:
        clauses.append("date >= %s")
        params.append(date_from)

    date_to = filters.get("date_to")
    if date_to:
        clauses.append("date <= %s")
        params.append(date_to)

    q = filters.get("q")
    if q:
        clauses.append("title ILIKE %s")
        params.append(f"%{q}%")

    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, params


class InvalidQuery(ValueError):
    """A caller-supplied argument the queries refuse (unknown sort field, bad bound...).

    Raised before any SQL runs. The API maps it to HTTP 400, the MCP server to an
    `invalid_argument` tool error.
    """


def check_int(name, value, minimum, maximum=MAX_BOUND):
    """Refuse a caller-supplied bound that is not a plain int in [minimum, maximum].

    A bool is refused although it is an int subclass (`limit=True` is a caller
    bug, not "1"); so are a numeric str, a float and None. Raises InvalidQuery
    naming the argument, before any SQL runs.
    """
    if not (isinstance(value, int) and not isinstance(value, bool)
            and minimum <= value <= maximum):
        raise InvalidQuery(f"{name} must be an integer between {minimum} and {maximum}")
