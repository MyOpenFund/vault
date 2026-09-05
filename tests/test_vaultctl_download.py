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
])
def test_safe_filename_is_always_a_bare_basename(header, expected):
    assert safe_filename(header, "doc123") == expected


def test_safe_filename_result_cannot_escape_the_output_directory(tmp_path):
    for header in ('attachment; filename="/etc/passwd"',
                   'attachment; filename="../../x"'):
        dest = tmp_path / safe_filename(header, "doc123")
        assert Path(dest).resolve().parent == tmp_path.resolve()
