"""CI pre-pulls postgres:16, with retries, before the integration suite (Spec W19)."""
from pathlib import Path

WORKFLOW = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "tests.yml").read_text()


def test_postgres_is_pulled_before_the_integration_tests():
    pull = WORKFLOW.index("docker pull postgres:16")
    integration = WORKFLOW.index("-m integration")
    assert pull < integration


def test_the_pull_is_retried_and_bounded():
    step = WORKFLOW[WORKFLOW.index("- name: Pre-pull postgres:16"):WORKFLOW.index("- name: Integration tests")]
    assert 'PULL_ATTEMPTS: "3"' in step
    assert 'timeout "$PULL_TIMEOUT_SECONDS"' in step
    assert step.rstrip().splitlines()[-1].strip() == "exit 1"  # all attempts failed -> job fails
