"""`docker compose config` over the committed `.env.example` (issue #5).

The unit test next door pins the literal text of `compose.yaml`; this one pins
what Docker actually makes of it. It is the only place that proves the three
things a text assertion cannot: that the file still parses after the healthcheck
and `DATABASE_URL` edits, that the repo as committed publishes nothing on the
host, and — by uncommenting them into a scratch copy — that the two commented
`ports:` lines an operator is told to enable are valid YAML in the right place
and resolve to the loopback rather than to every interface.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE = ROOT / "compose.yaml"


def compose_available():
    try:
        subprocess.run(["docker", "compose", "version"],
                       capture_output=True, check=True, timeout=30)
        return True
    except Exception:
        return False


def render(compose_path):
    """`docker compose config` as parsed JSON, or a failing assertion."""
    out = subprocess.run(
        ["docker", "compose", "--project-directory", str(ROOT),
         "--env-file", str(ROOT / ".env.example"),
         "-f", str(compose_path), "config", "--format", "json"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, f"compose config failed:\n{out.stderr}"
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def as_committed():
    if not compose_available():
        pytest.skip("docker compose unavailable")
    return render(COMPOSE)


@pytest.fixture(scope="module")
def with_the_port_published(tmp_path_factory):
    """The same file with the commented `ports:` block enabled.

    This is what the NAS runs. Uncommenting it here is what keeps those two
    lines honest: a comment is never parsed, so a typo in them would otherwise
    only surface on the deployment, at the worst possible moment.
    """
    if not compose_available():
        pytest.skip("docker compose unavailable")
    text = COMPOSE.read_text()
    enabled = re.sub(r"^(\s*)#(ports:|  - \"\$\{POSTGRES_BIND_ADDR)",
                     r"\1\2", text, flags=re.M)
    assert "\n    ports:\n" in enabled, "the commented ports: block was not found"
    scratch = tmp_path_factory.mktemp("compose") / "compose.yaml"
    scratch.write_text(enabled)
    return render(scratch)


def published(rendered, service):
    return [(p.get("host_ip"), str(p.get("published")), str(p.get("target")))
            for p in rendered["services"][service].get("ports", [])]


def test_the_committed_compose_publishes_no_database_port(as_committed):
    assert published(as_committed, "postgres") == []


def test_uncommenting_the_block_publishes_on_the_loopback_by_default(
    with_the_port_published
):
    # No POSTGRES_BIND_ADDR in .env.example, so the ${...:-127.0.0.1} default
    # is what an operator who forgets to set it gets: host-local, not the world.
    assert published(with_the_port_published, "postgres") == [
        ("127.0.0.1", "5432", "5432")]


@pytest.mark.parametrize("variant", ["as_committed", "with_the_port_published"])
def test_postgres_is_never_published_on_every_interface(variant, request):
    # Scoped to postgres deliberately. The api (8000) and metabase (3000) are
    # published on every interface by design — they are the vault's UIs, and
    # they are not a superuser-capable SQL port.
    for port in request.getfixturevalue(variant)["services"]["postgres"].get("ports", []):
        assert port.get("host_ip") not in ("0.0.0.0", "", None), \
            f"postgres publishes on every interface: {port}"


def test_the_api_connects_as_the_readonly_role(as_committed):
    url = as_committed["services"]["api"]["environment"]["DATABASE_URL"]
    assert url.startswith("postgresql://vault_readonly:"), url
    assert "docuser" not in url


def test_the_ingestion_service_carries_both_role_passwords(as_committed):
    env = as_committed["services"]["ingestion"]["environment"]
    assert env["VAULT_ORCHESTRATOR_PASSWORD"]
    assert env["VAULT_READONLY_PASSWORD"]
