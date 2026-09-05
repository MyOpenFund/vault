import logging
import os
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras

log = logging.getLogger("uvicorn.error")

DATABASE_URL = os.environ.get("DATABASE_URL")

# The role the API is meant to connect as (compose.yaml). Named here so the
# startup error can say which role is missing without parsing a DSN that may
# not even be well formed.
EXPECTED_ROLE = "vault_readonly"

# libpq's default is no timeout at all: a host that DROPs rather than REJECTs
# (a firewall rule, a stale tailnet address) leaves connect() waiting out the
# OS TCP retry budget -- around two minutes. Ten seconds is far past a healthy
# same-compose-network connect and short enough that the startup gate
# crash-loops visibly instead of hanging, and a request fails instead of
# holding a worker.
CONNECT_TIMEOUT_SECONDS = 10

FIRST_BOOT_HINT = (
    "The vault's least-privilege roles are created by the DDL train, so on any "
    "cluster that has not run it yet %s does not exist until the ingestion "
    "service has run once: `docker compose run --rm ingestion`. Check VAULT_READONLY_PASSWORD "
    "is set to the same value for both services. This container now exits "
    "non-zero; `restart: unless-stopped` will retry it."
)

# The other way this fails right after an .env edit, and the one that reads as
# a bug in the code rather than in the .env: compose interpolates the value
# into `postgresql://vault_readonly:<pw>@postgres:5432/documents`, so a
# password holding a URL delimiter re-cuts the DSN and libpq rejects (or
# misroutes) something that looks perfectly fine in the .env. `$` never even
# reaches libpq -- compose expands it first.
PASSWORD_CHARSET_HINT = (
    "If VAULT_READONLY_PASSWORD was just set or rotated: the value is "
    "interpolated by compose and embedded in a postgresql:// URL, so it must "
    "be URL-safe -- no $ / @ : # % ? characters. Generate one with "
    "`openssl rand -hex 24` (NOT `openssl rand -base64`)."
)


@contextmanager
def get_conn():
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        yield conn
    finally:
        conn.close()


def _redact(text):
    """The driver's message with the DSN password blanked out.

    psycopg2 quotes the connection string back for a malformed DSN, and the
    API's DSN carries VAULT_READONLY_PASSWORD. Cheap insurance: the password
    is never worth putting in a log line, whoever wrote the message.

    A password shorter than 4 characters is a substring of half the words in an
    English error message, so replacing it everywhere would mangle the message
    without hiding the `user:pw@host` that leaks it anyway. Below that length
    the DSN's whole authority is redacted instead.

    When urlsplit finds no password at all the whole DSN is blanked wherever
    the driver echoed it. That is not just the key=value case: a password
    holding a URL delimiter (see PASSWORD_CHARSET_HINT) re-cuts the URL so the
    `@` no longer sits in the authority, urlsplit reports no password, and
    targeted redaction would hand the entire literal to the log -- in exactly
    the failure the charset rule exists to prevent.
    """
    try:
        parts = urlsplit(DATABASE_URL or "")
        password, netloc = parts.password, parts.netloc
    except ValueError:  # unparseable DSN: nothing to parse, so blank it whole
        return text.replace(DATABASE_URL, "***") if DATABASE_URL else text
    if not password:
        return text.replace(DATABASE_URL, "***") if DATABASE_URL else text
    if len(password) >= 4:
        return text.replace(password, "***")
    return text.replace(netloc, "***")


def verify_connection():
    """Open and close one connection, or fail the process loudly.

    Called once at startup (see main.lifespan). Without it the API comes up
    happily against a database it cannot reach, answers /health with "ok", and
    returns 500 on every route that touches the corpus -- the silent version of
    a failure that `docker compose ps` should be shouting about.
    """
    reason = None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {_redact(str(exc)).strip()}"
    if reason is not None:
        log.error("vault API cannot connect to Postgres as %s: %s",
                  EXPECTED_ROLE, reason)
        log.error(FIRST_BOOT_HINT, EXPECTED_ROLE)
        log.error(PASSWORD_CHARSET_HINT)
        # Raised OUTSIDE the handler, like ingestion/roles.py does: inside it,
        # Python would chain the driver error onto __context__, and that error
        # drags its own traceback -- whose psycopg2.connect frame holds the DSN,
        # password and all. Out here the exception state is already cleared.
        raise RuntimeError("cannot reach the vault database") from None
    conn.close()


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
