from ingest import should_sweep


def test_no_candidates_means_no_sweep():
    assert should_sweep(candidates=0, live=1000, max_fraction=0.05) is False


def test_small_fraction_sweeps():
    assert should_sweep(candidates=10, live=1000, max_fraction=0.05) is True


def test_fraction_above_threshold_is_blocked():
    # 200/1000 = 20% of live rows vanished at once: that is a torn share
    # sync, not a legitimate deletion wave.
    assert should_sweep(candidates=200, live=1000, max_fraction=0.05) is False


def test_fraction_exactly_at_threshold_sweeps():
    assert should_sweep(candidates=50, live=1000, max_fraction=0.05) is True


def test_everything_vanished_is_blocked():
    assert should_sweep(candidates=1000, live=1000, max_fraction=0.05) is False


def test_fraction_one_disables_the_guard():
    # Operator override: max_fraction=1.0 always sweeps (candidates > 0).
    assert should_sweep(candidates=1000, live=1000, max_fraction=1.0) is True


def test_empty_table_never_sweeps():
    assert should_sweep(candidates=0, live=0, max_fraction=0.05) is False


# --- SWEEP_MAX_DELETE_FRACTION parsing (vault #6 / F12, Spec W17) ---------

import logging  # noqa: E402

import pytest  # noqa: E402

import ingest  # noqa: E402
from ingest import (  # noqa: E402
    SWEEP_MAX_DELETE_FRACTION_DEFAULT, IngestSettingsError, parse_sweep_fraction,
)


@pytest.mark.parametrize("env", [{}, {"SWEEP_MAX_DELETE_FRACTION": ""},
                                 {"SWEEP_MAX_DELETE_FRACTION": "  "}])
def test_unset_or_empty_fraction_is_the_default_with_an_info(env, caplog):
    with caplog.at_level(logging.INFO, logger="ingest"):
        assert parse_sweep_fraction(env) == SWEEP_MAX_DELETE_FRACTION_DEFAULT == 0.05
    assert any("SWEEP_MAX_DELETE_FRACTION" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)


@pytest.mark.parametrize("value, expected", [
    ("0", 0.0), ("0.05", 0.05), (" 0.2 ", 0.2), ("1", 1.0), ("1.0", 1.0), ("5e-2", 0.05),
])
def test_valid_fractions(value, expected):
    assert parse_sweep_fraction({"SWEEP_MAX_DELETE_FRACTION": value}) == expected


@pytest.mark.parametrize("value", [
    "abc", "5%", "0,05", "-0.01", "-1", "1.01", "2", "nan", "NaN", "inf", "-inf", "0.05x",
])
def test_invalid_fractions_name_the_variable_and_the_value(value):
    with pytest.raises(IngestSettingsError) as exc:
        parse_sweep_fraction({"SWEEP_MAX_DELETE_FRACTION": value})
    assert "SWEEP_MAX_DELETE_FRACTION" in str(exc.value)
    assert repr(value) in str(exc.value)


class _Connected(Exception):
    """Raised by the patched connect(): proves the run got past the settings."""


def _forbid_connect(monkeypatch):
    def connect(*_a, **_k):
        raise _Connected()

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never-dialed")
    monkeypatch.setattr(ingest.psycopg2, "connect", connect)


@pytest.mark.parametrize("value", ["-0.5", "nan", "banana", "0_1"])
def test_main_exits_before_connecting_on_a_bad_fraction(monkeypatch, tmp_path, caplog, value):
    _forbid_connect(monkeypatch)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", value)
    with caplog.at_level(logging.ERROR, logger="ingest"), pytest.raises(SystemExit) as exc:
        ingest.main(corpus="central-bank", data_dir=str(tmp_path))
    assert exc.value.code == 1
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert f"SWEEP_MAX_DELETE_FRACTION={value!r}" in record.getMessage()


def test_main_with_an_empty_fraction_uses_the_default_and_proceeds(monkeypatch, tmp_path, caplog):
    # `SWEEP_MAX_DELETE_FRACTION=` in .env used to crash with ValueError
    # before anything ran; now the settings pass and the run goes on to
    # connect (where this test stops it).
    _forbid_connect(monkeypatch)
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", "")
    with caplog.at_level(logging.INFO, logger="ingest"), pytest.raises(_Connected):
        ingest.main(corpus="central-bank", data_dir=str(tmp_path))
    assert any(r.levelno == logging.INFO and "using the default 0.05" in r.getMessage()
               for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_main_exits_before_connecting_on_bad_manifest_globs(monkeypatch, tmp_path):
    _forbid_connect(monkeypatch)
    monkeypatch.setenv("MANIFEST_GLOBS", "../escape/*.jsonl")
    with pytest.raises(SystemExit) as exc:
        ingest.main(corpus="central-bank", data_dir=str(tmp_path))
    assert exc.value.code == 1


# --- strict ASCII grammar (review M3) ---------------------------------------
# Python's float() also takes "0_1", non-ASCII digits, "infinity" and lets
# "1e-400" underflow to 0.0 (= "never sweep") without a word. The variable's
# grammar is plain ASCII decimal, optionally with an exponent.

@pytest.mark.parametrize("value, expected", [
    ("1e-1", 0.1), ("+1", 1.0), ("+0.5", 0.5), (".5", 0.5), ("1.", 1.0),
    ("0e5", 0.0), ("0.000", 0.0), ("5E-2", 0.05), ("\t0.3\n", 0.3),
])
def test_ascii_grammar_accepts(value, expected):
    assert parse_sweep_fraction({"SWEEP_MAX_DELETE_FRACTION": value}) == expected


@pytest.mark.parametrize("value", [
    "0_1", "0.0_5", "٠.٥", "０.５", "-0", "-0.0", "1e-400", "0.1e-400", "1e400",
    "e5", ".", "+", "1e", "0x1", "infinity", " 0.2", "0.2 0.3", "1/2",
])
def test_ascii_grammar_refuses(value):
    with pytest.raises(IngestSettingsError) as exc:
        parse_sweep_fraction({"SWEEP_MAX_DELETE_FRACTION": value})
    assert repr(value) in str(exc.value)


def test_blocked_message_keeps_small_fractions_readable():
    from ingest import sweep_blocked_message
    msg = sweep_blocked_message(candidates=3, live=1000, max_fraction=0.002)
    assert "3/1000" in msg and "0.30%" in msg
    assert "SWEEP_MAX_DELETE_FRACTION=0.002" in msg  # was rendered as "0%"
