from ingest import resolve_corpus


def test_absent_manifest_field_uses_default():
    assert resolve_corpus(None, "central-bank") == "central-bank"


def test_matching_manifest_field_is_accepted():
    assert resolve_corpus("central-bank", "central-bank") == "central-bank"


def test_contradicting_manifest_field_is_rejected():
    assert resolve_corpus("company", "central-bank") is None
