#!/usr/bin/env python3
"""Apply ``cards.json`` and ``dashboards.json`` to a Metabase instance.

The JSON files beside this script are the truth: this applier creates what is
missing and overwrites what exists, matching by **name** inside the target
collection ("Vault" by default), and never hard-codes a database or collection
id — both are resolved by name. Re-running it is a no-op on the server's state:
template-tag ids and dashboard parameter ids are UUIDv5 of the definition, and
existing dashcards keep their ids.

Stdlib only, and the credential (``METABASE_API_KEY`` or ``METABASE_SESSION``)
is read from the environment, never logged and never written anywhere.

    METABASE_URL=https://metabase.example METABASE_API_KEY=... \
        python metabase/apply.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen  # bound here so a test can swap the transport

LOG = logging.getLogger("metabase.apply")

HERE = Path(__file__).resolve().parent
DEFAULT_CARDS = HERE / "cards.json"
DEFAULT_DASHBOARDS = HERE / "dashboards.json"

DEFAULT_DATABASE = "postgres"
DEFAULT_COLLECTION = "Vault"
EXAMPLES_COLLECTION = "Examples"
DEFAULT_COLLECTION_COLOR = "#509EE3"
TIMEOUT_SECONDS = 60
ID_NAMESPACE = "vault-metabase"


class MetabaseError(RuntimeError):
    """A non-2xx (or unreachable) Metabase API response."""

    def __init__(self, status: int, path: str, text: str):
        self.status = status
        self.path = path
        self.text = text
        super().__init__(f"Metabase HTTP {status} on {path}: {text[:400]}")


class ApplyError(RuntimeError):
    """The instance cannot satisfy the definitions (unknown database, …)."""


# --------------------------------------------------------------------- client


class MetabaseClient:
    """Minimal JSON client over the Metabase REST API (stdlib ``urllib``)."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        session: str | None = None,
        timeout: int = TIMEOUT_SECONDS,
    ):
        if not api_key and not session:
            raise ValueError("MetabaseClient needs an api_key or a session token")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session = session
        self.timeout = timeout

    def __repr__(self) -> str:  # never leak the credential
        kind = "api-key" if self._api_key else "session"
        return f"<MetabaseClient {self.base_url} auth={kind}>"

    def auth_headers(self) -> dict[str, str]:
        if self._api_key:
            return {"x-api-key": self._api_key}
        return {"X-Metabase-Session": self._session}

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body: dict):
        return self._request("POST", path, body)

    def put(self, path: str, body: dict):
        return self._request("PUT", path, body)

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for header, value in self.auth_headers().items():
            request.add_header(header, value)
        LOG.debug("%s %s", method, path)
        # Order matters: HTTPError is a URLError is an OSError, and so are the
        # socket failures (TimeoutError, ConnectionResetError,
        # http.client.RemoteDisconnected) the last branch is here to catch.
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise MetabaseError(exc.code, path, detail) from None
        except urllib.error.URLError as exc:
            raise MetabaseError(0, path, f"unreachable: {exc.reason}") from None
        except OSError as exc:
            raise MetabaseError(0, path, f"transport failed: {exc!s} ({type(exc).__name__})") \
                from None
        if not 200 <= status < 300:
            raise MetabaseError(status, path, raw)
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            # A 2xx that is not JSON is a proxy or a login page, not an answer.
            raise MetabaseError(status, path, f"response is not JSON ({exc}): {raw}") from None


# -------------------------------------------------------------- pure builders


def tag_id(code: str, tag: str) -> str:
    """Stable template-tag id, so a re-apply does not churn the card."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ID_NAMESPACE}:{code}:{tag}"))


def parameter_id(dashboard_name: str, slug: str) -> str:
    """Stable dashboard parameter id (mappings point at it by value)."""
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{ID_NAMESPACE}:dashboard:{dashboard_name}:{slug}")
    )


def template_tags(card: dict) -> dict:
    tags = {}
    for name, spec in card.get("tags", {}).items():
        tag = {
            "id": tag_id(card["code"], name),
            "name": name,
            "display-name": spec.get("display_name") or name,
            "type": spec["type"],
            "required": bool(spec.get("required", False)),
        }
        if spec.get("default") is not None:
            tag["default"] = spec["default"]
        tags[name] = tag
    return tags


def card_payload(card: dict, database_id: int, collection_id: int | None) -> dict:
    """The POST/PUT body for one native-SQL card."""
    return {
        "name": card["name"],
        "description": card.get("description"),
        "collection_id": collection_id,
        "display": card.get("display", "table"),
        "visualization_settings": card.get("visualization_settings") or {},
        "dataset_query": {
            "type": "native",
            "database": database_id,
            "native": {
                "query": card["sql"],
                "template-tags": template_tags(card),
            },
        },
    }


def dashboard_parameters(dashboard: dict) -> list[dict]:
    """One Metabase parameter per dashboard filter.

    ``string/=`` and ``number/=`` defaults are lists; ``date/single`` takes the
    bare value. A null default is omitted entirely.
    """
    parameters = []
    for filt in dashboard.get("filters", []):
        parameter = {
            "id": parameter_id(dashboard["name"], filt["slug"]),
            "name": filt["name"],
            "slug": filt["slug"],
            "type": filt["type"],
        }
        default = filt.get("default")
        if default is not None:
            if filt["type"].startswith("date"):
                parameter["default"] = default
            else:
                parameter["default"] = default if isinstance(default, list) else [default]
        parameters.append(parameter)
    return parameters


def dashcards(
    dashboard: dict,
    card_ids_by_code: dict[str, int],
    parameter_ids_by_slug: dict[str, str],
    *,
    existing_dashcards: list[dict] | None = None,
) -> list[dict]:
    """The full ``dashcards`` list for a dashboard (the layout is replaced whole).

    A dashcard already on the dashboard for the same card keeps its id; a new
    one gets a negative placeholder (-1, -2, …) as the API expects.
    """
    reusable: dict[int, list[int]] = {}
    for existing in existing_dashcards or []:
        if existing.get("card_id") is not None and existing.get("id", 0) > 0:
            reusable.setdefault(existing["card_id"], []).append(existing["id"])

    built = []
    new_index = 0
    for placement in dashboard.get("cards", []):
        card_id = card_ids_by_code[placement["code"]]
        pool = reusable.get(card_id)
        if pool:
            dashcard_id = pool.pop(0)
        else:
            new_index += 1
            dashcard_id = -new_index
        built.append(
            {
                "id": dashcard_id,
                "card_id": card_id,
                "row": placement["row"],
                "col": placement["col"],
                "size_x": placement["size_x"],
                "size_y": placement["size_y"],
                "parameter_mappings": [
                    {
                        "parameter_id": parameter_ids_by_slug[slug],
                        "card_id": card_id,
                        "target": ["variable", ["template-tag", tag]],
                    }
                    for slug, tag in placement.get("mappings", {}).items()
                ],
            }
        )
    return built


# ----------------------------------------------------------------------- plan


@dataclass
class Plan:
    """What an apply would do, given what the instance already holds."""

    cards_to_create: list = field(default_factory=list)
    cards_to_update: list = field(default_factory=list)
    dashboards_to_create: list = field(default_factory=list)
    dashboards_to_update: list = field(default_factory=list)
    card_ids_by_code: dict = field(default_factory=dict)
    dashboard_ids_by_name: dict = field(default_factory=dict)


def plan(cards, dashboards, existing_cards, existing_dashboards) -> Plan:
    """Split the definitions into creates and updates, matching by name.

    ``existing_cards`` / ``existing_dashboards`` are API objects already
    restricted to the target collection.
    """
    result = Plan()
    card_ids_by_name = _ids_by_name(existing_cards, "card")
    dashboard_ids_by_name = _ids_by_name(existing_dashboards, "dashboard")

    for card in cards:
        existing_id = card_ids_by_name.get(card["name"])
        if existing_id is None:
            result.cards_to_create.append(card)
        else:
            result.cards_to_update.append(card)
            result.card_ids_by_code[card["code"]] = existing_id

    for dashboard in dashboards:
        existing_id = dashboard_ids_by_name.get(dashboard["name"])
        if existing_id is None:
            result.dashboards_to_create.append(dashboard)
        else:
            result.dashboards_to_update.append(dashboard)
            result.dashboard_ids_by_name[dashboard["name"]] = existing_id

    return result


def _ids_by_name(items, kind: str = "card") -> dict[str, int]:
    """Live objects by name. Metabase allows two of them to share a name; the
    applier can only adopt one, so say which — the other is left orphaned."""
    ids: dict[str, int] = {}
    for item in items or []:
        if item.get("archived", False):
            continue
        name = item["name"]
        if name in ids:
            LOG.warning(
                "duplicate %s name %r (ids %s and %s): adopting id %s, "
                "the other is left untouched",
                kind, name, ids[name], item["id"], item["id"],
            )
        ids[name] = item["id"]
    return ids


# -------------------------------------------------------------------- summary


@dataclass
class Summary:
    """Counts of what the apply did (or would do, under ``--dry-run``).

    ``dashboards_skipped`` counts definitions the applier deliberately did not
    send — a dashboard whose cards could not all be resolved. Cards have no such
    count: every card in the file is either created or updated. An unchanged card
    is not "skipped" either: it is PUT again (the JSON is the truth, and the API's
    own normalisation makes a content diff unreliable), which leaves the card
    byte-identical.
    """

    collection_id: int | None = None
    collection_created: bool = False
    cards_created: int = 0
    cards_updated: int = 0
    dashboards_created: int = 0
    dashboards_updated: int = 0
    dashboards_skipped: int = 0
    examples_archived: bool = False
    dry_run: bool = False

    def as_line(self) -> str:
        prefix = "would apply" if self.dry_run else "applied"
        return (
            f"{prefix}: collection={self.collection_id}"
            f" (created={self.collection_created});"
            f" cards +{self.cards_created} ~{self.cards_updated};"
            f" dashboards +{self.dashboards_created} ~{self.dashboards_updated}"
            f" !{self.dashboards_skipped};"
            f" examples_archived={self.examples_archived}"
        )


# ---------------------------------------------------------------- orchestrate


def apply(
    client,
    cards,
    dashboards,
    *,
    database_name: str = DEFAULT_DATABASE,
    collection_name: str = DEFAULT_COLLECTION,
    archive_examples: bool = False,
    dry_run: bool = False,
) -> Summary:
    """Create or update every card and dashboard inside ``collection_name``.

    Every call goes through ``client`` (so the applier is testable without a
    server). Under ``dry_run`` only GETs are issued and the plan is logged.
    """
    summary = Summary(dry_run=dry_run)

    database_id = resolve_database(client, database_name)
    LOG.info("database %r resolved to id %s", database_name, database_id)

    collections = _as_list(client.get("/api/collection"))
    collection = _find_root_collection(collections, collection_name)
    if collection is None:
        if dry_run:
            LOG.info("would create the root collection %r", collection_name)
            summary.collection_created = True
        else:
            collection = _create_collection(client, collection_name)
            summary.collection_created = True
            LOG.info("created collection %r (id %s)", collection_name, collection["id"])
    collection_id = collection["id"] if collection else None
    summary.collection_id = collection_id

    existing_cards = _in_collection(client.get("/api/card"), collection_id)
    existing_dashboards = _in_collection(client.get("/api/dashboard"), collection_id)
    todo = plan(cards, dashboards, existing_cards, existing_dashboards)

    # --- cards -------------------------------------------------------------
    card_ids_by_code = dict(todo.card_ids_by_code)
    for card in todo.cards_to_create:
        payload = card_payload(card, database_id, collection_id)
        if dry_run:
            LOG.info("would create card %s", card["name"])
        else:
            created = client.post("/api/card", payload)
            card_ids_by_code[card["code"]] = created["id"]
            LOG.info("created card %s (id %s)", card["name"], created["id"])
        summary.cards_created += 1
    for card in todo.cards_to_update:
        payload = card_payload(card, database_id, collection_id)
        card_id = card_ids_by_code[card["code"]]
        if dry_run:
            LOG.info("would update card %s (id %s)", card["name"], card_id)
        else:
            client.put(f"/api/card/{card_id}", payload)
            LOG.info("updated card %s (id %s)", card["name"], card_id)
        summary.cards_updated += 1

    # --- dashboards --------------------------------------------------------
    for dashboard in dashboards:
        missing = [
            placement["code"]
            for placement in dashboard.get("cards", [])
            if placement["code"] not in card_ids_by_code
        ]
        is_new = dashboard["name"] not in todo.dashboard_ids_by_name
        if missing and not dry_run:
            LOG.warning(
                "skipping dashboard %s: no card for %s", dashboard["name"], ", ".join(missing)
            )
            summary.dashboards_skipped += 1
            continue

        if is_new:
            summary.dashboards_created += 1
        else:
            summary.dashboards_updated += 1

        if dry_run:
            LOG.info(
                "would %s dashboard %s with %d cards",
                "create" if is_new else "update",
                dashboard["name"],
                len(dashboard.get("cards", [])),
            )
            continue

        if is_new:
            created = client.post(
                "/api/dashboard",
                {
                    "name": dashboard["name"],
                    "description": dashboard.get("description"),
                    "collection_id": collection_id,
                },
            )
            dashboard_id = created["id"]
            current_dashcards: list[dict] = []
            LOG.info("created dashboard %s (id %s)", dashboard["name"], dashboard_id)
        else:
            dashboard_id = todo.dashboard_ids_by_name[dashboard["name"]]
            full = client.get(f"/api/dashboard/{dashboard_id}") or {}
            current_dashcards = full.get("dashcards") or []

        parameters = dashboard_parameters(dashboard)
        layout = dashcards(
            dashboard,
            card_ids_by_code,
            {p["slug"]: p["id"] for p in parameters},
            existing_dashcards=current_dashcards,
        )
        client.put(
            f"/api/dashboard/{dashboard_id}",
            {
                "name": dashboard["name"],
                "description": dashboard.get("description"),
                "parameters": parameters,
                "dashcards": layout,
            },
        )
        LOG.info("laid out dashboard %s: %d cards", dashboard["name"], len(layout))

    # --- the demo collection ----------------------------------------------
    if archive_examples:
        examples = _find_root_collection(collections, EXAMPLES_COLLECTION)
        if examples is None:
            LOG.info("no %r collection to archive", EXAMPLES_COLLECTION)
        elif dry_run:
            LOG.info("would archive the %r collection (id %s)", EXAMPLES_COLLECTION, examples["id"])
            summary.examples_archived = True
        else:
            client.put(f"/api/collection/{examples['id']}", {"archived": True})
            summary.examples_archived = True
            LOG.info("archived the %r collection (id %s)", EXAMPLES_COLLECTION, examples["id"])

    LOG.debug("%s", summary.as_line())
    return summary


def resolve_database(client, database_name: str) -> int:
    """Resolve the database id by name — the id is never hard-coded."""
    payload = client.get("/api/database")
    databases = payload.get("data", []) if isinstance(payload, dict) else _as_list(payload)
    for database in databases:
        if database.get("name") == database_name:
            return database["id"]
    for database in databases:  # a lenient second pass
        if str(database.get("name", "")).lower() == database_name.lower():
            return database["id"]
    known = ", ".join(sorted(str(d.get("name")) for d in databases)) or "none"
    raise ApplyError(f"no Metabase database named {database_name!r} (known: {known})")


def _create_collection(client, name: str) -> dict:
    body = {"name": name, "parent_id": None}
    try:
        return client.post("/api/collection", body)
    except MetabaseError as exc:
        if exc.status == 400 and "color" in exc.text.lower():
            # Older builds require a color on creation.
            return client.post("/api/collection", {**body, "color": DEFAULT_COLLECTION_COLOR})
        raise


def _find_root_collection(collections, name: str) -> dict | None:
    for collection in collections:
        if (
            collection.get("name") == name
            and not collection.get("archived", False)
            and collection.get("location", "/") == "/"
            and collection.get("personal_owner_id") is None
        ):
            return collection
    return None


def _in_collection(items, collection_id) -> list[dict]:
    if collection_id is None:
        return []
    return [
        item
        for item in _as_list(items)
        if item.get("collection_id") == collection_id and not item.get("archived", False)
    ]


def _as_list(payload) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        return list(payload.get("data", []))
    return list(payload)


# ----------------------------------------------------------------------- cli


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="apply.py",
        description="Apply the vault's Metabase cards and dashboards (idempotent).",
    )
    parser.add_argument("--url", default=None, help="Metabase base URL (default $METABASE_URL)")
    parser.add_argument("--cards", type=Path, default=DEFAULT_CARDS)
    parser.add_argument("--dashboards", type=Path, default=DEFAULT_DASHBOARDS)
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="database name to query")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="target collection name")
    parser.add_argument(
        "--archive-examples",
        action="store_true",
        help=f"archive the {EXAMPLES_COLLECTION!r} demo collection (reversible)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
    )
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    url = args.url or os.environ.get("METABASE_URL")
    if not url:
        LOG.error("no Metabase URL: pass --url or set METABASE_URL")
        return 1

    api_key = os.environ.get("METABASE_API_KEY") or None
    session = os.environ.get("METABASE_SESSION") or None
    if not api_key and not session:
        LOG.error("no credential: set METABASE_API_KEY or METABASE_SESSION")
        return 1

    try:
        cards = json.loads(Path(args.cards).read_text(encoding="utf-8"))
        dashboards = json.loads(Path(args.dashboards).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.error("cannot read the definitions: %s", exc)
        return 1

    client = MetabaseClient(url, api_key=api_key, session=session)
    try:
        summary = apply(
            client,
            cards,
            dashboards,
            database_name=args.database,
            collection_name=args.collection,
            archive_examples=args.archive_examples,
            dry_run=args.dry_run,
        )
    except (MetabaseError, ApplyError) as exc:
        LOG.error("%s", exc)
        return 1

    print(summary.as_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
