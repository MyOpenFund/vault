"""Text assertions on `compose.yaml` / `.env.example` (issue #5).

Deliberately textual and dependency-free: `pyyaml` is not a dev dependency, and
the things worth pinning here are the *literal* forms an operator can get wrong
in one keystroke — a bind address that becomes `0.0.0.0`, a `DATABASE_URL` that
goes back to the superuser, a healthcheck that goes green on the entrypoint's
temporary init server. A parsed-YAML assertion would happily accept
`- "5432:5432"`, which is exactly the line this file exists to forbid.

The healthcheck's runtime behaviour cannot be pinned from here (it needs a
cluster with and without `deploy/initdb/`); it was verified by hand instead, and
`tests/integration/test_compose_config.py` proves the file still parses and
publishes on the loopback with no `.env` but `.env.example`.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "compose.yaml").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


def test_postgres_is_published_on_one_interface_never_on_all_of_them():
    assert '"${POSTGRES_BIND_ADDR:-127.0.0.1}:${POSTGRES_BIND_PORT:-5432}:5432"' in COMPOSE
    assert "0.0.0.0" not in COMPOSE and "0.0.0.0" not in ENV_EXAMPLE
    # a bind-address-less mapping publishes on every interface, firewall included
    assert not re.search(r'^\s*-\s*"?\d*:?5432:5432"?\s*$', COMPOSE, re.M)


def test_only_the_ingestion_service_still_uses_the_superuser():
    assert COMPOSE.count("postgresql://docuser:") == 1
    # `:?` and not `:-`: an unset VAULT_READONLY_PASSWORD must fail at
    # `compose up`, not silently build a DSN with an empty password that only
    # fails later, inside the container, as an authentication error.
    assert ("postgresql://vault_readonly:"
            "${VAULT_READONLY_PASSWORD:?set it in .env — see .env.example}"
            "@postgres:5432/documents") in COMPOSE


def test_ingestion_receives_both_role_passwords():
    assert "VAULT_ORCHESTRATOR_PASSWORD: ${VAULT_ORCHESTRATOR_PASSWORD:-}" in COMPOSE
    assert "VAULT_READONLY_PASSWORD: ${VAULT_READONLY_PASSWORD:-}" in COMPOSE


def test_healthcheck_probes_the_real_server_and_the_metabase_database():
    # pg_isready over the unix socket goes green on the entrypoint's TEMPORARY
    # init server; -h 127.0.0.1 and a query against `metabase` do not.
    assert "pg_isready -h 127.0.0.1 -U docuser -d documents" in COMPOSE
    assert "psql -h 127.0.0.1 -U docuser -d metabase" in COMPOSE


def test_env_example_documents_every_new_variable():
    for var in ("VAULT_ORCHESTRATOR_PASSWORD", "VAULT_READONLY_PASSWORD",
                "POSTGRES_BIND_ADDR", "POSTGRES_BIND_PORT"):
        assert f"\n{var}=" in ENV_EXAMPLE or f"\n#{var}=" in ENV_EXAMPLE


def test_the_repo_never_carries_a_real_tailnet_address():
    # The NAS's interface IP belongs in its Dockge .env and nowhere else; the
    # repo ships the placeholder only.
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                         COMPOSE.replace("127.0.0.1", "")), "a literal IP in compose.yaml"
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                         ENV_EXAMPLE.replace("127.0.0.1", "")), "a literal IP in .env.example"
