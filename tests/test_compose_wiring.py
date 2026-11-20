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


def service_block(name):
    """The text of one service in compose.yaml: from `  <name>:` to the next
    line indented by two spaces (the next service) or by none."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S)", COMPOSE, re.M | re.S)
    assert match, f"no service {name!r} in compose.yaml"
    return match.group(1)


def test_postgres_is_published_on_one_interface_never_on_all_of_them():
    assert '"${POSTGRES_BIND_ADDR:-127.0.0.1}:${POSTGRES_BIND_PORT:-5432}:5432"' in COMPOSE
    assert "0.0.0.0" not in COMPOSE and "0.0.0.0" not in ENV_EXAMPLE
    # a bind-address-less mapping publishes on every interface, firewall included
    assert not re.search(r'^\s*-\s*"?\d*:?5432:5432"?\s*$', COMPOSE, re.M)


def test_only_the_ingestion_services_still_use_the_superuser():
    # The two ingestion services (one per corpus) run the DDL train.
    assert COMPOSE.count("postgresql://docuser:") == 2
    assert "postgresql://docuser:" in service_block("ingestion")
    assert "postgresql://docuser:" in service_block("ingestion-company")
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


def test_ingestion_forwards_the_ddl_gate_settings_as_optional():
    # ${VAR:-}: unset in .env means empty, and schema_gate reads empty as its
    # default — the defaults live in one place, the code.
    for var in ("VAULT_DDL_LOCK_TIMEOUT", "VAULT_DDL_RETRY_DELAYS", "VAULT_DDL_FORCE"):
        assert f"{var}: ${{{var}:-}}" in COMPOSE
        assert f"#{var}=" in ENV_EXAMPLE


def test_ingestion_forwards_manifest_globs_as_optional():
    # Empty = layout detection (ingest.find_manifest_files); the NAS sets nothing.
    assert "MANIFEST_GLOBS: ${MANIFEST_GLOBS:-}" in COMPOSE
    assert "#MANIFEST_GLOBS=" in ENV_EXAMPLE


def test_ingestion_forwards_the_vocabulary_path_as_optional():
    # Empty = <DATA_DIR>/vocabulary.json; the NAS (manifest-dir layout) sets it.
    assert "VOCABULARY_PATH: ${VOCABULARY_PATH:-}" in COMPOSE
    assert "#VOCABULARY_PATH=" in ENV_EXAMPLE


# --- documents contract (chantier 4): CORPUS_ROOTS and the company ingestion ----

def test_the_api_serves_files_from_corpus_roots():
    api = service_block("api")
    # Default: the central-bank root only; a deployment adds company=... with
    # its mount (the company files are not on the NAS yet).
    assert "CORPUS_ROOTS: ${CORPUS_ROOTS:-central-bank=/cbdata}" in api
    assert "- ${CENTRAL_BANK_DATA_DIR:?set it in .env — see .env.example}:/cbdata:ro" in api
    assert "#CORPUS_ROOTS=" in ENV_EXAMPLE
    assert "\nCENTRAL_BANK_DATA_DIR=" in ENV_EXAMPLE


def test_the_raw_path_variables_are_gone():
    for gone in ("RAW_DATA_DIR", "${RAW_DIR", "/data/raw"):
        assert gone not in COMPOSE, gone
    assert not re.search(r"^#?RAW(_DATA)?_DIR=", ENV_EXAMPLE, re.M)


def test_the_company_ingestion_service():
    block = service_block("ingestion-company")
    # Same image as the central-bank ingestion.
    assert "dockerfile: ingestion/Dockerfile" in block
    assert "context: ." in block
    assert "container_name: vault_ingestion_company" in block
    assert "CORPUS: company" in block
    assert "DATA_DIR: /data" in block
    assert "MANIFEST_GLOBS: manifest/*.jsonl" in block
    assert "VOCABULARY_PATH: /data/vocabulary.json" in block
    assert "- ${COMPANY_MANIFEST_DIR:?set it in .env — see .env.example}:/data:ro" in block
    assert 'restart: "no"' in block
    # Not started by `docker compose up`: run on demand, like `ingestion`.
    assert 'profiles: ["company"]' in block
    # It may be the run that applies the DDL train: same roles and gate settings.
    for var in ("VAULT_ORCHESTRATOR_PASSWORD", "VAULT_READONLY_PASSWORD",
                "VAULT_DDL_LOCK_TIMEOUT", "VAULT_DDL_RETRY_DELAYS", "VAULT_DDL_FORCE",
                "SWEEP_MAX_DELETE_FRACTION"):
        assert f"{var}: ${{{var}:-" in block, var
    # The central-bank producer-file overrides are not forwarded: they name
    # central-bank files.
    for var in ("CADENCE_PATH", "RUNS_PATH", "DISCOVERY_ERRORS_PATH"):
        assert f"{var}:" not in block, var
    assert "${MANIFEST_DIR" not in block and "${MANIFEST_GLOBS" not in block
    assert "\nCOMPANY_MANIFEST_DIR=" in ENV_EXAMPLE


def test_the_repo_holds_no_host_path_for_the_company_corpus():
    assert "/Users/" not in COMPOSE and "/Users/" not in ENV_EXAMPLE
    assert "/volume1" not in COMPOSE and "/volume1" not in ENV_EXAMPLE
