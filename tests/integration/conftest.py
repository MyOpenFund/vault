import json
import subprocess
import time
import uuid

import pytest

import psycopg2


def docker_available():
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, check=True, timeout=10
        )
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def pg_url():
    if not docker_available():
        pytest.skip("docker unavailable")

    name = f"vault-it-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "-e", "POSTGRES_PASSWORD=it",
                "-e", "POSTGRES_USER=docuser",
                "-e", "POSTGRES_DB=documents",
                "-p", "127.0.0.1:0:5432",
                "postgres:16",
            ],
            check=True, capture_output=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip("docker run timed out — image pull may be hung; pre-pull postgres:16")
    try:
        url = None
        for _ in range(60):
            out = subprocess.run(
                ["docker", "port", name, "5432/tcp"],
                capture_output=True, text=True,
            )
            if out.returncode == 0 and out.stdout.strip():
                port = out.stdout.strip().splitlines()[0].rsplit(":", 1)[1]
                candidate = f"postgresql://docuser:it@127.0.0.1:{port}/documents"
                try:
                    psycopg2.connect(candidate).close()
                    url = candidate
                    break
                except Exception:
                    pass
            time.sleep(1)
        if url is None:
            raise RuntimeError("postgres container did not become ready")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture()
def clean_db(pg_url):
    """Drop the documents table between tests so each starts fresh."""
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS rag_ingestions")
        cur.execute("DROP TABLE IF EXISTS documents")
    conn.close()
    return pg_url


def write_manifest(directory, name, docs):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(d) for d in docs) + "\n", encoding="utf-8"
    )
    return path


def make_doc(doc_id, source="us", **overrides):
    doc = {
        "doc_id": doc_id,
        "bank_code": source,
        "doc_type": "C1",
        "title": f"doc {doc_id}",
        "date": "2010-01-13",
        "year": 2010,
        "language": "en",
        "provenance": "bis_index",
        "local_path": f"data/raw/{source}/C1/2010/{doc_id}.pdf",
    }
    doc.update(overrides)
    return doc


def make_entry(**overrides):
    obj = {
        "bank_code": "us",
        "doc_type": "C1",
        "last": "2026-08-01",
        "interval_days": 14,
        "next_expected": "2026-08-15",
        "days_until": -12,
        "status": "overdue",
        "expected_per_year": 26,
        "n_3y": 78,
    }
    obj.update(overrides)
    return obj


def write_cadence(directory, entries):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cadence.jsonl"
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )
    return path


def run_ingest(monkeypatch, pg_url, data_dir, corpus="central-bank", sweep="1.0"):
    import ingest

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CORPUS", corpus)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", sweep)
    ingest.main()


def fetch_all(pg_url, query):
    conn = psycopg2.connect(pg_url)
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    conn.close()
    return rows
