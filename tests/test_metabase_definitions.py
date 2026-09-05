import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "metabase"
CARDS = json.loads((ROOT / "cards.json").read_text())
DASHBOARDS = json.loads((ROOT / "dashboards.json").read_text())
TAG_RE = re.compile(r"\{\{(\w+)\}\}")
ALLOWED_TYPES = {"text", "number", "date"}
GRID_WIDTH = 24


def test_eighteen_unique_cards():
    codes = [c["code"] for c in CARDS]
    assert len(codes) == 18 and len(set(codes)) == 18
    assert all(c["name"].startswith(c["code"] + " · ") for c in CARDS)


@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_every_tag_in_sql_is_declared(card):
    used = set(TAG_RE.findall(card["sql"]))
    declared = set(card["tags"])
    assert used == declared, f"{card['code']}: used {used} declared {declared}"
    for tag, spec in card["tags"].items():
        assert spec["type"] in ALLOWED_TYPES
        assert isinstance(spec["required"], bool)
        if spec["required"]:
            assert spec["default"] is not None, f"{card['code']}.{tag}: required without default"


@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_documents_cards_filter_soft_deletes(card):
    if re.search(r"\bFROM documents\b", card["sql"]):
        assert "deleted_at IS NULL" in card["sql"], card["code"]


def test_dashboards_reference_existing_cards_without_overlap():
    by_code = {c["code"]: c for c in CARDS}
    assert len(DASHBOARDS) == 3
    for d in DASHBOARDS:
        slugs = {f["slug"] for f in d["filters"]}
        cells = set()
        for dc in d["cards"]:
            card = by_code[dc["code"]]
            assert 0 <= dc["col"] and dc["col"] + dc["size_x"] <= GRID_WIDTH, dc
            for r in range(dc["row"], dc["row"] + dc["size_y"]):
                for c in range(dc["col"], dc["col"] + dc["size_x"]):
                    assert (r, c) not in cells, f"{d['name']}: overlap at {(r, c)} for {dc['code']}"
                    cells.add((r, c))
            for slug, tag in dc["mappings"].items():
                assert slug in slugs, f"{d['name']}/{dc['code']}: unknown filter {slug}"
                assert tag in card["tags"], f"{d['name']}/{dc['code']}: card has no tag {tag}"


def test_every_card_is_on_exactly_one_dashboard():
    placed = [dc["code"] for d in DASHBOARDS for dc in d["cards"]]
    assert sorted(placed) == sorted(c["code"] for c in CARDS)


# --- Guards beyond the plan's template ------------------------------------
# The plan's schema is the contract apply.py will read; pin the shape so a
# hand edit that drops a key fails here rather than at HTTP time.

CARD_KEYS = {"code", "name", "description", "display", "visualization_settings",
             "sql", "tags"}
ALLOWED_DISPLAYS = {"scalar", "table", "bar", "row", "line", "combo", "pivot"}
FILTER_TYPE_BY_TAG_TYPE = {"text": "string/=", "number": "number/=", "date": "date/single"}


@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_card_shape(card):
    assert set(card) == CARD_KEYS, card["code"]
    assert card["display"] in ALLOWED_DISPLAYS, card["code"]
    assert isinstance(card["visualization_settings"], dict)
    assert card["description"].strip(), card["code"]
    assert card["sql"].strip(), card["code"]
    for tag, spec in card["tags"].items():
        assert set(spec) == {"type", "required", "default", "display_name"}, tag
        assert spec["display_name"].strip()


@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_optional_blocks_only_wrap_optional_tags(card):
    """A `[[ ]]` block exists to disappear; a required tag inside one can
    never disappear, so the bracket would be a lie."""
    for block in re.findall(r"\[\[(.*?)\]\]", card["sql"], flags=re.S):
        for tag in TAG_RE.findall(block):
            assert not card["tags"][tag]["required"], f"{card['code']}: {tag} required inside [[ ]]"


@pytest.mark.parametrize("card", CARDS, ids=[c["code"] for c in CARDS])
def test_tags_used_outside_optional_blocks_have_a_value(card):
    """Conversely: a tag substituted unconditionally must always resolve, so
    it needs a default (Metabase otherwise refuses to run the card)."""
    bare = re.sub(r"\[\[.*?\]\]", "", card["sql"], flags=re.S)
    for tag in set(TAG_RE.findall(bare)):
        assert card["tags"][tag]["default"] is not None, f"{card['code']}: {tag} has no default"


def test_tag_conventions_are_uniform_across_cards():
    """The same tag name means the same thing on every card."""
    seen = {}
    for card in CARDS:
        for tag, spec in card["tags"].items():
            if tag in seen:
                assert spec == seen[tag][1], f"{card['code']}.{tag} differs from {seen[tag][0]}"
            else:
                seen[tag] = (card["code"], spec)
    assert seen["corpus"][1]["default"] == "central-bank"
    assert seen["collection"][1]["default"] == "cb_corpus_v2"


def test_dashboard_filter_types_match_the_tags_they_map_to():
    by_code = {c["code"]: c for c in CARDS}
    for d in DASHBOARDS:
        by_slug = {f["slug"]: f for f in d["filters"]}
        assert len(by_slug) == len(d["filters"]), d["name"]
        for f in d["filters"]:
            assert f["type"] in set(FILTER_TYPE_BY_TAG_TYPE.values()), f
            assert f["name"].strip()
        for dc in d["cards"]:
            for slug, tag in dc["mappings"].items():
                tag_type = by_code[dc["code"]]["tags"][tag]["type"]
                assert by_slug[slug]["type"] == FILTER_TYPE_BY_TAG_TYPE[tag_type], \
                    f"{d['name']}/{dc['code']}: {slug} vs {tag}"


def test_every_dashboard_filter_drives_at_least_one_card():
    """A filter wired to nothing is a widget that silently does nothing."""
    for d in DASHBOARDS:
        used = {slug for dc in d["cards"] for slug in dc["mappings"]}
        assert used == {f["slug"] for f in d["filters"]}, d["name"]


def test_no_card_creates_schema_objects():
    """The views and tables exist in the ingester's DDL train; a card that
    creates them would silently fork the schema."""
    forbidden = re.compile(r"\b(CREATE|DROP|ALTER|INSERT|UPDATE|DELETE|TRUNCATE|GRANT)\b",
                           re.IGNORECASE)
    for card in CARDS:
        # strip SQL comments before looking for DDL verbs
        body = re.sub(r"--[^\n]*", "", card["sql"])
        assert not forbidden.search(body), card["code"]


def test_feature_coverage_map_is_complete():
    """Every inventory feature of the catalogue's coverage map has its card."""
    coverage = {
        1: {"R1", "R3", "R4"},
        2: {"C1", "C2", "C3", "C4", "C5", "Q3"},
        3: {"C4", "R2", "Q4"},
        4: {"C1", "C2", "C3"},
        5: {"C4"},
        6: {"C5"},
        7: {"C6"},
        8: {"Q3"},
        9: {"Q1"},
        10: {"Q2"},
        11: {"Q5"},
    }
    codes = {c["code"] for c in CARDS}
    for feature, cards in coverage.items():
        missing = cards - codes
        assert not missing, f"inventory #{feature}: missing {missing}"
