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
