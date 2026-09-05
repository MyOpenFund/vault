"""Least-privilege database roles for the vault (issue #5).

Two LOGIN roles, owned by the DDL train exactly the way the tables and views
are: `vault_orchestrator` (the off-host data-orchestrator: SELECT everywhere,
INSERT on runs, INSERT/UPDATE on rag_ingestions, and a COLUMN-level UPDATE on
documents' two probe facts) and `vault_readonly` (Metabase's analytics
connection, the API, the future agent: SELECT and nothing else).

Why here and not in deploy/initdb/: initdb runs once, on an empty data dir, so
it would never execute on an existing cluster -- and the train DROPs and
recreates all five views on every run, which destroys any grant held on them.
Re-issuing the grants after the CREATE VIEWs, inside the same transaction, is
what makes them survive the night.

Passwords come from the environment (see PASSWORD_ENV). A role whose variable
is unset or empty is skipped with a WARNING, together with its grants: that is
the dev/CI path, where no role exists at all. Rotation is "change the variable,
run the ingestion service once".

Secrets discipline: the password is composed with psycopg2.sql.Literal, never
interpolated, never logged, and never allowed to travel inside an exception
(psycopg2 errors carry cur.query).
"""

import logging

from psycopg2 import sql

log = logging.getLogger("ingest")

ROLE_ORCHESTRATOR = "vault_orchestrator"
ROLE_READONLY = "vault_readonly"
ROLES = (ROLE_ORCHESTRATOR, ROLE_READONLY)

PASSWORD_ENV = {
    ROLE_ORCHESTRATOR: "VAULT_ORCHESTRATOR_PASSWORD",
    ROLE_READONLY: "VAULT_READONLY_PASSWORD",
}

# Read surface, identical for both roles. GRANT ... ON ALL TABLES is evaluated
# at execution time, so the views recreated a few statements earlier in the same
# train are included; the ALTER DEFAULT PRIVILEGES covers anything a LATER train
# creates, before its own explicit grant runs.
_READ_GRANTS = (
    "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}",  # converge, don't accumulate
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE ON SCHEMA public TO {role}",
    "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}",
)

# Write surface: the data-orchestrator's four statements and nothing else.
# documents' UPDATE is column-level on purpose -- the probe pass writes those
# two facts and must not be able to touch a title, a path or a tombstone.
# No sequence grant: the orchestrator never INSERTs into documents, the only
# table with a SERIAL. Referential-integrity checks on rag_ingestions' FK run
# with the table owner's rights, so they need no grant on documents either.
WRITE_GRANTS = {
    ROLE_ORCHESTRATOR: (
        "GRANT INSERT ON runs TO {role}",
        "GRANT INSERT, UPDATE ON rag_ingestions TO {role}",
        "GRANT UPDATE (has_text_layer, page_count) ON documents TO {role}",
    ),
    ROLE_READONLY: (),
}

_CREATE_ROLE_SQL = """
DO $do$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {name}) THEN
        CREATE ROLE {role} LOGIN;
    END IF;
END $do$
"""

# Every attribute is spelled out, including the ones that are already the
# CREATE ROLE default: this statement runs on every train, so it is what makes
# the role's cluster powers CONVERGE. A role widened out of band (a rushed psql
# session, an older deploy script) is narrowed back the next night instead of
# keeping whatever it was given.
_ALTER_ROLE_SQL = (
    "ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
    "NOBYPASSRLS PASSWORD {password}"
)


def grant_templates(role):
    """Every grant statement template issued for `role`, in order.

    Password-free by construction (the ALTER ROLE is built separately), which
    is what makes the privilege surface unit-testable without a database.
    """
    return _READ_GRANTS + WRITE_GRANTS[role]


def roles_from_env(env):
    """{role: password} for the roles whose password variable is set.

    An unset OR empty value means "not configured": compose forwards
    ${VAR:-}, so empty is what an operator who has not set it produces.
    """
    provisioned = {}
    for role in ROLES:
        var = PASSWORD_ENV[role]
        password = env.get(var) or ""
        if password:
            provisioned[role] = password
        else:
            log.warning(f"{var} not set — skipping role {role} (no grants issued)")
    return provisioned


def ensure_roles(cur, passwords):
    """Create/refresh the roles in `passwords` and (re)issue their grants.

    Must run on the DDL train's cursor, inside its transaction, after the
    views have been recreated. Returns the role names provisioned this run.
    """
    if not passwords:
        return []
    cur.execute("SELECT current_database()")
    (dbname,) = cur.fetchone()
    quiet = _can_quiet_password_logging(cur)

    provisioned = []
    for role in ROLES:
        if role not in passwords:
            continue
        _provision(cur, role, passwords[role], dbname, quiet)
        provisioned.append(role)
    log.info(f"roles provisioned: {', '.join(provisioned)}")
    return provisioned


def _can_quiet_password_logging(cur):
    """Whether this connection may mute log_statement around the ALTER ROLE.

    log_statement is SUSET, so the SET is only attempted on a superuser
    connection -- a failed SET would abort the whole train's transaction.
    """
    cur.execute("SELECT current_setting('is_superuser') = 'on'")
    (is_superuser,) = cur.fetchone()
    return is_superuser


def _provision(cur, role, password, dbname, quiet=False):
    ident = sql.Identifier(role)
    alter_role = [
        sql.SQL(_ALTER_ROLE_SQL).format(role=ident, password=sql.Literal(password)),
    ]
    if quiet:
        # Muted for exactly one statement -- the only one carrying the literal
        # -- and restored immediately: a server running log_statement = 'ddl'
        # keeps its audit trail of every GRANT this function issues. (The
        # default is 'none', so on most servers this changes nothing.) On the
        # failure path the RESET is skipped, which is harmless: the train's
        # transaction is already doomed and SET LOCAL dies with it.
        alter_role = (
            [sql.SQL("SET LOCAL log_statement = 'none'")]
            + alter_role
            + [sql.SQL("RESET log_statement")]
        )
    statements = [
        sql.SQL(_CREATE_ROLE_SQL).format(name=sql.Literal(role), role=ident),
    ] + alter_role + [
        sql.SQL(tpl).format(role=ident, db=sql.Identifier(dbname))
        for tpl in grant_templates(role)
    ]
    for statement in statements:
        try:
            cur.execute(statement)
        except Exception:
            # Clear this frame before the traceback captures it: `statement`,
            # `statements` and `alter_role` hold a Literal whose repr is the
            # password, and a --showlocals-style reporter would print it.
            statement = statements = alter_role = password = None
            # `from None` is load-bearing: a chained psycopg2 error carries
            # cur.query, i.e. the rendered ALTER ROLE with the password in it.
            raise RuntimeError(f"failed to provision role {role}") from None
