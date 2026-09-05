"""Unit tests for ``metabase/apply.py``.

No network: a ``FakeClient`` holds the Metabase state in memory, assigns ids on
POST, reflects that state on GET and records every call, so the applier's
idempotence can be asserted by replaying it against its own output.
"""

from __future__ import annotations

import copy
import importlib.util
import itertools
import json
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
METABASE = ROOT / "metabase"


def _load_apply():
    spec = importlib.util.spec_from_file_location("metabase_apply", METABASE / "apply.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mb = _load_apply()

CARDS = json.loads((METABASE / "cards.json").read_text())
DASHBOARDS = json.loads((METABASE / "dashboards.json").read_text())
BY_CODE = {c["code"]: c for c in CARDS}


class FakeClient:
    """An in-memory Metabase good enough for the applier's five endpoints."""

    def __init__(self, *, databases=None, collections=None, require_color=False):
        self.databases = (
            list(databases)
            if databases is not None
            else [
                {"id": 1, "name": "Sample Database", "engine": "h2"},
                {"id": 7, "name": "postgres", "engine": "postgres"},
            ]
        )
        self.collections = (
            list(collections)
            if collections is not None
            else [{"id": 2, "name": "Examples", "archived": False, "location": "/"}]
        )
        self.cards: dict[int, dict] = {}
        self.dashboards: dict[int, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.require_color = require_color
        self.wrap_database_list = True
        self._ids = itertools.count(100)
        self._dashcard_ids = itertools.count(500)

    # ------------------------------------------------------------- helpers
    def paths(self, method, path=None, prefix=None):
        return [
            p
            for m, p, _ in self.calls
            if m == method
            and (path is None or p == path)
            and (prefix is None or p.startswith(prefix))
        ]

    def count(self, method, path=None, prefix=None):
        return len(self.paths(method, path=path, prefix=prefix))

    def bodies(self, method, prefix):
        return [b for m, p, b in self.calls if m == method and p.startswith(prefix)]

    def state(self):
        return copy.deepcopy((self.collections, self.cards, self.dashboards))

    # -------------------------------------------------------------- routing
    def get(self, path):
        self.calls.append(("GET", path, None))
        if path == "/api/database":
            data = copy.deepcopy(self.databases)
            return {"data": data} if self.wrap_database_list else data
        if path == "/api/collection":
            return [copy.deepcopy(c) for c in self.collections if not c.get("archived")]
        if path == "/api/card":
            return [copy.deepcopy(c) for c in self.cards.values() if not c.get("archived")]
        if path == "/api/dashboard":
            return [
                {
                    "id": d["id"],
                    "name": d["name"],
                    "collection_id": d.get("collection_id"),
                    "archived": d.get("archived", False),
                }
                for d in self.dashboards.values()
                if not d.get("archived")
            ]
        if path.startswith("/api/dashboard/"):
            return copy.deepcopy(self.dashboards[int(path.rsplit("/", 1)[1])])
        raise mb.MetabaseError(404, path, "fake: no route")

    def post(self, path, body):
        self.calls.append(("POST", path, copy.deepcopy(body)))
        if path == "/api/collection":
            if self.require_color and "color" not in body:
                raise mb.MetabaseError(
                    400, path, '{"errors":{"color":"value must be a string"}}'
                )
            created = {
                "id": next(self._ids),
                "name": body["name"],
                "archived": False,
                "location": "/",
            }
            self.collections.append(created)
            return copy.deepcopy(created)
        if path == "/api/card":
            created = copy.deepcopy(body)
            created["id"] = next(self._ids)
            created.setdefault("archived", False)
            self.cards[created["id"]] = created
            return copy.deepcopy(created)
        if path == "/api/dashboard":
            created = copy.deepcopy(body)
            created["id"] = next(self._ids)
            created.setdefault("archived", False)
            created.setdefault("parameters", [])
            created.setdefault("dashcards", [])
            self.dashboards[created["id"]] = created
            return copy.deepcopy(created)
        raise mb.MetabaseError(404, path, "fake: no route")

    def put(self, path, body):
        self.calls.append(("PUT", path, copy.deepcopy(body)))
        kind, _, ident = path[len("/api/") :].partition("/")
        body = copy.deepcopy(body)
        if kind == "card":
            stored = self.cards[int(ident)]
            stored.update(body)
            return copy.deepcopy(stored)
        if kind == "dashboard":
            stored = self.dashboards[int(ident)]
            for dashcard in body.get("dashcards", []):
                if dashcard["id"] < 0:
                    dashcard["id"] = next(self._dashcard_ids)
            stored.update(body)
            return copy.deepcopy(stored)
        if kind == "collection":
            for collection in self.collections:
                if collection["id"] == int(ident):
                    collection.update(body)
                    return copy.deepcopy(collection)
            raise mb.MetabaseError(404, path, "fake: no such collection")
        raise mb.MetabaseError(404, path, "fake: no route")


# ---------------------------------------------------------------- builders


def test_tag_id_is_stable_and_scoped_to_card_and_tag():
    assert mb.tag_id("C0", "corpus") == mb.tag_id("C0", "corpus")
    assert mb.tag_id("C0", "corpus") != mb.tag_id("C1", "corpus")
    assert mb.tag_id("C0", "corpus") != mb.tag_id("C0", "bank")
    assert len(mb.tag_id("C0", "corpus")) == 36


def test_parameter_id_is_stable_and_scoped_to_dashboard_and_slug():
    first = mb.parameter_id("Vault — Corpus", "corpus")
    assert first == mb.parameter_id("Vault — Corpus", "corpus")
    assert first != mb.parameter_id("Vault — Coverage & QC", "corpus")
    assert first != mb.parameter_id("Vault — Corpus", "bank")
    assert first != mb.tag_id("Vault — Corpus", "corpus")


def test_card_payload_shape():
    payload = mb.card_payload(BY_CODE["C1"], database_id=7, collection_id=42)
    assert payload["name"] == BY_CODE["C1"]["name"]
    assert payload["collection_id"] == 42
    assert payload["display"] == BY_CODE["C1"]["display"]
    assert payload["visualization_settings"] == BY_CODE["C1"]["visualization_settings"]
    query = payload["dataset_query"]
    assert query["type"] == "native"
    assert query["database"] == 7
    assert query["native"]["query"] == BY_CODE["C1"]["sql"]

    tags = query["native"]["template-tags"]
    assert set(tags) == set(BY_CODE["C1"]["tags"])
    corpus = tags["corpus"]
    assert corpus == {
        "id": mb.tag_id("C1", "corpus"),
        "name": "corpus",
        "display-name": "Corpus",
        "type": "text",
        "required": True,
        "default": "central-bank",
    }
    # an optional tag carries no default at all
    assert tags["bank"]["required"] is False
    assert "default" not in tags["bank"]


def test_card_payload_covers_every_declared_tag_of_every_card():
    for card in CARDS:
        tags = mb.card_payload(card, 7, 42)["dataset_query"]["native"]["template-tags"]
        assert set(tags) == set(card["tags"]), card["code"]
        for name, tag in tags.items():
            assert tag["name"] == name
            assert tag["id"] == mb.tag_id(card["code"], name)
            assert tag["type"] in {"text", "number", "date"}
            assert isinstance(tag["required"], bool)
            if card["tags"][name]["required"]:
                assert tag["default"] == card["tags"][name]["default"]


def test_dashboard_parameters_types_and_defaults():
    corpus = mb.dashboard_parameters(DASHBOARDS[0])
    by_slug = {p["slug"]: p for p in corpus}
    assert by_slug["corpus"]["type"] == "string/="
    assert by_slug["corpus"]["id"] == mb.parameter_id(DASHBOARDS[0]["name"], "corpus")
    assert by_slug["corpus"]["name"] == "Corpus"
    assert by_slug["corpus"]["default"] == ["central-bank"]  # string/= defaults are lists
    assert by_slug["as_of"]["type"] == "date/single"
    assert "default" not in by_slug["as_of"]  # null default is omitted
    assert "default" not in by_slug["bank"]

    rag = {p["slug"]: p for p in mb.dashboard_parameters(DASHBOARDS[2])}
    assert rag["lookback_days"]["type"] == "number/="
    assert rag["lookback_days"]["default"] == [90]  # number/= defaults are lists too


def test_dashboard_parameters_date_default_is_a_bare_string():
    dashboard = {
        "name": "D",
        "filters": [{"slug": "as_of", "name": "As of", "type": "date/single", "default": "2026-01-01"}],
        "cards": [],
    }
    assert mb.dashboard_parameters(dashboard)[0]["default"] == "2026-01-01"


def test_dashcards_layout_mappings_and_negative_ids():
    dashboard = DASHBOARDS[0]
    ids = {c["code"]: 1000 + i for i, c in enumerate(CARDS)}
    parameters = {p["slug"]: p["id"] for p in mb.dashboard_parameters(dashboard)}
    built = mb.dashcards(dashboard, ids, parameters)

    assert [dc["id"] for dc in built] == [-1, -2, -3, -4, -5, -6, -7]
    first, second = built[0], built[1]
    assert first["card_id"] == ids["C0"]
    assert (first["row"], first["col"], first["size_x"], first["size_y"]) == (0, 0, 24, 3)
    assert first["parameter_mappings"] == [
        {
            "parameter_id": parameters["corpus"],
            "card_id": ids["C0"],
            "target": ["variable", ["template-tag", "corpus"]],
        }
    ]
    assert {m["parameter_id"] for m in second["parameter_mappings"]} == {
        parameters[slug] for slug in dashboard["cards"][1]["mappings"]
    }
    assert all(
        m["target"][0] == "variable" and m["target"][1][0] == "template-tag"
        for dc in built
        for m in dc["parameter_mappings"]
    )


def test_dashcards_map_to_the_card_tag_not_the_filter_slug():
    qc = DASHBOARDS[1]
    ids = {c["code"]: 1000 + i for i, c in enumerate(CARDS)}
    parameters = {p["slug"]: p["id"] for p in mb.dashboard_parameters(qc)}
    built = {dc["card_id"]: dc for dc in mb.dashcards(qc, ids, parameters)}
    # Q1 maps the dashboard's "as_of" filter onto the card's "ref_date" tag
    q1 = built[ids["Q1"]]
    mapping = next(m for m in q1["parameter_mappings"] if m["parameter_id"] == parameters["as_of"])
    assert mapping["target"] == ["variable", ["template-tag", "ref_date"]]


def test_dashcards_without_mappings_are_allowed():
    rag = DASHBOARDS[2]
    ids = {c["code"]: 1000 + i for i, c in enumerate(CARDS)}
    parameters = {p["slug"]: p["id"] for p in mb.dashboard_parameters(rag)}
    built = {dc["card_id"]: dc for dc in mb.dashcards(rag, ids, parameters)}
    assert built[ids["R3"]]["parameter_mappings"] == []


def test_dashcards_reuse_existing_dashcard_ids():
    dashboard = DASHBOARDS[0]
    ids = {c["code"]: 1000 + i for i, c in enumerate(CARDS)}
    parameters = {p["slug"]: p["id"] for p in mb.dashboard_parameters(dashboard)}
    existing = [{"id": 900, "card_id": ids["C3"]}, {"id": 901, "card_id": ids["C0"]}]
    built = mb.dashcards(dashboard, ids, parameters, existing_dashcards=existing)
    by_card = {dc["card_id"]: dc["id"] for dc in built}
    assert by_card[ids["C0"]] == 901
    assert by_card[ids["C3"]] == 900
    assert by_card[ids["C1"]] < 0  # unknown ones stay new


# -------------------------------------------------------------------- plan


def test_plan_splits_creates_and_updates():
    existing_cards = [{"id": 11, "name": CARDS[0]["name"]}]
    existing_dashboards = [{"id": 21, "name": DASHBOARDS[0]["name"]}]
    result = mb.plan(CARDS, DASHBOARDS, existing_cards, existing_dashboards)
    assert [c["code"] for c in result.cards_to_update] == [CARDS[0]["code"]]
    assert result.card_ids_by_code[CARDS[0]["code"]] == 11
    assert len(result.cards_to_create) == 17
    assert [d["name"] for d in result.dashboards_to_update] == [DASHBOARDS[0]["name"]]
    assert len(result.dashboards_to_create) == 2
    assert result.dashboard_ids_by_name[DASHBOARDS[0]["name"]] == 21


# ------------------------------------------------------------------- apply


def test_first_apply_creates_collection_cards_and_dashboards():
    fake = FakeClient()
    summary = mb.apply(fake, CARDS, DASHBOARDS)

    assert fake.count("POST", path="/api/collection") == 1
    assert fake.count("POST", path="/api/card") == 18
    assert fake.count("POST", path="/api/dashboard") == 3
    assert fake.count("PUT", prefix="/api/dashboard/") == 3
    assert fake.count("PUT", prefix="/api/card/") == 0
    assert fake.count("PUT", prefix="/api/collection/") == 0

    assert summary.cards_created == 18
    assert summary.cards_updated == 0
    assert summary.dashboards_created == 3
    assert summary.dashboards_updated == 0
    assert summary.collection_created is True
    assert summary.examples_archived is False

    collection_post = fake.bodies("POST", "/api/collection")[0]
    assert collection_post["name"] == "Vault"
    assert collection_post["parent_id"] is None

    collection_id = next(c["id"] for c in fake.collections if c["name"] == "Vault")
    assert {c["collection_id"] for c in fake.cards.values()} == {collection_id}
    assert {d["collection_id"] for d in fake.dashboards.values()} == {collection_id}
    # the database is resolved by name, never hard-coded
    assert {c["dataset_query"]["database"] for c in fake.cards.values()} == {7}


def test_second_apply_updates_and_creates_nothing():
    fake = FakeClient()
    mb.apply(fake, CARDS, DASHBOARDS)
    fake.calls.clear()

    summary = mb.apply(fake, CARDS, DASHBOARDS)

    assert fake.count("POST") == 0
    assert fake.count("PUT", prefix="/api/card/") == 18
    assert fake.count("PUT", prefix="/api/dashboard/") == 3
    assert summary.cards_created == 0
    assert summary.cards_updated == 18
    assert summary.dashboards_created == 0
    assert summary.dashboards_updated == 3
    assert summary.collection_created is False
    assert len(fake.cards) == 18
    assert len(fake.dashboards) == 3


def test_reapply_leaves_the_server_state_identical():
    fake = FakeClient()
    mb.apply(fake, CARDS, DASHBOARDS)
    before = fake.state()
    mb.apply(fake, CARDS, DASHBOARDS)
    assert fake.state() == before, "a no-op re-apply must not churn ids or content"


def test_apply_ignores_cards_of_other_collections():
    fake = FakeClient()
    fake.cards[55] = {
        "id": 55,
        "name": CARDS[0]["name"],
        "collection_id": 999,
        "archived": False,
    }
    mb.apply(fake, CARDS, DASHBOARDS)
    assert fake.count("PUT", path="/api/card/55") == 0
    assert fake.count("POST", path="/api/card") == 18


def test_apply_reuses_an_existing_vault_collection():
    fake = FakeClient(
        collections=[
            {"id": 2, "name": "Examples", "archived": False, "location": "/"},
            {"id": 3, "name": "Vault", "archived": False, "location": "/"},
        ]
    )
    summary = mb.apply(fake, CARDS, DASHBOARDS)
    assert fake.count("POST", path="/api/collection") == 0
    assert summary.collection_created is False
    assert summary.collection_id == 3
    assert {c["collection_id"] for c in fake.cards.values()} == {3}


def test_apply_tolerates_a_bare_database_list():
    fake = FakeClient()
    fake.wrap_database_list = False
    mb.apply(fake, CARDS, DASHBOARDS)
    assert {c["dataset_query"]["database"] for c in fake.cards.values()} == {7}


def test_apply_retries_the_collection_post_with_a_color():
    fake = FakeClient(require_color=True)
    mb.apply(fake, CARDS, DASHBOARDS)
    posts = fake.bodies("POST", "/api/collection")
    assert len(posts) == 2
    assert "color" not in posts[0]
    assert posts[1]["color"].startswith("#")


def test_apply_fails_when_the_database_is_unknown():
    fake = FakeClient(databases=[{"id": 1, "name": "Sample Database", "engine": "h2"}])
    with pytest.raises(mb.ApplyError):
        mb.apply(fake, CARDS, DASHBOARDS)


def test_archive_examples_is_never_implicit():
    fake = FakeClient()
    summary = mb.apply(fake, CARDS, DASHBOARDS)
    assert fake.count("PUT", prefix="/api/collection/") == 0
    assert fake.count("PUT", path="/api/collection/2") == 0
    assert summary.examples_archived is False
    assert next(c for c in fake.collections if c["name"] == "Examples")["archived"] is False


def test_archive_examples_archives_the_examples_collection():
    fake = FakeClient()
    summary = mb.apply(fake, CARDS, DASHBOARDS, archive_examples=True)
    assert fake.paths("PUT", prefix="/api/collection/") == ["/api/collection/2"]
    assert fake.bodies("PUT", "/api/collection/") == [{"archived": True}]
    assert summary.examples_archived is True
    assert next(c for c in fake.collections if c["name"] == "Examples")["archived"] is True


def test_archive_examples_is_idempotent_when_already_archived():
    fake = FakeClient(
        collections=[{"id": 2, "name": "Examples", "archived": True, "location": "/"}]
    )
    summary = mb.apply(fake, CARDS, DASHBOARDS, archive_examples=True)
    assert fake.count("PUT", prefix="/api/collection/") == 0
    assert summary.examples_archived is False


def test_dry_run_issues_no_writes():
    fake = FakeClient()
    summary = mb.apply(fake, CARDS, DASHBOARDS, archive_examples=True, dry_run=True)
    assert fake.count("POST") == 0
    assert fake.count("PUT") == 0
    assert {m for m, _, _ in fake.calls} == {"GET"}
    assert summary.dry_run is True
    assert summary.cards_created == 18
    assert summary.dashboards_created == 3
    assert fake.cards == {} and fake.dashboards == {}


def test_dry_run_on_an_applied_instance_plans_updates_only():
    fake = FakeClient()
    mb.apply(fake, CARDS, DASHBOARDS)
    fake.calls.clear()
    summary = mb.apply(fake, CARDS, DASHBOARDS, dry_run=True)
    assert fake.count("POST") == 0 and fake.count("PUT") == 0
    assert summary.cards_updated == 18
    assert summary.dashboards_updated == 3


def test_apply_propagates_metabase_errors():
    class Boom(FakeClient):
        def post(self, path, body):
            self.calls.append(("POST", path, body))
            raise mb.MetabaseError(500, path, "boom")

    with pytest.raises(mb.MetabaseError):
        mb.apply(Boom(), CARDS, DASHBOARDS)


# ------------------------------------------------------------------ client


def test_client_prefers_the_api_key_header():
    client = mb.MetabaseClient("https://metabase.example/", api_key="k", session="s")
    assert client.auth_headers() == {"x-api-key": "k"}
    assert client.base_url == "https://metabase.example"


def test_client_falls_back_to_the_session_header():
    client = mb.MetabaseClient("https://metabase.example", session="s")
    assert client.auth_headers() == {"X-Metabase-Session": "s"}


def test_client_refuses_to_be_built_without_credentials():
    with pytest.raises(ValueError):
        mb.MetabaseClient("https://metabase.example")


def test_client_never_reveals_the_credential():
    client = mb.MetabaseClient("https://metabase.example", api_key="super-secret")
    assert "super-secret" not in repr(client)
    assert "super-secret" not in str(client)


def test_metabase_error_message_carries_status_and_path():
    error = mb.MetabaseError(404, "/api/card/1", "not found")
    assert error.status == 404 and error.path == "/api/card/1"
    assert "404" in str(error) and "/api/card/1" in str(error)


# -------------------------------------------------------------------- main


def _fake_factory(fake, captured):
    def factory(base_url, api_key=None, session=None):
        captured.update(base_url=base_url, api_key=api_key, session=session)
        return fake

    return factory


def test_main_dry_run_uses_the_api_key_from_the_environment(monkeypatch, caplog, capsys):
    fake, captured = FakeClient(), {}
    monkeypatch.setattr(mb, "MetabaseClient", _fake_factory(fake, captured))
    monkeypatch.setenv("METABASE_URL", "https://metabase.example")
    monkeypatch.setenv("METABASE_API_KEY", "super-secret")
    monkeypatch.setenv("METABASE_SESSION", "session-secret")

    with caplog.at_level(logging.DEBUG):
        assert mb.main(["--dry-run"]) == 0

    assert captured == {
        "base_url": "https://metabase.example",
        "api_key": "super-secret",
        "session": "session-secret",
    }
    assert fake.count("POST") == 0 and fake.count("PUT") == 0
    out = capsys.readouterr()
    printed = out.out + out.err + caplog.text
    assert "super-secret" not in printed
    assert "session-secret" not in printed


def test_main_falls_back_to_the_session_when_no_api_key(monkeypatch):
    fake, captured = FakeClient(), {}
    monkeypatch.setattr(mb, "MetabaseClient", _fake_factory(fake, captured))
    monkeypatch.delenv("METABASE_API_KEY", raising=False)
    monkeypatch.setenv("METABASE_SESSION", "session-secret")
    assert mb.main(["--url", "https://metabase.example", "--dry-run"]) == 0
    assert captured["api_key"] is None and captured["session"] == "session-secret"


def test_main_applies_for_real_without_dry_run(monkeypatch):
    fake, captured = FakeClient(), {}
    monkeypatch.setattr(mb, "MetabaseClient", _fake_factory(fake, captured))
    monkeypatch.setenv("METABASE_URL", "https://metabase.example")
    monkeypatch.setenv("METABASE_API_KEY", "k")
    assert mb.main([]) == 0
    assert fake.count("POST", path="/api/card") == 18
    assert len(fake.dashboards) == 3


def test_main_exits_1_without_credentials(monkeypatch, caplog):
    monkeypatch.delenv("METABASE_API_KEY", raising=False)
    monkeypatch.delenv("METABASE_SESSION", raising=False)
    monkeypatch.setenv("METABASE_URL", "https://metabase.example")
    with caplog.at_level(logging.ERROR):
        assert mb.main([]) == 1
    assert "METABASE_API_KEY" in caplog.text


def test_main_exits_1_without_a_url(monkeypatch):
    monkeypatch.delenv("METABASE_URL", raising=False)
    monkeypatch.setenv("METABASE_API_KEY", "k")
    assert mb.main([]) == 1


def test_main_exits_1_on_a_metabase_error(monkeypatch, caplog):
    class Boom(FakeClient):
        def get(self, path):
            raise mb.MetabaseError(401, path, "Unauthenticated")

    captured = {}
    monkeypatch.setattr(mb, "MetabaseClient", _fake_factory(Boom(), captured))
    monkeypatch.setenv("METABASE_URL", "https://metabase.example")
    monkeypatch.setenv("METABASE_API_KEY", "k")
    with caplog.at_level(logging.ERROR):
        assert mb.main([]) == 1
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_main_reads_the_definitions_beside_the_script_by_default(monkeypatch):
    fake, captured = FakeClient(), {}
    monkeypatch.setattr(mb, "MetabaseClient", _fake_factory(fake, captured))
    monkeypatch.setenv("METABASE_URL", "https://metabase.example")
    monkeypatch.setenv("METABASE_API_KEY", "k")
    monkeypatch.chdir(ROOT.parent)
    assert mb.main(["--dry-run"]) == 0
    assert fake.count("GET", path="/api/card") == 1


def test_main_archive_examples_flag(monkeypatch):
    fake, captured = FakeClient(), {}
    monkeypatch.setattr(mb, "MetabaseClient", _fake_factory(fake, captured))
    monkeypatch.setenv("METABASE_URL", "https://metabase.example")
    monkeypatch.setenv("METABASE_API_KEY", "k")
    assert mb.main(["--archive-examples"]) == 0
    assert fake.paths("PUT", prefix="/api/collection/") == ["/api/collection/2"]
