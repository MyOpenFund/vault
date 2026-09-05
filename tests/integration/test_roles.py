import psycopg2
import pytest

from .conftest import VAULT_ROLES, drop_vault_roles, role_conn, run_ingest

pytestmark = pytest.mark.integration

ORCH_PW = "orch-pw-A"
RO_PW = "ro-pw-A"

ROLES = VAULT_ROLES

VIEWS = ("runs_sources", "rag_backlog", "rag_backlog_any",
         "sources_without_cadence", "source_health")

TABLES = ("documents", "runs", "rag_ingestions", "cadence", "discovery_errors")


def train(monkeypatch, pg_url, data_dir, orch=ORCH_PW, ro=RO_PW):
    """One full DDL train with the given role passwords (None = variable unset)."""
    for var, value in (("VAULT_ORCHESTRATOR_PASSWORD", orch),
                       ("VAULT_READONLY_PASSWORD", ro)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    run_ingest(monkeypatch, pg_url, data_dir)


def scalar(pg_url, sql_text, params=None):
    conn = psycopg2.connect(pg_url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text, params)
            return cur.fetchone()
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def drop_the_roles_afterwards(pg_url):
    """Roles are cluster-level, not schema-level: `clean_db` cannot undo them.

    The container is module-scoped, so nothing this module creates can reach
    another module — but a role left behind would still outlive every test
    here, so the module hands the cluster back the way it found it.
    """
    yield
    drop_vault_roles(pg_url)


def test_roles_exist_with_no_cluster_powers(clean_db, tmp_path, monkeypatch):
    train(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname LIKE 'vault\\_%' ORDER BY rolname"
        )
        rows = cur.fetchall()
    conn.close()
    assert rows == [
        ("vault_orchestrator", True, False, False, False),
        ("vault_readonly", True, False, False, False),
    ]


def test_role_attributes_converge_when_someone_widened_them_by_hand(
    clean_db, tmp_path, monkeypatch
):
    # A role granted cluster powers out of band (a rushed psql session, an
    # older deploy script) must be brought back by the next train, not left
    # as it is: the ALTER ROLE spells out every attribute it wants off.
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'vault_orchestrator'")
        if not cur.fetchone():
            cur.execute("CREATE ROLE vault_orchestrator LOGIN")
        cur.execute("ALTER ROLE vault_orchestrator WITH SUPERUSER CREATEDB "
                    "CREATEROLE REPLICATION BYPASSRLS")
    conn.close()

    train(monkeypatch, clean_db, tmp_path)

    assert scalar(clean_db, """
        SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication,
               rolbypassrls, rolcanlogin
        FROM pg_roles WHERE rolname = 'vault_orchestrator'
    """) == (False, False, False, False, False, True)


def test_privilege_matrix(clean_db, tmp_path, monkeypatch):
    train(monkeypatch, clean_db, tmp_path)
    got = scalar(clean_db, """
        SELECT has_table_privilege('vault_orchestrator','documents','SELECT'),
               has_table_privilege('vault_orchestrator','documents','UPDATE'),
               has_column_privilege('vault_orchestrator','documents','has_text_layer','UPDATE'),
               has_column_privilege('vault_orchestrator','documents','page_count','UPDATE'),
               has_column_privilege('vault_orchestrator','documents','title','UPDATE'),
               has_table_privilege('vault_orchestrator','runs','INSERT'),
               has_table_privilege('vault_orchestrator','runs','UPDATE'),
               has_table_privilege('vault_orchestrator','rag_ingestions','INSERT'),
               has_table_privilege('vault_orchestrator','rag_ingestions','UPDATE'),
               has_table_privilege('vault_orchestrator','rag_ingestions','DELETE'),
               has_table_privilege('vault_readonly','documents','SELECT'),
               has_table_privilege('vault_readonly','runs','INSERT'),
               has_table_privilege('vault_readonly','documents','UPDATE'),
               has_schema_privilege('vault_readonly','public','CREATE')
    """)
    #      SELECT docs, UPDATE docs (table), has_text_layer, page_count, title,
    #      runs INSERT/UPDATE, rag INSERT/UPDATE/DELETE, ro SELECT/INSERT/UPDATE, ro CREATE
    assert got == (True, False, True, True, False,
                   True, False, True, True, False,
                   True, False, False, False)


def test_both_roles_read_every_table_and_view_and_can_destroy_none_of_them(
    clean_db, tmp_path, monkeypatch
):
    # The matrix above pins the interesting cells one by one; this one sweeps
    # the whole schema so a table or view added later is covered by default
    # (SELECT) and cannot quietly arrive with a destructive privilege.
    train(monkeypatch, clean_db, tmp_path)
    for role in ROLES:
        for relation in TABLES + VIEWS:
            assert scalar(clean_db, "SELECT has_table_privilege(%s, %s, 'SELECT')",
                          (role, relation)) == (True,), f"{role} cannot read {relation}"
            for destructive in ("DELETE", "TRUNCATE"):
                assert scalar(clean_db, f"SELECT has_table_privilege(%s, %s, '{destructive}')",
                              (role, relation)) == (False,), \
                    f"{role} has {destructive} on {relation}"


def test_readonly_holds_select_and_nothing_else_on_every_relation(
    clean_db, tmp_path, monkeypatch
):
    train(monkeypatch, clean_db, tmp_path)
    for relation in TABLES + VIEWS:
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE",
                          "REFERENCES", "TRIGGER"):
            assert scalar(clean_db, f"SELECT has_table_privilege(%s, %s, '{privilege}')",
                          ("vault_readonly", relation)) == (False,), \
                f"vault_readonly has {privilege} on {relation}"


def test_view_grants_survive_the_nightly_drop_and_recreate(clean_db, tmp_path, monkeypatch):
    # The regression this chantier exists for: the train DROPs every view at its
    # head and recreates it at its tail, so a grant issued once would be gone
    # after the second run. One train would pass even with a broken design.
    train(monkeypatch, clean_db, tmp_path)
    train(monkeypatch, clean_db, tmp_path)
    for role in ROLES:
        for view in VIEWS:
            assert scalar(clean_db, "SELECT has_table_privilege(%s, %s, 'SELECT')",
                          (role, view)) == (True,), f"{role} lost SELECT on {view}"
    # and it is really readable, not just marked as such
    for role, password in (("vault_orchestrator", ORCH_PW), ("vault_readonly", RO_PW)):
        conn = role_conn(clean_db, role, password)
        for view in VIEWS:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {view}")
                assert cur.fetchone() is not None
        conn.close()


def test_default_privileges_cover_a_table_created_after_the_train(
    clean_db, tmp_path, monkeypatch
):
    # ALTER DEFAULT PRIVILEGES is the safety net for the window between a new
    # table appearing and the next train's explicit GRANT: whatever docuser
    # creates from now on is readable by both roles the moment it exists.
    train(monkeypatch, clean_db, tmp_path)
    conn = psycopg2.connect(clean_db)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t_created_later (x int)")
        for role in ROLES:
            assert scalar(clean_db, "SELECT has_table_privilege(%s, 't_created_later', 'SELECT')",
                          (role,)) == (True,), f"{role} cannot read a later table"
            assert scalar(clean_db, "SELECT has_table_privilege(%s, 't_created_later', 'INSERT')",
                          (role,)) == (False,)
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS t_created_later CASCADE")
        conn.close()


def test_readonly_role_cannot_write(clean_db, tmp_path, monkeypatch):
    train(monkeypatch, clean_db, tmp_path)
    conn = role_conn(clean_db, "vault_readonly", RO_PW)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM documents")
    for stmt in ("INSERT INTO runs (run_id, tool) VALUES ('x','y')",
                 "UPDATE documents SET page_count = 1",
                 "DELETE FROM rag_ingestions"):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute(stmt)
    conn.close()


def test_password_rotation_applies_on_every_train(clean_db, tmp_path, monkeypatch):
    train(monkeypatch, clean_db, tmp_path, orch="rot-1")
    role_conn(clean_db, "vault_orchestrator", "rot-1").close()
    train(monkeypatch, clean_db, tmp_path, orch=ORCH_PW)  # back to the module default
    role_conn(clean_db, "vault_orchestrator", ORCH_PW).close()
    with pytest.raises(psycopg2.OperationalError) as exc:
        role_conn(clean_db, "vault_orchestrator", "rot-1")
    # The old password is REJECTED, not merely unusable for some other reason
    # (a role that vanished, a connection refused) — those would also raise
    # OperationalError and would make this test pass for the wrong reason.
    assert "password authentication failed" in str(exc.value)


def test_pg_hba_trusts_only_the_containers_own_loopback(clean_db, tmp_path, monkeypatch):
    # A published port DNATs to the container's eth0 address, not its loopback,
    # so every off-host client lands on the scram-sha-256 rule. Nothing in this
    # chantier edits pg_hba; this test pins that assumption.
    train(monkeypatch, clean_db, tmp_path)
    assert scalar(clean_db, """
        SELECT count(*) FROM pg_hba_file_rules
         WHERE auth_method = 'trust'
           AND address IS NOT NULL
           AND address NOT IN ('127.0.0.1', '::1')
    """) == (0,)
