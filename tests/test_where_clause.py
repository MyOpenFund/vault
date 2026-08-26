from db import build_where_clause


def base_filters(**overrides):
    filters = {
        "corpus": None,
        "source_code": None,
        "doc_type": None,
        "language": None,
        "provenance": None,
        "year": None,
        "mime_type": None,
        "date_from": None,
        "date_to": None,
        "q": None,
    }
    filters.update(overrides)
    return filters


def test_default_excludes_soft_deleted():
    where_sql, params = build_where_clause(base_filters())
    assert "deleted_at IS NULL" in where_sql
    assert params == []


def test_include_deleted_disables_the_filter():
    where_sql, params = build_where_clause(base_filters(), include_deleted=True)
    assert "deleted_at" not in where_sql
    assert where_sql == ""
    assert params == []


def test_exact_filters_are_parameterized():
    where_sql, params = build_where_clause(
        base_filters(corpus="central-bank", source_code="us", year=2010)
    )
    assert "corpus = %s" in where_sql
    assert "source_code = %s" in where_sql
    assert "year = %s" in where_sql
    assert set(params) == {"central-bank", "us", 2010}


def test_date_bounds():
    where_sql, params = build_where_clause(
        base_filters(date_from="2010-01-01", date_to="2010-12-31")
    )
    assert "date >= %s" in where_sql
    assert "date <= %s" in where_sql
    assert params == ["2010-01-01", "2010-12-31"]


def test_free_text_search_uses_ilike():
    where_sql, params = build_where_clause(base_filters(q="housing"))
    assert "title ILIKE %s" in where_sql
    assert params == ["%housing%"]
