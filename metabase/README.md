# Metabase over the vault, as code

`cards.json` and `dashboards.json` are the truth for the vault's Metabase layer:
18 native-SQL cards on three dashboards. They are **edited by hand** here and
**applied** to a Metabase instance by `apply.py` — never the other way round. A
change made in the Metabase UI is overwritten on the next apply.

| File | What it holds |
|---|---|
| `cards.json` | One object per card: `code`, `name` (`"<code> · <title>"`), `description`, `display`, `visualization_settings`, `sql`, `tags` |
| `dashboards.json` | One object per dashboard: `name`, `description`, `filters`, and `cards` (grid placement + filter→tag mappings) |

## Dashboards

- **Vault — Corpus** (C0–C6) — what is in the corpus: volume, shape, coverage, document explorer.
- **Vault — Coverage & QC** (Q1–Q5) — what is missing, late or wrong.
- **Vault — RAG & Runs** (R1–R6) — is the machine healthy.

## Conventions

- **SQL** is Metabase native SQL: `{{tag}}` is a simple variable (Text / Number /
  Date, never a Field Filter — simple variables work inside CTEs, which several
  cards depend on), `[[AND … {{tag}}]]` is an optional clause that disappears when
  the variable has no value.
- A tag substituted **outside** a `[[ ]]` block is `required` with a `default`, so
  the card always runs; a tag that only ever appears **inside** one is optional with
  `default: null`. `{{corpus}}` defaults to `central-bank`, `{{collection}}` to
  `cb_corpus_v2`.
- Every card reading `documents` carries `d.deleted_at IS NULL` (C0 carries it per
  `FILTER` clause, because reporting the soft-deleted count is its job).
- Cards **read only**: no card creates or alters a schema object. The views they
  use (`rag_backlog`, `runs_sources`, …) belong to `ingestion/ingest.py`'s DDL train.
- Dashboards are a 24-column grid; dashcards must not overlap.

## Applying

```bash
METABASE_URL=https://… METABASE_API_KEY=… python metabase/apply.py --dry-run
```

`apply.py` is idempotent by card/dashboard name inside the `Vault` collection.
Authentication comes from `METABASE_API_KEY` or `METABASE_SESSION` in the
environment — never commit a key or an instance host name.

## Guards

- `tests/test_metabase_definitions.py` — schema, tag declarations, soft-delete
  filter, grid placement, filter/tag type agreement. No network, no database.
- `tests/integration/test_metabase_cards_sql.py` — renders every card the way
  Metabase does and executes it against a Postgres built by the real ingester,
  both with card defaults only and with every filter supplied.
