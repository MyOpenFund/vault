"""`vaultctl` end to end against a fake API (Spec W20).

Every command goes through typer's CliRunner, and `_client()` is patched to an
httpx.Client whose transport is an httpx.MockTransport: the real request
building, status handling and printing run; only the network is replaced.
"""
import httpx
import pytest
from typer.testing import CliRunner

import vaultctl

runner = CliRunner()


class FakeApi:
    """Routes one request to a canned response and records what was asked."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def api(monkeypatch):
    """Install a handler: `api(lambda request: httpx.Response(...))`."""
    installed = {}

    def install(handler):
        fake = FakeApi(handler)
        installed["fake"] = fake
        monkeypatch.setattr(
            vaultctl, "_client",
            lambda: httpx.Client(base_url="http://vault.test",
                                 transport=httpx.MockTransport(fake)),
        )
        return fake

    return install


def invoke(*args):
    return runner.invoke(vaultctl.app, list(args))


def _doc(doc_id, **overrides):
    doc = {"id": 1, "doc_id": doc_id, "corpus": "central-bank", "source_code": "us",
           "doc_type": "C1", "date": "2010-01-13", "title": f"title {doc_id}"}
    doc.update(overrides)
    return doc


def _page(items, total=None):
    return {"total": len(items) if total is None else total, "limit": 20, "offset": 0,
            "items": items}


# --- list ------------------------------------------------------------------

def test_list_forwards_only_the_given_filters(api):
    fake = api(lambda r: httpx.Response(200, json=_page([])))
    result = invoke("list", "--corpus", "central-bank", "--source-code", "us",
                    "--doc-type", "C1", "--year", "2010", "--q", "50% growth",
                    "--limit", "5", "--offset", "10")
    assert result.exit_code == 0, result.output
    (request,) = fake.requests
    assert request.method == "GET" and request.url.path == "/documents"
    assert dict(request.url.params) == {
        "corpus": "central-bank", "source_code": "us", "doc_type": "C1", "year": "2010",
        "q": "50% growth", "sort_by": "date", "sort_dir": "desc", "limit": "5",
        "offset": "10",
    }


def test_list_defaults_send_no_filter(api):
    fake = api(lambda r: httpx.Response(200, json=_page([])))
    assert invoke("list").exit_code == 0
    assert dict(fake.requests[0].url.params) == {
        "sort_by": "date", "sort_dir": "desc", "limit": "20", "offset": "0"}


def test_list_prints_a_table_with_the_total(api):
    api(lambda r: httpx.Response(200, json=_page([_doc("aa11"), _doc("bb22")], total=42)))
    result = invoke("list")
    assert result.exit_code == 0, result.output
    assert "42 document(s) found (showing 2)" in result.output
    assert "aa11" in result.output and "bb22" in result.output


def test_list_with_no_result(api):
    api(lambda r: httpx.Response(200, json=_page([])))
    result = invoke("list", "--q", "nothing")
    assert result.exit_code == 0
    assert "0 document(s) found (showing 0)" in result.output


def test_list_tolerates_null_columns(api):
    sparse = {"id": 1, "doc_id": "cc33", "corpus": None, "source_code": None,
              "doc_type": None, "date": None, "title": None}
    api(lambda r: httpx.Response(200, json=_page([sparse])))
    result = invoke("list")
    assert result.exit_code == 0, result.output
    assert "cc33" in result.output


def test_list_prints_titles_literally_not_as_markup(api):
    # Titles come from manifests: square brackets are data, not rich markup.
    # An unbalanced closing tag used to raise MarkupError mid-table.
    api(lambda r: httpx.Response(200, json=_page([
        _doc("dd44", title="[/] x"), _doc("ee55", title="[bold]B[/bold]")])))
    result = invoke("list")
    assert result.exit_code == 0, result.output
    assert "[/] x" in result.output
    assert "[bold]B[/bold]" in result.output


@pytest.mark.parametrize("status", [400, 422, 500, 503])
def test_list_error_status_exits_non_zero(api, status):
    api(lambda r: httpx.Response(status, json={"detail": "nope"}))
    result = invoke("list")
    assert result.exit_code == 1
    assert "Error:" in result.output and str(status) in result.output


def test_list_unreachable_api_exits_non_zero(api):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    api(refuse)
    result = invoke("list")
    assert result.exit_code == 1
    assert "connection refused" in result.output


# --- get -------------------------------------------------------------------

def test_get_prints_every_field(api):
    fake = api(lambda r: httpx.Response(200, json=_doc("ff66", page_count=7)))
    result = invoke("get", "ff66")
    assert result.exit_code == 0, result.output
    assert fake.requests[0].url.path == "/documents/ff66"
    assert "doc_id: ff66" in result.output
    assert "page_count: 7" in result.output


def test_get_prints_values_literally_not_as_markup(api):
    api(lambda r: httpx.Response(200, json=_doc("gg77", title="[/red] odd [link]")))
    result = invoke("get", "gg77")
    assert result.exit_code == 0, result.output
    assert "title: [/red] odd [link]" in result.output


def test_get_unknown_doc_id(api):
    api(lambda r: httpx.Response(404, json={"detail": "Document x not found"}))
    result = invoke("get", "nope")
    assert result.exit_code == 1
    assert "Document 'nope' not found" in result.output


def test_get_server_error(api):
    api(lambda r: httpx.Response(500, text="boom"))
    result = invoke("get", "hh88")
    assert result.exit_code == 1
    assert "500" in result.output


def test_get_timeout(api):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    api(slow)
    result = invoke("get", "ii99")
    assert result.exit_code == 1
    assert "timed out" in result.output


# --- stats -----------------------------------------------------------------

STATS = {
    "total_documents": 1234,
    "by_corpus": [{"key": "central-bank", "count": 1234}],
    "by_source_code": [{"key": "us", "count": 1000}, {"key": None, "count": 234}],
    "by_doc_type": [],
    "by_language": [{"key": "en", "count": 1234}],
    "by_year": [{"key": 2010, "count": 1234}],
    "by_provenance": [{"key": "bis_index", "count": 1234}],
}


def test_stats_prints_the_total_and_each_section(api):
    fake = api(lambda r: httpx.Response(200, json=STATS))
    result = invoke("stats")
    assert result.exit_code == 0, result.output
    assert fake.requests[0].url.path == "/stats/summary"
    assert "Total documents: 1234" in result.output
    for label in ("By corpus", "By source", "By document type", "By language",
                  "By provenance"):
        assert label in result.output
    assert "None" in result.output  # a NULL key is shown, not dropped


def test_stats_error_status(api):
    api(lambda r: httpx.Response(502, text="bad gateway"))
    result = invoke("stats")
    assert result.exit_code == 1
    assert "502" in result.output


# --- download --------------------------------------------------------------

PDF = b"%PDF-1.4 \x00\xff binary\n"


def _file(headers=None, body=PDF):
    return lambda r: httpx.Response(200, content=body, headers=headers or {})


def test_download_writes_the_bytes_under_the_output_dir(api, tmp_path):
    fake = api(_file({"content-disposition": 'attachment; filename="report.pdf"'}))
    out = tmp_path / "nested" / "out"   # created on demand
    result = invoke("download", "jj00", "--output", str(out))
    assert result.exit_code == 0, result.output
    assert fake.requests[0].url.path == "/documents/jj00/file"
    assert (out / "report.pdf").read_bytes() == PDF
    assert list(out.iterdir()) == [out / "report.pdf"]


def test_download_short_option(api, tmp_path):
    api(_file({"content-disposition": 'attachment; filename="r.pdf"'}))
    assert invoke("download", "kk11", "-o", str(tmp_path)).exit_code == 0
    assert (tmp_path / "r.pdf").read_bytes() == PDF


@pytest.mark.parametrize("header", [
    'attachment; filename="../../escape.pdf"',
    'attachment; filename="/etc/escape.pdf"',
])
def test_download_reduces_an_unsafe_name_to_its_basename(api, tmp_path, header):
    api(_file({"content-disposition": header}))
    out = tmp_path / "out"
    result = invoke("download", "ll22", "--output", str(out))
    assert result.exit_code == 0, result.output
    assert (out / "escape.pdf").read_bytes() == PDF
    assert not (tmp_path / "escape.pdf").exists()


def test_download_without_a_disposition_uses_the_doc_id(api, tmp_path):
    api(_file())
    assert invoke("download", "mm33", "-o", str(tmp_path)).exit_code == 0
    assert (tmp_path / "mm33").read_bytes() == PDF


def test_download_empty_body_writes_an_empty_file(api, tmp_path):
    api(_file({"content-disposition": 'attachment; filename="e.pdf"'}, body=b""))
    assert invoke("download", "nn44", "-o", str(tmp_path)).exit_code == 0
    assert (tmp_path / "e.pdf").read_bytes() == b""


def test_download_404_with_a_json_detail(api, tmp_path):
    api(lambda r: httpx.Response(404, json={"detail": "file missing on disk"}))
    result = invoke("download", "oo55", "-o", str(tmp_path))
    assert result.exit_code == 1
    assert "File not found for 'oo55'" in result.output
    assert "file missing on disk" in result.output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("body", ["<html>Not Found</html>", "", "[1, 2]", '"just a string"'])
def test_download_404_without_a_json_object_body_still_dies_cleanly(api, tmp_path, body):
    # A proxy in front of the API can answer 404 with HTML; the CLI must
    # still print its error, not a JSONDecodeError traceback.
    api(lambda r: httpx.Response(404, text=body))
    result = invoke("download", "pp66", "-o", str(tmp_path))
    assert result.exit_code == 1
    assert "File not found for 'pp66'" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [403, 500])
def test_download_error_status_writes_nothing(api, tmp_path, status):
    api(lambda r: httpx.Response(status, content=b"partial-bytes"))
    result = invoke("download", "qq77", "-o", str(tmp_path))
    assert result.exit_code == 1
    assert str(status) in result.output
    assert list(tmp_path.iterdir()) == []


def test_download_transport_error_writes_nothing(api, tmp_path):
    def drop(request):
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    api(drop)
    result = invoke("download", "rr88", "-o", str(tmp_path))
    assert result.exit_code == 1
    assert "peer closed connection" in result.output
    assert list(tmp_path.iterdir()) == []


# --- configuration ---------------------------------------------------------

def test_client_targets_the_configured_api_url(monkeypatch):
    monkeypatch.setattr(vaultctl, "API_URL", "http://vault.example:8123")
    with vaultctl._client() as client:
        assert str(client.base_url) == "http://vault.example:8123"



def test_error_text_is_printed_literally(api):
    def odd(request):
        raise httpx.ConnectError("[/] [errno 111] refused", request=request)

    api(odd)
    result = invoke("stats")
    assert result.exit_code == 1
    assert "[/] [errno 111] refused" in result.output


@pytest.mark.parametrize("dirname", ["[bold]out", "x[/]y", "[/red]"])
def test_download_path_is_printed_literally(api, tmp_path, dirname):
    # The output directory is the user's own text, but still not markup: a
    # "[/]" in it used to crash the success line after the file was written.
    api(_file({"content-disposition": 'attachment; filename="r.pdf"'}))
    out = tmp_path / dirname
    result = invoke("download", "ss99", "-o", str(out))
    assert result.exit_code == 0, result.output
    assert (out / "r.pdf").read_bytes() == PDF
    assert str(out / "r.pdf") in result.output.replace("\n", "")
