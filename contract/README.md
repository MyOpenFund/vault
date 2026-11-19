# vault-contract

The MyOpenFund **`documents` contract**, version **1.0.0**: what a corpus writes on each line
of its manifests, what its vocabulary file declares, and how the vault checks both.

One contract for every corpus. Common fields have one name and one meaning everywhere and
become typed columns in the vault; corpus-specific fields travel as extras. The vault
ingestion enforces the contract; producers validate their own rows and vocabulary against it
in CI; data-orchestrator builds the RAG payload from the same split.

Standard library only, Python 3.12+.

## Install

Pin it by commit, from the `contract/` subdirectory of the vault repo:

```
vault-contract @ git+https://github.com/MyOpenFund/vault@<commit>#subdirectory=contract
```

In this repo, `pip install -r requirements-dev.txt` installs it in editable mode. The vault
ingestion image does not pip-install it: it copies `src/vault_contract` next to `ingest.py`.

## Usage

```python
import json

from vault_contract import load_vocabulary, quality_notices, split_row, validate_row

vocabulary = load_vocabulary(json.load(open("vocabulary.json")))   # ValueError if invalid
row = json.loads(manifest_line)
columns, extra = split_row(row)
refusals = validate_row(columns, vocabulary, expected_corpus="company")
# [] = accepted; else [Refusal(reason="bad_date_precision", field="date_precision", value="Day"), ...]
notices = quality_notices(columns, row=row)
# written either way; e.g. [Notice(reason="empty_title", field="title", value="")]
```

- `split_row(row) -> (columns, extra)`: `columns` holds the 18 common fields in DDL order
  (`None` when absent), `extra` every other key, values untouched. It never refuses.
- `validate_row(columns, vocabulary, *, expected_corpus) -> list[Refusal]`: every structural
  defect of the row. With `vocabulary=None` (a corpus with no vocabulary file yet) the code
  checks are skipped and the shape checks kept.
- `quality_notices(columns, *, row) -> list[Notice]`: what is worth counting but not
  refusing. It takes the original `row` too, because `split_row` drops the precision of an
  undated row and only the row still shows it.
- `validate_vocabulary(doc) -> list[str]` and `load_vocabulary(doc) -> Vocabulary`.
- `vault_contract.golden`: the reference rows below, shipped with the package.

## Fields

A manifest line is one JSON object. These 18 keys are the common fields:

| Field | Type | Required | Rule | central-bank example | company example |
|---|---|---|---|---|---|
| `doc_id` | text | yes | Non-empty string, no NUL, stable, **unique within its corpus**; the producer chooses its formula | `ceb04c0aa89ed888` (hash of bank, type, URL) | `cf0e49e6e176a851` (hash of CIK, form, accession) |
| `corpus` | text | yes | Must equal the ingesting service's `CORPUS` | `central-bank` | `company` |
| `source_code` | text | yes | The authority that publishes; declared in the corpus vocabulary | `ecb` | `sec` |
| `doc_type` | text | yes | Declared in the corpus vocabulary; meaningful only with `corpus` | `A1` (rate decision) | `A1` (10-K) |
| `title` | text | expected | A string, expected non-empty (absent or blank: notice `empty_title`) | `Monetary policy decisions` | `Apple Inc. 10-K (2024)` |
| `date` | date | no | ISO `YYYY-MM-DD`; a month or year date is the first day of the period | `2022-07-21` | `2024-11-01` (filing date) |
| `year` | integer | when `date` is set | The year of `date`; absent when `date` is | `2022` | `2024` |
| `date_precision` | text | no | `day` \| `month` \| `year`; null = not stated (notice `precision_not_stated` on a dated row); dropped on an undated row (notice `precision_without_date`) | `day` | `day` |
| `date_kind` | text | no | `publication` \| `meeting` \| `period` | `meeting` | `publication` |
| `language` | text | yes | ISO 639-1, two lowercase letters | `en` | `en` |
| `provenance` | text | yes | How the document was obtained; declared in the corpus vocabulary | `bank_site` | `edgar_index` |
| `document_url` | text | no | Address of the document itself (any type) | `https://www.ecb.europa.eu/press/pr/date/2022/html/ecb.mp220721~53e5bdd317.en.html` | `https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm` |
| `source_url` | text | no | Landing or index page it was found on | `https://www.ecb.europa.eu/press/pr/html/index.en.html` | `https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/0000320193-24-000123-index.htm` |
| `mime_type` | text | when `local_path` is set | Type of the file at `local_path` (a type known before any download is not refused) | `application/pdf` | `application/pdf` or `text/plain` |
| `sha256` | text | when a file was fetched | 64 lowercase hex: the hash of the document **as the authority served it**, not of a converted file | hash of the fetched PDF, or of the fetched HTML for a rendered row | hash of the primary document `aapl-20240928.htm` |
| `local_path` | text | no | POSIX path **relative to the corpus data root** (the directory holding `manifest/`): never absolute, no drive letter, no backslash, no `..` segment | `raw/ecb/C1/2023/e1e6a63950c50d3f.pdf` | `raw/0000320193/A1/2024/cf0e49e6e176a851.pdf` |
| `entity_scheme` | text | when the document concerns one company | `cik`, `lei`, or a register scheme declared in the corpus vocabulary | (absent) | `cik` |
| `entity_value` | text | with `entity_scheme` | The identifier in that scheme, as the scheme spells it | (absent) | `0000320193` |

Rules around the table:

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
- **Blank means absent.** In an optional field, `""` is read as absent (`None`). A blank
  required field is kept for the checks to judge (`bad_language`, `missing_doc_id`, ...;
  a blank title is only the notice `empty_title`).
- **No date, no precision.** `split_row` drops `date_precision` from an undated row;
  `date_kind` is kept.
- **Identity in the vault is (`corpus`, `doc_id`)**: two corpora may both have a document `1`.
- **`doc_type` belongs to its corpus.** central-bank `A1` (rate-decision press release) and
  company `A1` (10-K) share a code, not a meaning; a `doc_type` is always read with `corpus`.

### Transition (removed by the cleanup release)

Until every manifest is migrated, `split_row` also reads:

- `bank_code` as `source_code` and `pdf_url` as `document_url`, when the contract key is
  absent or blank; the old key never lands in `extra`;
- a `local_path` starting with `data/`, stored without that prefix.

The vault also accepts a row without `corpus` (the ingesting service's `CORPUS` applies) and a
corpus without a vocabulary file (code checks off).

## Two levels: refusals and notices

- A **refusal** is about structure or identity: the row is **not written**, and the vault
  skips its soft-delete sweep for that run. Example: `date_precision: "Day"` is refused
  (`bad_date_precision`); a closed value is spelled exactly.
- A **notice** is about quality: the row **is written** and the notice is counted. Example:
  a dated row with `date_precision: null` gets `precision_not_stated`; a producer that says
  nothing does not claim a day, so null means "not stated", never "day".

Both reason lists, in check order, are part of the contract: run reports count rows by reason.

### Refusal reasons

`validate_row` returns one `Refusal(reason, field, value)` per defect.

| Reason | When |
|---|---|
| `missing_doc_id` | `doc_id` absent or `""` |
| `missing_corpus` | `corpus` absent or `""`, once the transition is over (`transition=False`) |
| `missing_source_code` | `source_code` absent or `""` (checked with or without a vocabulary) |
| `missing_doc_type` | `doc_type` absent or `""` |
| `missing_language` | `language` absent or `""` |
| `missing_provenance` | `provenance` absent or `""` |
| `bad_text` | a text column set to something else than a non-blank string without NUL: a number (a CIK `320193` has lost its zeros), a list, an object, `"  "`, a NUL; `field` names the column |
| `corpus_conflict` | `corpus` present and not the ingesting service's corpus |
| `unknown_source_code` | `source_code` not declared in the vocabulary |
| `unknown_doc_type` | `doc_type` not declared in the vocabulary (a raw form such as `10-K` included) |
| `unknown_provenance` | `provenance` not declared in the vocabulary |
| `bad_language` | `language` not two lowercase letters (`english`, `EN`) |
| `bad_date` | `date` not an existing ISO `YYYY-MM-DD` day |
| `year_mismatch` | `year` not the integer year of `date`, or a `year` without `date` |
| `bad_date_precision` | `date_precision` of a dated row not `day`, `month` or `year` (`Day` included) |
| `bad_date_kind` | `date_kind` of a dated row not `publication`, `meeting` or `period` |
| `bad_local_path` | `local_path` not a plain relative POSIX path: absolute, a `scheme:` or drive prefix (`file:///…`, `C:`), a backslash, an empty, `.` or `..` segment (`raw//a.pdf`, `./a`, trailing `/`), leading or trailing whitespace, a control character, a first segment starting with `~` |
| `bad_sha256` | `sha256` not 64 lowercase hexadecimal characters |
| `entity_half` | one of `entity_scheme` / `entity_value` without the other |
| `unknown_entity_scheme` | `entity_scheme` not declared in the vocabulary |

A field refused for presence or type is not judged further (a numeric `source_code` is
`bad_text`, not also `unknown_source_code`).

### Notice reasons

`quality_notices` returns one `Notice(reason, field, value)` per finding; `value` is the
value as the producer wrote it.

| Reason | When |
|---|---|
| `corpus_missing` | transition: no `corpus`; the ingesting service's `CORPUS` applies |
| `legacy_alias` | transition: an old key filled its column (`field` = `bank_code` or `pdf_url`) |
| `legacy_data_prefix` | transition: `local_path` started with `data/`, stripped |
| `empty_title` | `title` absent, null or blank |
| `precision_not_stated` | `date` set, `date_precision` absent or null |
| `date_not_period_start` | `date_precision` `month` and the day is not 1, or `year` and the date is not 1 January |
| `precision_without_date` | `date_precision` set on an undated row (dropped by `split_row`) |
| `kind_without_date` | `date_kind` set on an undated row (dropped by `split_row`) |
| `mime_type_missing` | `local_path` set, `mime_type` absent |

`contract/scripts/check_manifests.py` counts both over a directory of manifests.

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
- `label` is required for source codes and document types; `family` is optional.
- `entity_schemes` may be empty (a corpus whose documents concern no single company).
- Unknown top-level keys are allowed.

The vault loads it into its `vocabulary` table before the documents, as a full replace per
corpus.

## Versioning

`MAJOR.MINOR.PATCH`, the package version being the contract version:

- **MINOR**: a new optional field, a new allowed value;
- **MAJOR**: a renamed or removed field, a new mandatory field, a changed meaning;
- **PATCH**: documentation and fixes that change no accepted or refused row.

A reader accepts its own MAJOR and any MINOR at or below its own (`is_supported_version`): a
vocabulary declaring `1.1.0` is refused by a 1.0.x reader, which cannot know what 1.1 added.

## Golden rows

`vault_contract.golden` ships the reference cases every implementation is tested against:

- `accepted()`: five reference rows without notice, `sec_10k` (Apple 10-K FY2024, rendered
  PDF), `sec_form4` (a Form 4 about Apple, insider as `filer_*` extras, text artefact),
  `cb_rate_decision` (ECB, `date_kind: meeting`, not downloaded yet), `cb_undated` (no date,
  no precision), `cb_rendered_html` (ECB speech rendered from HTML: `application/pdf`,
  `html_path` extra, `sha256` of the HTML); and one row per notice, each with its
  `expected_notices`: `cb_empty_title`, `cb_precision_not_stated`, `cb_precision_without_date`;
- `refused()`: one case per refusal reason at least, each with its `expected_corpus` and
  exact `expected_reasons`;
- `vocabulary("company")`, `vocabulary("central-bank")`: the vocabulary excerpts they are
  checked against.

Hashes in the golden rows are placeholders, well-formed but not the real files' hashes.

## Vector stores

Collections are per corpus today, so a Qdrant point id derived from `doc_id` alone is unique.
**A collection mixing corpora must put `corpus` in its point ids**: `doc_id` is unique only
within its corpus, and two corpora's documents would otherwise overwrite each other's points.
