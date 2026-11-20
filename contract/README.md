# vault-contract

The MyOpenFund **`documents` contract**, version **1.0.0**: what a corpus writes on each line
of its manifests, what its vocabulary file declares, and how the vault checks both.

One contract for every corpus. Common fields have one name and one meaning everywhere and
become typed columns in the vault; corpus-specific fields travel as extras. The vault
ingestion enforces the contract; producers validate their own rows and vocabulary against it
in CI; data-orchestrator builds the RAG payload from the same split.

Standard library only, Python 3.10+ (data-orchestrator imports it at run time; CI runs the
package's tests and the checker over the golden rows on 3.10, job `contract-py310`).

## Install

Pin it by commit, from the `contract/` subdirectory of the vault repo:

```
vault-contract @ git+https://github.com/MyOpenFund/vault@<commit>#subdirectory=contract
```

In this repo, `pip install -r requirements-dev.txt` installs it in editable mode. The vault
ingestion image does not pip-install it: it copies `src/vault_contract` next to `ingest.py`.

## Usage

One call per manifest row:

```python
import json

from vault_contract import check_row, load_vocabulary

vocabulary = load_vocabulary(json.load(open("vocabulary.json")))   # ValueError if invalid
checked = check_row(json.loads(manifest_line), vocabulary, expected_corpus="company")
checked.columns     # the 18 common fields, in DDL order (None when absent)
checked.extra       # every other key, values untouched
checked.refusals    # () = accepted; else (Refusal("bad_date_precision", "date_precision", "Day"), ...)
checked.notices     # written either way; e.g. (Notice("empty_title", "title", ""),)
checked.accepted    # not checked.refusals
```

`check_row(row, vocabulary, *, expected_corpus, transition=True) -> CheckedRow` runs the
three lower-level functions, which stay public for callers that need one step only:

- `split_row(row, *, transition=True) -> (columns, extra)`: never refuses, never mutates
  `row` (data-orchestrator builds the RAG payload from it).
- `validate_row(columns, vocabulary, *, expected_corpus, transition=True) -> list[Refusal]`:
  with `vocabulary=None` (a corpus with no vocabulary file yet) membership in the vocabulary
  is not checked; every presence and shape check is kept. A vocabulary of another corpus
  raises `ValueError`.
- `quality_notices(columns, *, row, transition=True) -> list[Notice]`: takes the original
  `row` too, because what `split_row` mapped or dropped only shows there.
- `validate_vocabulary(doc) -> list[str]` and `load_vocabulary(doc) -> Vocabulary`.
- `vault_contract.golden`: the reference rows below, shipped with the package.

From a shell, over a directory of manifests:

```
python -m vault_contract.check <manifest_dir> --corpus central-bank \
    [--vocabulary vocabulary.json] [--glob '*.jsonl'] [--no-transition] [--strict]
```

It reads files as the vault ingestion does (UTF-8; lines ended by `\n`, `\r\n` or `\r` only)
and prints the count of refusals and notices per reason with up to 3 examples each. A
non-blank line that is not a JSON object (a torn write, a JSON array, nesting or an integer
too large to parse) is refused as `not_json`, as the vault ingestion refuses it. It also
resolves `doc_id`s as the vault does: the last valid line of a `doc_id` wins, so every earlier
valid occurrence is refused as `duplicate_doc_id`, pointing at the kept one. Exit 0: nothing
refused. 1: a row refused (`not_json` included), or under `--strict` a notice. 2: usage
error (empty `--corpus` or one that is not a corpus code, bad `--glob`), no matching file, a file that cannot be read, is not
UTF-8 or starts with a UTF-8 BOM (the ingestion would refuse its first line as `not_json`),
no line that is a JSON object at all, an unreadable or invalid vocabulary, a vocabulary of
another corpus.

## Fields

A manifest line is one JSON object. These 18 keys are the common fields. "When absent"
says what happens to a row without the field (absent, `null`, or `""`): **refused**
(`REFUSED_WHEN_ABSENT`), **notice** (`EXPECTED`, the row is written), or nothing.

| Field | Type | When absent | Rule | central-bank example | company example |
|---|---|---|---|---|---|
| `doc_id` | text | refused | Stable, **unique within its corpus**; the producer chooses its formula | `ceb04c0aa89ed888` (hash of bank, type, URL) | `cf0e49e6e176a851` (hash of CIK, form, accession) |
| `corpus` | text | refused (notice `corpus_missing` during the transition) | Must equal the ingesting service's `CORPUS` | `central-bank` | `company` |
| `source_code` | text | refused | The authority that publishes; declared in the corpus vocabulary | `ecb` | `sec` |
| `doc_type` | text | refused | Declared in the corpus vocabulary; meaningful only with `corpus` | `A1` (rate decision) | `A1` (10-K) |
| `title` | text | notice `empty_title` | Expected non-empty | `Monetary policy decisions` | `Apple Inc. 10-K (2024)` |
| `date` | date | — | ISO `YYYY-MM-DD`; a month or year date is the first day of the period (else notice `date_not_period_start`) | `2022-07-21` | `2024-11-01` (filing date) |
| `year` | integer | refused when `date` is set (`year_mismatch`) | The year of `date`; absent when `date` is | `2022` | `2024` |
| `date_precision` | text | notice `precision_not_stated` when `date` is set | `day` \| `month` \| `year`; null = not stated, never "day"; dropped on an undated row (notice `precision_without_date`) | `day` | `day` |
| `date_kind` | text | — | `publication` \| `meeting` \| `period`; dropped on an undated row (notice `kind_without_date`) | `meeting` | `publication` |
| `language` | text | refused | ISO 639-1, two lowercase letters | `en` | `en` |
| `provenance` | text | refused | How the document was obtained; declared in the corpus vocabulary | `bank_site` | `edgar_index` |
| `document_url` | text | — | Address of the document itself (any type) | `https://www.ecb.europa.eu/press/pr/date/2022/html/ecb.mp220721~53e5bdd317.en.html` | `https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm` |
| `source_url` | text | — | Landing or index page it was found on | `https://www.ecb.europa.eu/press/pr/html/index.en.html` | `https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm` |
| `mime_type` | text | notice `mime_type_missing` when `local_path` is set | Type of the file at `local_path` (a type known before any download is accepted) | `application/pdf` | `application/pdf` or `text/plain` |
| `sha256` | text | — (expected when a file was fetched) | 64 lowercase hex: the hash of the document **as the authority served it**, not of a converted file | hash of the fetched PDF, or of the fetched HTML for a rendered row | hash of the primary document `aapl-20240928.htm` |
| `local_path` | text | — | Plain POSIX path **relative to the corpus data root** (the directory holding `manifest/`); see `bad_local_path` | `raw/ecb/C1/2023/e1e6a63950c50d3f.pdf` | `raw/0000320193/A1/2024/cf0e49e6e176a851.pdf` |
| `entity_scheme` | text | — (expected when the document concerns one company) | `cik`, `lei`, or a register scheme declared in the corpus vocabulary | (absent) | `cik` |
| `entity_value` | text | refused when `entity_scheme` is set (`entity_half`) | The identifier in that scheme, **as the scheme spells it** (a string: CIK `0000320193`, never `320193`) | (absent) | `0000320193` |

Rules around the table:

- **Text is text.** Every text column that is set must be a non-blank string without NUL
  (`bad_text` otherwise): Postgres TEXT cannot hold NUL, and a number loses its spelling.
- **Extras.** Every other key goes to `extra` without refusal: `accession`, `sec_form`,
  `period_of_report`, `company`, `ticker`, `entity_id`, `repec_handle`, `date_source`,
  `html_path`, ... A cross-scheme `entity_id` stays an extra.
- **Entity = the company the document is about**, not its author. A Form 4 filed by an
  officer is about Apple: `entity_scheme: cik`, `entity_value: 0000320193`. The author, when
  different and known, is an extra (`filer_scheme`, `filer_value`), never `entity_*`. An ECB
  rate decision concerns no company: no entity.
- **`local_path` and `sha256` describe different things on purpose**: the file the RAG reads,
  and the bytes the authority published. A rendered PDF keeps the hash of the HTML it was
  rendered from. A producer that has the hash of the file at `local_path` puts it in the
  `local_sha256` extra.
- **Blank means absent.** A blank string (`""` or whitespace) in a field that is not refused
  when absent is read as absent (`None`). In a field refused when absent, `""` is
  `missing_<field>` and whitespace is `bad_text`.
- **No date, no precision, no kind.** `split_row` drops `date_precision` and `date_kind`
  from an undated row (each drop is a notice).
- **Identity in the vault is (`corpus`, `doc_id`)**: two corpora may both have a document `1`.
- **`doc_type` belongs to its corpus.** central-bank `A1` (rate-decision press release) and
  company `A1` (10-K) share a code, not a meaning; a `doc_type` is always read with `corpus`.

### Transition (`transition=True`, the switch the cleanup release flips)

Until every manifest is migrated, `transition=True` (the default everywhere) also reads, and
notices each time:

- `bank_code` as `source_code` and `pdf_url` as `document_url`, when the contract key is
  absent or blank; the old key never lands in `extra` (notice `legacy_alias`, `field` = the
  old key);
- a `local_path` starting with `data/`, stored without that prefix (`legacy_data_prefix`; a
  bare `data/` is refused, not emptied);
- a row without `corpus`: the ingesting service's `CORPUS` applies (`corpus_missing`).

The cleanup release passes `transition=False`: the old keys are then ordinary extras (so a
row with `bank_code` only is `missing_source_code`), a `data/` path is refused
(`bad_local_path`, still noticed), and a row without `corpus` is `missing_corpus`. Run the checker with `--no-transition` to see that future on
today's manifests. Independently, a corpus without a vocabulary file is checked without
membership (`vocabulary=None`).

## Two levels: refusals and notices

- A **refusal** is about structure or identity: the row is **not written**, and the vault
  skips its soft-delete sweep for that run. Example: `date_precision: "Day"` is refused
  (`bad_date_precision`); a closed value is spelled exactly.
- A **notice** is about quality: the row **is written** and the notice is counted. Example:
  a dated row with `date_precision: null` gets `precision_not_stated`; a producer that says
  nothing does not claim a day, so null means "not stated", never "day".

Both reason lists, in check order, are part of the contract: run reports count rows by reason.

### Refusal reasons

`validate_row` returns one `Refusal(reason, field, value)` per defect of the columns,
`extra_refusals` one per unstorable top-level key of `extra`; `check_row` returns both, the
columns' first.

| Reason | When |
|---|---|
| `missing_doc_id` | `doc_id` absent or `""` |
| `missing_corpus` | `corpus` absent or `""`, once the transition is over (`transition=False`) |
| `missing_source_code` | `source_code` absent or `""` (checked with or without a vocabulary) |
| `missing_doc_type` | `doc_type` absent or `""` |
| `missing_language` | `language` absent or `""` |
| `missing_provenance` | `provenance` absent or `""` |
| `bad_text` | a text column set to something else than a non-blank string without NUL or lone surrogate: a number (a CIK `320193` has lost its zeros), a list, an object, `"  "`, a NUL, an escaped `\ud800`; also a `doc_id` with leading or trailing whitespace; `field` names the column |
| `too_long` | a text column past its cap; `field` names it. Identifiers and paths count UTF-8 bytes, other text characters: `doc_id`, `entity_value` ≤ 512 bytes (`MAX_ID_BYTES`); `corpus`, `source_code`, `doc_type`, `date_precision`, `date_kind`, `language`, `provenance`, `entity_scheme` ≤ 32 characters (`MAX_CODE_CHARS`, as a vocabulary code); `local_path` ≤ 4096 bytes and each segment ≤ 255 bytes (`MAX_PATH_BYTES`, `MAX_PATH_SEGMENT_BYTES`: Linux PATH_MAX and NAME_MAX); `title` ≤ 2000 characters (`MAX_TITLE_CHARS`); `document_url`, `source_url` ≤ 4096 characters (`MAX_URL_CHARS`). A too-long field is not judged further |
| `corpus_conflict` | `corpus` present and not the ingesting service's corpus (a code: `expected_corpus` must match `CODE_PATTERN`, else `ValueError`, so a row `corpus` that is not a code always conflicts) |
| `unknown_source_code` | `source_code` not declared in the vocabulary |
| `unknown_doc_type` | `doc_type` not declared in the vocabulary (a raw form such as `10-K` included) |
| `unknown_provenance` | `provenance` not declared in the vocabulary |
| `bad_language` | `language` not two lowercase letters (`english`, `EN`) |
| `bad_date` | `date` not an existing ISO `YYYY-MM-DD` day |
| `year_mismatch` | `year` not the integer year of `date`, or a `year` without `date` |
| `bad_date_precision` | `date_precision` of a dated row not `day`, `month` or `year` (`Day` included) |
| `bad_date_kind` | `date_kind` of a dated row not `publication`, `meeting` or `period` |
| `bad_local_path` | `local_path` not a plain relative POSIX path: absolute, a `scheme:` or drive prefix (`file:///…`, `C:`), a backslash, an empty, `.` or `..` segment (`raw//a.pdf`, `./a`, trailing `/`, a bare `data/`), a segment with leading or trailing whitespace, a control, format or line/paragraph separator character (Unicode Cc, Cf, Zl, Zp: U+0085, U+200B, U+2028, U+FEFF, ...), a first segment starting with `~`; after the transition, a `data/` prefix |
| `bad_sha256` | `sha256` not 64 lowercase hexadecimal characters |
| `entity_half` | one of `entity_scheme` / `entity_value` without the other; `field` names the missing one |
| `unknown_entity_scheme` | `entity_scheme` not declared in the vocabulary |
| `bad_extra` | an `extra` key or value a JSON database (Postgres JSONB, UTF-8) cannot store: a NUL character or a lone surrogate in a string or a key, a `NaN` or an infinity (Python's `json` writes them, JSON does not allow them), at any depth; one refusal per top-level key, `field` the key as written (a consumer storing it shows its repr when the key itself is the culprit) |

Across rows, `python -m vault_contract.check` also refuses `duplicate_doc_id`: every valid
occurrence of a `doc_id` before the last one within the corpus (the vault keeps the last).
Before any row, a non-blank line that is not a JSON object is refused as `not_json` by both
the checker and the vault ingestion (not a contract reason: such a line is not a row).

A field refused for presence or type is not judged further (a numeric `source_code` is
`bad_text`, not also `unknown_source_code`).

### Notice reasons

`quality_notices` returns one `Notice(reason, field, value)` per finding; `value` is the
value as the producer wrote it.

| Reason | When |
|---|---|
| `corpus_missing` | transition: no `corpus`; the ingesting service's `CORPUS` applies |
| `legacy_alias` | transition: an old key filled its column (`field` = `bank_code` or `pdf_url`) |
| `legacy_data_prefix` | `local_path` starts with `data/`: stripped during the transition, refused (`bad_local_path`) after it; noticed in both modes |
| `empty_title` | `title` absent, null or blank |
| `precision_not_stated` | `date` set, `date_precision` absent or null |
| `date_not_period_start` | `date_precision` `month` and the day is not 1, or `year` and the date is not 1 January |
| `precision_without_date` | `date_precision` set on an undated row (dropped by `split_row`) |
| `kind_without_date` | `date_kind` set on an undated row (dropped by `split_row`) |
| `mime_type_missing` | `local_path` set, `mime_type` absent |

`python -m vault_contract.check` counts both over a directory of manifests (see Usage).

## Vocabulary file

Each producer writes `vocabulary.json` at its data root, generated from its own code, and
validates it with `validate_vocabulary` in its CI:

```json
{"contract": "documents", "contract_version": "1.0.0", "corpus": "company",
 "source_codes": [{"code": "sec", "label": "SEC EDGAR"}],
 "doc_types": [{"code": "A1", "label": "Annual report (10-K)", "family": "A"}],
 "provenances": ["edgar_index", "edgar_fts", "edgar_submissions", "wayback"],
 "entity_schemes": ["cik"]}
```

- Every code (`corpus`, codes, `family`, provenances, entity schemes) matches
  `[A-Za-z0-9_.-]{1,32}`, case-sensitive; no duplicate within a list.
- `source_codes`, `doc_types` and `provenances` are non-empty lists; `entity_schemes` may be
  empty (a corpus whose documents concern no single company).
- `label` (non-blank, no control character such as NUL, a newline or a tab, no lone surrogate) is required for source codes and document types; `family` is
  optional.
- Unknown top-level keys are allowed.

The vault loads it into its `vocabulary` table before the documents, as a full replace per
corpus.

## Versioning

`MAJOR.MINOR.PATCH`, the package version being the contract version. The field table, the
closed value sets and **both reason lists are part of the contract**:

- **MAJOR**: a renamed or removed field, a new mandatory field, a changed meaning, and **a
  new refusal reason** or a stricter refusal (rows accepted yesterday would be refused),
  unless the release announces it as the end of a transition (the cleanup release flipping
  `transition`);
- **MINOR**: a new optional field, a new allowed value, **a new notice reason** (rows are
  still written);
- **PATCH**: documentation and fixes that change no accepted or refused row and no notice.

`bad_extra` and `too_long` are part of 1.0.0: both were added (the extra check moved from
the vault ingestion into the package) before 1.0.0 was deployed anywhere, so no producer saw
a release without them.

A reader accepts its own MAJOR and any MINOR at or below its own (`is_supported_version`): a
vocabulary declaring `1.1.0` is refused by a 1.0.x reader, which cannot know what 1.1 added.

## Golden examples

Why they exist, and what belongs in them:

1. **They are the executable definition of the contract.** Each case is a manifest row plus
   what the vault must do with it: the columns it becomes and the notices it raises (accepted
   rows), or the one reason it is refused for (refused rows). Prose can be read two ways; a
   case cannot.
2. **They ship inside the package**, so every producer (central-bank-corpus, company-corpus, a
   third party) and the data-orchestrator test against the very same cases the vault tests
   against, and CI runs `python -m vault_contract.check` over them on Python 3.10.
3. **The rule for what goes in: the minimum that proves the contract works.** One accepted row
   per real producer form (SEC 10-K, SEC Form 4, central-bank modern row, central-bank legacy
   on-disk form, central-bank undated row, central-bank rendered-HTML row), every notice
   triggered at least once by those rows, and exactly one refused case per refusal reason.
   More variants of a rule (other bad paths, other bad texts, ...) belong in the vault's unit
   tests (`tests/test_contract_validate.py`), not here.
4. **A new refusal reason or notice comes with its golden case in the same change**: the
   coverage tests fail otherwise (every reason has a refused case, every notice an accepted one).
5. **The URLs are real public addresses on purpose** (a realistic row); the hashes are
   placeholders, well-formed but not the real files' hashes.

## Golden rows

`vault_contract.golden` ships the reference cases every implementation is tested against, two
JSON Lines files (one case per line, `case` naming it):

- `accepted()` (`accepted.jsonl`): one row per real producer form, each with its
  `expected_corpus`, `row`, `expected_columns` (`split_row` output) and `expected_notices`:
  `sec_10k` (Apple 10-K FY2024, rendered PDF) and `cb_rate_decision` (ECB, `date_kind:
  meeting`) without notice; `sec_form4` (a Form 4 about Apple, insider as `filer_*` extras,
  text artefact without mime_type nor precision); `cb_legacy_form` (the pre-contract
  central-bank form: `bank_code`, `pdf_url`, no `corpus`, `data/raw/...`, a RePEc month date
  on the 12th, an empty title); `cb_undated` (no date, a leftover precision and kind);
  `cb_rendered_html` (ECB speech rendered from HTML: `application/pdf`, `html_path` extra,
  `sha256` of the HTML);
- `refused()` (`refused.jsonl`): exactly one case per refusal reason, with its
  `expected_corpus`, `expected_reasons` and, for a case judged as after the cleanup,
  `"transition": false`;
- `vocabulary("company")`, `vocabulary("central-bank")`: the vocabulary excerpts they are
  checked against.

## Vector stores

Collections are per corpus today, so a Qdrant point id derived from `doc_id` alone is unique.
**A collection mixing corpora must put `corpus` in its point ids**: `doc_id` is unique only
within its corpus, and two corpora's documents would otherwise overwrite each other's points.
