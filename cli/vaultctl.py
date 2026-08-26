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

    filename = doc_id
    if "content-disposition" in resp.headers:
        filename = resp.headers["content-disposition"].split("filename=")[-1].strip('"')

    dest = output_dir / filename
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
