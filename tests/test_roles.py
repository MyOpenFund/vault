import logging

import pytest

import roles


class FakeCursor:
    """Records composed statements; answers the two probes ensure_roles issues."""

    def __init__(self, fail_on=None):
        self.executed = []
        self._fail_on = fail_on

    def execute(self, statement, params=None):
        self.executed.append(statement)
        if self._fail_on is not None and len(self.executed) == self._fail_on:
            # Mimics psycopg2: the driver's error would carry cur.query, i.e.
            # the rendered ALTER ROLE with the password literal in it.
            raise RuntimeError("boom: ALTER ROLE vault_orchestrator PASSWORD 's3cr3t'")

    def fetchone(self):
        if "current_database" in str(self.executed[-1]):
            return ("documents",)
        return (False,)  # not superuser -> the SET LOCAL log_statement is skipped


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


def test_grant_templates_are_the_documented_surface_and_nothing_more():
    orch = roles.grant_templates(roles.ROLE_ORCHESTRATOR)
    ro = roles.grant_templates(roles.ROLE_READONLY)
    for tpl in (orch, ro):
        assert tpl[0].startswith("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM")
        assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}" in tpl
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role}" in tpl
        assert not any(bad in " ".join(tpl) for bad in
                       ("GRANT ALL", "SUPERUSER", "CREATEDB", "CREATEROLE", "WITH GRANT OPTION"))
    writes = ("GRANT INSERT ON runs TO {role}",
              "GRANT INSERT, UPDATE ON rag_ingestions TO {role}",
              "GRANT UPDATE (has_text_layer, page_count) ON documents TO {role}")
    assert set(writes) <= set(orch)
    assert not set(writes) & set(ro)
    assert not any(w in " ".join(ro) for w in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
