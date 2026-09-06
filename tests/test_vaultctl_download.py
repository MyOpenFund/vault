"""`vaultctl download` writes where it was told to, whatever the server says (#5)."""
from pathlib import Path

import pytest

from vaultctl import safe_filename


@pytest.mark.parametrize("header,expected", [
    ('attachment; filename="report.pdf"', "report.pdf"),
    ('attachment; filename=report.pdf', "report.pdf"),
    # The API response is untrusted input (plain HTTP by default): pathlib
    # semantics mean an absolute name REPLACES output_dir entirely and a
    # "../" name escapes it (#5).
    ('attachment; filename="/etc/cron.d/evil"', "evil"),
    ('attachment; filename="../../../evil.pdf"', "evil.pdf"),
    ('attachment; filename="dir/evil.pdf"', "evil.pdf"),
    ('attachment; filename=".."', "doc123"),
    ('attachment; filename=""', "doc123"),
    ('attachment', "doc123"),
    (None, "doc123"),
    # A parameter AFTER filename= is legal in the grammar (RFC 6266 §4.1), so
    # the value ends at the next `;`, not at the end of the header. Splitting
    # from the right first would have produced `a.pdf"; size=3`.
    ('attachment; filename="a.pdf"; size=3', "a.pdf"),
    ('attachment; size=3; filename="a.pdf"', "a.pdf"),
    ('attachment; filename=a.pdf; size=3', "a.pdf"),
    # ... and the traversal defence still applies to the trimmed value.
    ('attachment; filename="../../etc/passwd"; size=3', "passwd"),
    ('attachment; filename=".."; size=3', "doc123"),
    ('attachment; filename=""; size=3', "doc123"),
    ('attachment; filename=; size=3', "doc123"),
    # Parameter names are case-insensitive and whitespace around `=` is legal
    # (RFC 9110 §5.6.6), so a server that spells it differently must still get
    # a basename rather than silently falling back to the document id.
    ('attachment; FILENAME="report.pdf"', "report.pdf"),
    ('attachment; Filename="report.pdf"', "report.pdf"),
    ('attachment; filename = "report.pdf"', "report.pdf"),
    ('attachment; FILENAME = "../../etc/passwd"', "passwd"),
    # ... but `filename*=` (RFC 5987) is a DIFFERENT parameter, not this one.
    ('attachment; filename*=UTF-8\'\'r.pdf', "doc123"),
])
def test_safe_filename_is_always_a_bare_basename(header, expected):
    assert safe_filename(header, "doc123") == expected


def test_safe_filename_result_cannot_escape_the_output_directory(tmp_path):
    for header in ('attachment; filename="/etc/passwd"',
                   'attachment; filename="../../x"'):
        dest = tmp_path / safe_filename(header, "doc123")
        assert Path(dest).resolve().parent == tmp_path.resolve()
