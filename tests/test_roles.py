import logging

import pytest

import roles


class FakeCursor:
    """Records composed statements; answers the two probes ensure_roles issues.

    `keep=False` records nothing but a count: the traceback test needs a cursor
    whose own attributes cannot be what carries the password into a frame local
    (a composed statement's repr renders the Literal, password and all).
    """

    def __init__(self, fail_on=None, superuser=False, keep=True):
        self.executed = []
        self.count = 0
        self._probe = ""
        self._fail_on = fail_on
        self._superuser = superuser
        self._keep = keep

    def execute(self, statement, params=None):
        self.count += 1
        if isinstance(statement, str):
            # The two probes are the only plain strings ensure_roles executes;
            # everything else is a psycopg2.sql composable.
            self._probe = statement
        if self._keep:
            self.executed.append(statement)
        if self._fail_on is not None and self.count == self._fail_on:
            # Mimics psycopg2: the driver's error would carry cur.query, i.e.
            # the rendered ALTER ROLE with the password literal in it.
            raise RuntimeError("boom: ALTER ROLE vault_orchestrator PASSWORD 's3cr3t'")

    def fetchone(self):
        if "current_database" in self._probe:
            return ("documents",)
        return (self._superuser,)

    def statements(self):
        """The recorded statements as text (repr keeps the composed pieces)."""
        return [repr(statement) for statement in self.executed]


def test_no_password_env_means_no_roles_and_one_warning_each(caplog):
    with caplog.at_level(logging.WARNING):
        assert roles.roles_from_env({}) == {}
    text = caplog.text
    assert "VAULT_ORCHESTRATOR_PASSWORD" in text and "vault_orchestrator" in text
    assert "VAULT_READONLY_PASSWORD" in text and "vault_readonly" in text


def test_empty_string_counts_as_unset():
    # compose passes ${VAULT_READONLY_PASSWORD:-}, so "" is the normal
    # "operator has not set it" value, not a zero-length password.
    assert roles.roles_from_env({"VAULT_READONLY_PASSWORD": ""}) == {}


def test_roles_are_independent(caplog):
    with caplog.at_level(logging.WARNING):
        got = roles.roles_from_env({"VAULT_ORCHESTRATOR_PASSWORD": "s3cr3t"})
    assert got == {roles.ROLE_ORCHESTRATOR: "s3cr3t"}
    assert "VAULT_READONLY_PASSWORD" in caplog.text
    assert "s3cr3t" not in caplog.text


def test_password_never_reaches_the_log(caplog):
    cur = FakeCursor()
    with caplog.at_level(logging.DEBUG):
        roles.ensure_roles(cur, {roles.ROLE_ORCHESTRATOR: "s3cr3t"})
    assert "s3cr3t" not in caplog.text


def test_a_failing_role_statement_raises_without_leaking_the_password():
    cur = FakeCursor(fail_on=4)  # 1 current_database, 2 is_superuser, 3 DO, 4 ALTER ROLE
    with pytest.raises(RuntimeError) as exc:
        roles.ensure_roles(cur, {roles.ROLE_ORCHESTRATOR: "s3cr3t"})
    assert "vault_orchestrator" in str(exc.value)
    assert "s3cr3t" not in str(exc.value)
    assert exc.value.__cause__ is None  # `from None`: the driver error is dropped
    # Raised outside the except block, so Python never chained the driver error
    # as the implicit context either -- and that error carries cur.query, i.e.
    # the rendered ALTER ROLE with the password in it.
    assert exc.value.__context__ is None


def test_the_password_survives_in_no_frame_local_of_the_raised_traceback():
    # str(exc) is not the whole story: a reporter that prints frame locals
    # (pytest's --showlocals, Sentry, a bare traceback module call) walks the
    # traceback of the exception that escaped. Every frame it can reach must be
    # password-free except the caller's own {role: password} mapping.
    cur = FakeCursor(fail_on=4, keep=False)
    with pytest.raises(RuntimeError) as exc:
        roles.ensure_roles(cur, {roles.ROLE_ORCHESTRATOR: "s3cr3t"})

    walked = []
    tb = exc.value.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        for name, value in list(frame.f_locals.items()):
            walked.append((frame.f_code.co_name, name))
            if frame.f_code.co_name == "ensure_roles" and name == "passwords":
                continue  # the caller's mapping, by construction
            assert "s3cr3t" not in repr(value), \
                f"password reachable via {frame.f_code.co_name}.{name}"
        tb = tb.tb_next
    # the walk really reached the frame that composed the ALTER ROLE
    assert ("_provision", "password") in walked
    assert ("ensure_roles", "passwords") in walked


def test_statements_are_issued_in_the_documented_order_and_no_others():
    cur = FakeCursor()  # not a superuser: no log_statement fiddling at all
    roles.ensure_roles(cur, {roles.ROLE_ORCHESTRATOR: "s3cr3t"})
    got = cur.statements()
    grants = roles.grant_templates(roles.ROLE_ORCHESTRATOR)
    # 2 probes, DO guard, ALTER ROLE, the grants, then the schema-level REVOKE
    assert len(got) == 2 + 2 + len(grants) + 1
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in got[-1]
    assert "current_database" in got[0]
    assert "is_superuser" in got[1]
    assert "CREATE ROLE" in got[2]
    assert "ALTER ROLE" in got[3]
    assert not any("log_statement" in statement or
                   "log_min_error_statement" in statement or
                   "log_min_duration_statement" in statement for statement in got)
    for statement, template in zip(got[4:-1], grants):
        head = template.split("{")[0].strip()
        assert head in statement, f"expected {head!r} in {statement}"


def test_the_alter_role_spells_out_every_cluster_power_it_denies():
    # Attributes must converge on every run: a role widened out of band (or by
    # an older CREATE ROLE) is narrowed back by the next train, so the ALTER
    # names them all rather than relying on CREATE ROLE's defaults.
    cur = FakeCursor()
    roles.ensure_roles(cur, {roles.ROLE_READONLY: "s3cr3t"})
    alter = cur.statements()[3]
    for attribute in ("LOGIN", "NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE",
                      "NOREPLICATION", "NOBYPASSRLS", "PASSWORD"):
        assert attribute in alter, f"{attribute} missing from {alter}"


def test_log_statement_is_muted_around_the_alter_role_and_restored_right_after():
    # Scoped to the one statement that carries the literal: a server running
    # log_statement = 'ddl' keeps its audit trail of the GRANTs.
    # log_min_error_statement goes with it: its default logs a FAILING
    # statement in full, which for the ALTER ROLE means the password literal.
    # log_min_duration_statement goes with them: a server with slow-query
    # logging on (>= 0) logs the statement text of anything slow enough, and
    # the ALTER ROLE is the one statement whose text is the password.
    cur = FakeCursor(superuser=True)
    roles.ensure_roles(cur, {roles.ROLE_ORCHESTRATOR: "s3cr3t"})
    got = cur.statements()
    assert "SET LOCAL log_statement" in got[3]
    assert "SET LOCAL log_min_error_statement" in got[4]
    assert "SET LOCAL log_min_duration_statement = -1" in got[5]
    assert "ALTER ROLE" in got[6]
    assert "RESET log_statement" in got[7]
    assert "RESET log_min_error_statement" in got[8]
    assert "RESET log_min_duration_statement" in got[9]
    assert sum("log_statement" in statement for statement in got) == 2
    assert sum("log_min_error_statement" in statement for statement in got) == 2
    assert sum("log_min_duration_statement" in statement for statement in got) == 2


def test_grant_templates_are_the_documented_surface_and_nothing_more():
    orch = roles.grant_templates(roles.ROLE_ORCHESTRATOR)
    ro = roles.grant_templates(roles.ROLE_READONLY)
    for tpl in (orch, ro):
        assert tpl[0].startswith("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM")
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}" in tpl
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}" in tpl
        assert not any(bad in " ".join(tpl) for bad in
                       ("GRANT ALL", "SUPERUSER", "CREATEDB", "CREATEROLE",
                        "BYPASSRLS", "REPLICATION", "ALTER ROLE",
                        "WITH GRANT OPTION"))
    writes = ("GRANT INSERT ON runs TO {role}",
              "GRANT INSERT, UPDATE ON rag_ingestions TO {role}",
              "GRANT UPDATE (has_text_layer, page_count) ON documents TO {role}")
    assert set(writes) <= set(orch)
    assert not set(writes) & set(ro)
    assert not any(w in " ".join(ro) for w in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))


def test_the_train_revokes_the_public_schemas_inherited_create_right():
    # PostgreSQL 15 stopped granting CREATE on schema `public` to PUBLIC, but a
    # cluster initialised before 15 -- or one where someone re-granted it by
    # hand -- still does, and PUBLIC includes both vault roles. "vault_readonly
    # cannot CREATE TABLE" must not rest on the server's default, so the train
    # revokes it itself, once per run rather than once per role.
    cur = FakeCursor()
    roles.ensure_roles(cur, {roles.ROLE_READONLY: "s3cr3t"})
    got = cur.statements()
    hardening = [s for s in got if "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in s]
    assert len(hardening) == 1, got
    # ... and it is not aimed at a role: FROM PUBLIC, never FROM vault_readonly
    assert "vault_readonly" not in hardening[0]


def test_an_unconfigured_train_touches_nothing_at_all():
    # The dev/CI path: no password variable set, so no role, no grant -- and no
    # schema-level REVOKE either. A train that provisions nothing must leave
    # the cluster exactly as it found it.
    cur = FakeCursor()
    assert roles.ensure_roles(cur, {}) == []
    assert cur.statements() == []
