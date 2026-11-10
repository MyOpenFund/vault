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


@pytest.mark.parametrize("value", ["", "-0.5", "nan", "banana"])
def test_main_exits_before_connecting_on_a_bad_fraction(monkeypatch, tmp_path, value):
    # Empty must NOT crash any more (it used to raise ValueError); the bad
    # ones exit 1 before any connection is attempted.
    def no_connect(*_a, **_k):
        raise AssertionError("connected despite an invalid setting")

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never-dialed")
    monkeypatch.setenv("SWEEP_MAX_DELETE_FRACTION", value)
    monkeypatch.setattr(ingest.psycopg2, "connect", no_connect)
    if value == "":
        with pytest.raises(AssertionError, match="connected"):
            ingest.main(corpus="central-bank", data_dir=str(tmp_path))
        return
    with pytest.raises(SystemExit) as exc:
        ingest.main(corpus="central-bank", data_dir=str(tmp_path))
    assert exc.value.code == 1


def test_main_exits_before_connecting_on_bad_manifest_globs(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/never-dialed")
    monkeypatch.setenv("MANIFEST_GLOBS", "../escape/*.jsonl")
    monkeypatch.setattr(ingest.psycopg2, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    with pytest.raises(SystemExit) as exc:
        ingest.main(corpus="central-bank", data_dir=str(tmp_path))
    assert exc.value.code == 1
