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

Dashboard 2's `as_of` filter maps onto Q1's and Q3's **`ref_date`** tag on
purpose — the two names mean the same thing ("evaluate as of this date"), and no
card on that dashboard has an `{{as_of}}` tag — so this is not a mis-wiring to
"fix".

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
- A date variable that means "evaluate as of now" is written
  `coalesce(NULL::date [[, {{ref_date}}]], current_date)`: the optional block
  disappears when the widget is empty, so the card falls back to *today* instead
  of to a frozen literal. `{{ref_date}}` (Q1, Q3) uses it.
- Cards **read only**: no card creates or alters a schema object. The only view
  they use (`rag_backlog`, read by Q4) belongs to `ingestion/ingest.py`'s DDL train.
- Dashboards are a 24-column grid; dashcards must not overlap.

## Deviations from the source catalogue

Three, all deliberate; everything else is the catalogue's SQL verbatim.

1. **Q5 · `error_type` → `error_class`.** The shipped `discovery_errors` table
   has `error_class`; the catalogue's *proposed* DDL called it `error_type`. The
   card fails with `UndefinedColumn` otherwise.
2. **No `CREATE VIEW` anywhere.** The catalogue ships `rag_backlog` as a
   `CREATE OR REPLACE VIEW` to run alongside Q4. The view now exists in the DDL
   train, so Q4 simply reads it and no card emits DDL (pinned by
   `test_no_card_creates_schema_objects`).
3. **`{{ref_date}}` (Q1, Q3) falls back to `current_date`, not to a literal.**
   The catalogue makes it required with "today" as the default, which in a
   checked-in file means a date that freezes forever. Both cards instead compute
   `as_of` in a `params` CTE as
   `coalesce(NULL::date [[, {{ref_date}}]], current_date)`, and the tag is
   optional with `default: null`. Empty widget → today; a date in the widget →
   that date. The same reasoning makes C4's `{{rag_state}}` optional: its whole
   predicate sits in one `[[AND ( … )]]` block, so an unset widget means "no
   restriction" rather than a card that refuses to run.

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
