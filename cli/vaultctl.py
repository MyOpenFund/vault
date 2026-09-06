#!/usr/bin/env python3
"""
CLI to query the vault API.

Configuration: VAULT_API_URL environment variable
(default: http://localhost:8000)

Examples:
    vaultctl list --corpus central-bank --source-code us --year 2010
    vaultctl list --q "housing bubble" --limit 10
    vaultctl get c184d44f298ff622
    vaultctl download c184d44f298ff622 -o ./downloads/
    vaultctl stats
"""

import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="CLI to query the vault corpus")
console = Console()

API_URL = os.environ.get("VAULT_API_URL", "http://localhost:8000")


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_URL, timeout=30.0)


def _die(message: str):
    console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=1)


@app.command("list")
def list_documents(
    corpus: Optional[str] = typer.Option(None, help="Filter by corpus (e.g. central-bank)"),
    source_code: Optional[str] = typer.Option(None, help="Filter by source code (e.g. us)"),
    doc_type: Optional[str] = typer.Option(None, help="Filter by document type (e.g. C1)"),
    language: Optional[str] = typer.Option(None, help="Filter by language (e.g. en)"),
    provenance: Optional[str] = typer.Option(None, help="Filter by provenance"),
    year: Optional[int] = typer.Option(None, help="Filter by year"),
    date_from: Optional[str] = typer.Option(None, help="Min date (YYYY-MM-DD)"),
    date_to: Optional[str] = typer.Option(None, help="Max date (YYYY-MM-DD)"),
    q: Optional[str] = typer.Option(None, help="Free-text search on the title"),
    sort_by: str = typer.Option("date", help="Sort field"),
    sort_dir: str = typer.Option("desc", help="asc or desc"),
    limit: int = typer.Option(20, help="Number of results"),
    offset: int = typer.Option(0, help="Offset for pagination"),
):
    """List documents with filters."""
    params = {
        "corpus": corpus,
        "source_code": source_code,
        "doc_type": doc_type,
        "language": language,
        "provenance": provenance,
        "year": year,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "limit": limit,
        "offset": offset,
    }
    params = {k: v for k, v in params.items() if v is not None}

    with _client() as client:
        try:
            resp = client.get("/documents", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            _die(str(e))

    data = resp.json()
    table = Table(title=f"{data['total']} document(s) found (showing {len(data['items'])})")
    table.add_column("doc_id", style="cyan")
    table.add_column("corpus")
    table.add_column("source")
    table.add_column("type")
    table.add_column("date")
    table.add_column("title", max_width=50)

    for item in data["items"]:
        table.add_row(
            item.get("doc_id", ""),
            item.get("corpus") or "",
            item.get("source_code") or "",
            item.get("doc_type") or "",
            str(item.get("date") or ""),
            item.get("title") or "",
        )

    console.print(table)


@app.command("get")
def get_document(doc_id: str):
    """Show the full detail of a document."""
    with _client() as client:
        try:
            resp = client.get(f"/documents/{doc_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                _die(f"Document '{doc_id}' not found")
            _die(str(e))
        except httpx.HTTPError as e:
            _die(str(e))

    doc = resp.json()
    for key, value in doc.items():
        console.print(f"[cyan]{key}[/cyan]: {value}")


def safe_filename(content_disposition, fallback):
    """The download filename, reduced to a bare basename.

    The header is a disposition type followed by `;`-separated parameters
    (RFC 6266 §4.1), so `filename` is not necessarily the last one: the value
    ends at the next `;`. Splitting the whole header on `filename=` alone
    would swallow any parameter that follows it, turning
    `attachment; filename="a.pdf"; size=3` into `a.pdf"; size=3`.

    The server's Content-Disposition is untrusted input (the default transport
    is plain HTTP), so whatever the split yields is reduced to a basename:
    `Path(name).name` strips any directory part, and neither an absolute path
    nor a "../" traversal can move the write out of the chosen output
    directory (vault #5).
    """
    if not content_disposition:
        return fallback
    raw = None
    for parameter in content_disposition.split(";"):
        key, _, value = parameter.partition("=")
        # Parameter names are case-insensitive and whitespace may surround the
        # `=` (RFC 9110 §5.6.6), so `FILENAME = "x"` names the same parameter.
        # Matching the key exactly also keeps `filename*=` (RFC 5987) out: it
        # is a different parameter with a different value grammar.
        if key.strip().lower() == "filename":
            raw = value.strip().strip('"')
            break
    if raw is None:
        return fallback
    name = Path(raw).name
    return name if name not in ("", ".", "..") else fallback


@app.command("download")
def download_document(
    doc_id: str,
    output_dir: Path = typer.Option(Path("."), "--output", "-o", help="Destination directory"),
):
    """Download the actual file associated with a document."""
    output_dir.mkdir(parents=True, exist_ok=True)

    with _client() as client:
        try:
            resp = client.get(f"/documents/{doc_id}/file")
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                _die(f"File not found for '{doc_id}' ({e.response.json().get('detail', '')})")
            _die(str(e))
        except httpx.HTTPError as e:
            _die(str(e))

    filename = safe_filename(resp.headers.get("content-disposition"), doc_id)
    dest = output_dir / filename
    # NOTE: the whole response body is buffered in memory before the write.
    # Known, and out of scope for #5 — streaming the download is its own issue.
    dest.write_bytes(resp.content)
    console.print(f"[green]Downloaded:[/green] {dest}")


@app.command("stats")
def stats():
    """Show aggregate figures across the whole corpus."""
    with _client() as client:
        try:
            resp = client.get("/stats/summary")
            resp.raise_for_status()
        except httpx.HTTPError as e:
            _die(str(e))

    data = resp.json()
    console.print(f"[bold]Total documents:[/bold] {data['total_documents']}\n")

    for section, label in [
        ("by_corpus", "By corpus"),
        ("by_source_code", "By source"),
        ("by_doc_type", "By document type"),
        ("by_language", "By language"),
        ("by_provenance", "By provenance"),
    ]:
        table = Table(title=label)
        table.add_column("Value")
        table.add_column("Count", justify="right")
        for row in data[section]:
            table.add_row(str(row["key"]), str(row["count"]))
        console.print(table)


if __name__ == "__main__":
    app()
