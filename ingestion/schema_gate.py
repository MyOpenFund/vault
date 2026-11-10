"""Gate and lock discipline for the DDL train (vault #4).

The train in ingest.py drops and recreates the five views and issues ALTER
TABLEs: every statement of that kind takes an ACCESS EXCLUSIVE lock, which
waits behind any open reader (Metabase, the API, the MCP) and, while it waits,
makes every NEW reader queue behind it -- a convoy. Running it on every pass
made each nightly run a convoy risk for nothing, since the schema almost never
changes. This module holds the two halves of the fix:

1. The gate. The train text is fingerprinted (sha256 of the SQL) and the
   fingerprint is stored in the one-row table `vault_schema` by the train
   itself, in the train's transaction. A later run skips the train when the
   stored fingerprint matches AND every relation the train owns still exists
   (`expected_relations`) AND VAULT_DDL_FORCE is not set. Any edit of the
   train text therefore re-runs it exactly once; a dropped table or view
   re-runs it by itself; VAULT_DDL_FORCE=1 covers manual drift the gate
   cannot see (a dropped column, an index altered by hand).

2. Bounded lock waits. When the train does run, it runs with
   `SET LOCAL lock_timeout` (VAULT_DDL_LOCK_TIMEOUT), so a stuck reader costs
   at most that long a queue, never an open-ended one. On a lock timeout the
   transaction is rolled back, the sessions holding locks on the train's
   relations are logged (never their query text: it can carry literals), and
   the train is retried after each delay of VAULT_DDL_RETRY_DELAYS. After the
   last attempt the run fails loudly (TrainLockTimeout): new code may need the
   new schema, so it must not ingest against the old one.

Everything here is pure or takes the connection as an argument; the wiring
(advisory lock, gate read, roles) lives in ingest.main().
"""

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass

import psycopg2.errors

log = logging.getLogger("ingest")

ENV_LOCK_TIMEOUT = "VAULT_DDL_LOCK_TIMEOUT"
ENV_RETRY_DELAYS = "VAULT_DDL_RETRY_DELAYS"
ENV_FORCE = "VAULT_DDL_FORCE"

# Defaults. 5 s bounds the queue a waiting train imposes on new readers; three
# attempts spread over ~45 s ride out a dashboard refresh or an API page
# without stretching a nightly run. Both are operator-tunable (README,
# .env.example), these are only the values used when nothing is set.
DEFAULT_LOCK_TIMEOUT = "5s"
DEFAULT_RETRY_DELAYS = (10.0, 30.0)

# Literal value of VAULT_DDL_RETRY_DELAYS meaning "one attempt, no retry".
# Not the empty string: compose forwards ${VAR:-}, so empty is what an
# operator who set nothing produces, and that must mean the default.
NO_RETRY = "none"

_TRUE = frozenset({"1", "true", "yes"})
_FALSE = frozenset({"0", "false", "no", ""})

# lock_timeout is an int GUC in milliseconds: Postgres refuses anything above
# INT_MAX ("value out of bounds"), so it is refused here, before connecting.
POSTGRES_MAX_LOCK_TIMEOUT_MS = 2**31 - 1

# Postgres time units accepted for lock_timeout (case-sensitive on the server
# too), with their value in milliseconds. A bare number is milliseconds.
_UNIT_MS = {"ms": 1, "s": 1000, "min": 60_000}
_LOCK_TIMEOUT_RE = re.compile(r"([0-9]+)(ms|s|min)?", re.ASCII)
_DELAY_RE = re.compile(r"[0-9]+(\.[0-9]+)?", re.ASCII)


class SettingsError(ValueError):
    """A VAULT_DDL_* variable holds a value the train cannot use."""


class TrainLockTimeout(RuntimeError):
    """The train could not get its locks within the configured attempts."""


@dataclass(frozen=True)
class GateSettings:
    lock_timeout: str           # normalized Postgres interval, e.g. "5s"
    lock_timeout_ms: int
    retry_delays: tuple         # seconds to sleep before attempts 2..N
    force: bool

    @property
    def attempts(self):
        return len(self.retry_delays) + 1


def _refuse(var, value, why):
    raise SettingsError(f"{var}={value!r} is invalid: {why}")


def _parse_lock_timeout(raw):
    if raw is None or raw.strip() == "":
        raw_value = DEFAULT_LOCK_TIMEOUT
    else:
        raw_value = raw
    m = _LOCK_TIMEOUT_RE.fullmatch(raw_value.strip())
    if not m:
        _refuse(ENV_LOCK_TIMEOUT, raw,
                "expected a positive integer with an optional unit ms, s or min (e.g. 5s)")
    amount, unit = int(m.group(1)), m.group(2) or "ms"
    ms = amount * _UNIT_MS[unit]
    if amount <= 0:
        # 0 means "wait forever" to Postgres: exactly the convoy this gate removes.
        _refuse(ENV_LOCK_TIMEOUT, raw, "must be greater than zero (0 disables the timeout)")
    if ms > POSTGRES_MAX_LOCK_TIMEOUT_MS:
        _refuse(ENV_LOCK_TIMEOUT, raw,
                f"above Postgres's limit of {POSTGRES_MAX_LOCK_TIMEOUT_MS} ms")
    return f"{amount}{unit}", ms


def _parse_retry_delays(raw):
    if raw is None or raw.strip() == "":
        return DEFAULT_RETRY_DELAYS
    if raw.strip().lower() == NO_RETRY:
        return ()
    delays = []
    for item in raw.split(","):
        item = item.strip()
        if not _DELAY_RE.fullmatch(item):
            _refuse(ENV_RETRY_DELAYS, raw,
                    "expected comma-separated non-negative numbers of seconds "
                    f"(e.g. 10,30), or {NO_RETRY!r} for a single attempt")
        value = float(item)
        if not math.isfinite(value):
            _refuse(ENV_RETRY_DELAYS, raw, "delays must be finite")
        delays.append(value)
    return tuple(delays)


def _parse_force(raw):
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    _refuse(ENV_FORCE, raw, "expected 1/true/yes or 0/false/no (or unset)")


def load_settings(env):
    """Parse and validate the three VAULT_DDL_* variables from `env`.

    Raises SettingsError naming the variable and the value. Called before
    connecting, so a typo fails the run without touching the database.
    """
    lock_timeout, lock_timeout_ms = _parse_lock_timeout(env.get(ENV_LOCK_TIMEOUT))
    return GateSettings(
        lock_timeout=lock_timeout,
        lock_timeout_ms=lock_timeout_ms,
        retry_delays=_parse_retry_delays(env.get(ENV_RETRY_DELAYS)),
        force=_parse_force(env.get(ENV_FORCE)),
    )


def fingerprint(sql_text):
    """sha256 hex of the train text. Any edit, comments included, re-runs it once."""
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


_COMMENT_RE = re.compile(r"--[^\n]*")
_RELATION_RE = re.compile(
    r"\bCREATE\s+(?:TABLE\s+IF\s+NOT\s+EXISTS|VIEW)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def expected_relations(sql_text):
    """Tables (`CREATE TABLE IF NOT EXISTS x`) and views (`CREATE VIEW x`) the
    train creates, in order of appearance, lower-cased, without duplicates.

    `--` comments are stripped first (the train's prose mentions CREATE
    statements). The train holds no string literal containing `--`; if it ever
    does, the unit test pinning the real train's list catches the slip.
    """
    names = []
    for name in _RELATION_RE.findall(_COMMENT_RE.sub("", sql_text)):
        name = name.lower()
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("no CREATE TABLE IF NOT EXISTS / CREATE VIEW found in the train")
    return tuple(names)


def stale_reason(stored, expected, missing, force):
    """Why the train must run, or None when the schema is current.

    `stored` is the fingerprint read from vault_schema (None when the table or
    its row is absent), `expected` the fingerprint of the train in this code,
    `missing` the train's relations that do not exist right now.
    """
    if force:
        return f"forced by {ENV_FORCE}"
    if missing:
        return f"missing relation(s): {', '.join(missing)}"
    if stored is None:
        return "no fingerprint recorded yet (first run with the gate)"
    if stored != expected:
        return f"train changed (recorded {stored[:12]}, code {expected[:12]})"
    return None


MISSING_RELATIONS_SQL = (
    "SELECT r FROM unnest(%s::text[]) WITH ORDINALITY AS t(r, i) "
    "WHERE to_regclass(r) IS NULL ORDER BY i"
)
STORED_FINGERPRINT_SQL = "SELECT fingerprint FROM vault_schema WHERE singleton"
GATE_TABLE = "vault_schema"


def read_state(conn, relations):
    """(stored fingerprint or None, missing relations) in one short transaction.

    Only catalog lookups and a one-row SELECT: ACCESS SHARE at most, so the
    gate read itself never waits behind a reader. Must run under the
    `vault-ddl` advisory lock, like the train it decides about.
    """
    with conn.cursor() as cur:
        cur.execute(MISSING_RELATIONS_SQL, (list(relations),))
        missing = tuple(r for (r,) in cur.fetchall())
        stored = None
        if GATE_TABLE not in missing:
            cur.execute(STORED_FINGERPRINT_SQL)
            row = cur.fetchone()
            stored = row[0] if row else None
    conn.commit()
    return stored, missing


SET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = %s"

# Sessions holding a granted lock on one of the train's relations. Not
# pg_blocking_pids(): once lock_timeout fired, the train is no longer waiting,
# so there is nothing left to ask it about. A granted lock on a relation the
# train must lock ACCESS EXCLUSIVE is, by definition, what it waited behind.
# NEVER select pg_stat_activity.query: it can carry literals (a password in an
# ALTER ROLE, client data in a WHERE clause).
BLOCKERS_SQL = """
SELECT a.pid, a.usename, a.application_name, a.state,
       round(extract(epoch FROM now() - a.xact_start)::numeric, 1) AS xact_age_s,
       string_agg(DISTINCT c.relname, ', ' ORDER BY c.relname) AS relations
FROM pg_locks l
JOIN pg_class c ON c.oid = l.relation
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.granted
  AND l.pid <> pg_backend_pid()
  AND l.relation = ANY (
        SELECT to_regclass(r)::oid FROM unnest(%s::text[]) AS r
         WHERE to_regclass(r) IS NOT NULL)
GROUP BY a.pid, a.usename, a.application_name, a.state, a.xact_start
ORDER BY a.xact_start NULLS LAST, a.pid
"""


def _describe_blockers(conn, relations):
    """One line per session holding a lock on the train's relations.

    Runs in its own short transaction (the train's was rolled back) and never
    raises: failing to describe the blockers must not mask the lock timeout.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(BLOCKERS_SQL, (list(relations),))
            rows = cur.fetchall()
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return f"could not list the blocking sessions ({type(exc).__name__})"
    if not rows:
        return "no session holds a lock on the train's relations any more"
    return "; ".join(
        f"pid={pid} user={user!r} application={app!r} state={state!r} "
        f"xact_age={age}s relations={rels}"
        for pid, user, app, state, age, rels in rows
    )


# Indirection so a test can act between two attempts (release a reader)
# without patching time.sleep for the whole process.
_sleep = time.sleep


def run_with_lock_retries(conn, apply, settings, relations, sleep=None):
    """Run `apply(cur)` in one transaction under a bounded lock wait, retrying.

    Each attempt: `SET LOCAL lock_timeout`, `apply(cur)`, commit. On
    LockNotAvailable: rollback, one WARNING naming the sessions holding locks
    on `relations`, sleep the next delay, try again. After the last attempt,
    TrainLockTimeout. Any other error is rolled back and re-raised at once (a
    broken train is not made better by retrying it).
    """
    if sleep is None:
        sleep = _sleep
    attempts = settings.attempts
    for attempt in range(1, attempts + 1):
        try:
            with conn.cursor() as cur:
                cur.execute(SET_LOCK_TIMEOUT_SQL, (settings.lock_timeout,))
                apply(cur)
            conn.commit()
            if attempt > 1:
                log.info(f"DDL train applied on attempt {attempt}/{attempts}")
            return
        except psycopg2.errors.LockNotAvailable:
            conn.rollback()
        except Exception:
            conn.rollback()
            raise
        blockers = _describe_blockers(conn, relations)
        if attempt < attempts:
            delay = settings.retry_delays[attempt - 1]
            log.warning(
                f"DDL train attempt {attempt}/{attempts} could not get its locks within "
                f"{settings.lock_timeout} ({ENV_LOCK_TIMEOUT}); blocking sessions: "
                f"{blockers}; retrying in {delay:g}s"
            )
            sleep(delay)
        else:
            log.warning(
                f"DDL train attempt {attempt}/{attempts} could not get its locks within "
                f"{settings.lock_timeout} ({ENV_LOCK_TIMEOUT}); blocking sessions: "
                f"{blockers}; giving up"
            )
    raise TrainLockTimeout(
        f"DDL train could not get its locks after {attempts} attempt(s) "
        f"({ENV_LOCK_TIMEOUT}={settings.lock_timeout}, "
        f"{ENV_RETRY_DELAYS}={','.join(f'{d:g}' for d in settings.retry_delays) or NO_RETRY}); "
        "nothing was ingested — see the WARNINGs above for the sessions holding the locks"
    )
