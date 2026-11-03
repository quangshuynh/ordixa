# Ordixa reference: CLI and reconciliation

Detailed reference for the Ordixa command line and its reconciliation
workflow: taking the order and payment exports a small business already has,
mapping them through a declared import profile, and producing a reviewable
reconciliation workbook. The spreadsheet `clean` and `compare` workflows
share this CLI and are documented in
[SPREADSHEET_WORKFLOWS.md](SPREADSHEET_WORKFLOWS.md). Ordixa sits alongside
the original Business Data Automation pipeline and reuses its validation
helpers and reconciliation engine. The [README](../README.md) is the overview,
[DEMO.md](DEMO.md) the guided walkthrough, [DATA_HANDLING.md](DATA_HANDLING.md)
the procedure for real customer files, and
[MIGRATING_TO_V2.md](MIGRATING_TO_V2.md) what changed in 2.0.0.

Status: an early, tested foundation. It has been exercised only against the
fictional exports in [`examples/ordixa`](../examples/ordixa). It is not
production-ready, has no commercial validation, and does not claim to read
arbitrary Shopify, Square, Stripe, QuickBooks, or other vendor exports. Each
real export format needs a profile written and checked against sample files.

```text
customer export (CSV/XLSX)
    → profile mapping and strict normalization
    → canonical validated rows (errors / warnings / exclusions)
    → existing reconciliation engine (app/reconciliation)
    → independent totals cross-check
    → run directory: workbook, CSV artifacts, manifest
```

## Quick start

```bash
python -m pip install -r requirements.txt
python -m app.ordixa run --profile examples/ordixa/profiles/harbor-lane.toml   --input examples/ordixa/exports/demo --out output/ordixa
```

PostgreSQL is not required. `python -m app.ordixa.demo` runs the same
workflow as a guided, self-verifying walkthrough ([DEMO.md](DEMO.md)). Validate a profile without processing data:

```bash
python -m app.ordixa check-profile --profile examples/ordixa/profiles/harbor-lane.toml
```

Preflight a batch of exports against the profile before a real run
(read-only, see [Pilot preflight](#pilot-preflight)):

```bash
python -m app.ordixa check-profile --profile examples/ordixa/profiles/harbor-lane.toml   --input examples/ordixa/exports/pilot/3-final-delivery
```

Describe an unfamiliar export before writing a profile (read-only, see
[Inspecting exports](#inspecting-exports)):

```bash
python -m app.ordixa inspect --input examples/ordixa/exports/text-ids
```

Clean one table, or compare two versions of it by business key, with a
table profile instead of a reconciliation profile (see
[SPREADSHEET_WORKFLOWS.md](SPREADSHEET_WORKFLOWS.md)):

```bash
python -m app.ordixa clean --profile examples/ordixa/profiles/inventory-clean.toml   --input examples/ordixa/exports/inventory/inventory-before.csv --out output/inventory-clean
python -m app.ordixa compare --profile examples/ordixa/profiles/inventory-compare.toml   --before examples/ordixa/exports/inventory/inventory-before.csv   --after examples/ordixa/exports/inventory/inventory-after.csv --out output/inventory-comparison
```

| Command | Purpose |
| --- | --- |
| `run` | reconcile a directory of order, payment, and customer exports |
| `check-profile` | validate any profile; preflight it read-only with `--input` (or `--before`/`--after` for a compare profile); `--explain` shows the mapping and normalization rules |
| `inspect` | describe unfamiliar exports without printing cell values |
| `verify` | re-check a finished run against its manifest; `--input` may repeat |
| `clean` | standardize one CSV/XLSX table with a clean profile |
| `compare` | report added, removed, and modified records between two exports |

`python -m app.ordixa --version` prints the application version, and every
command's `--help` ends with copyable examples. Successful `run`, `clean`,
and `compare` commands finish with the run directory and the exact `verify`
command for it.

| Exit code | Meaning |
| --- | --- |
| 0 | Run completed (rejected rows are reported, not fatal); `inspect` read every file; preflight found nothing blocking |
| 1 | Unexpected internal error; details are withheld unless `ORDIXA_DEBUG=1` |
| 2 | Invalid profile or command usage |
| 3 | Input problem: missing directory, dataset, column, oversized or unreadable file; `inspect` could not read a file; preflight is blocked |
| 4 | Reconciliation or comparison integrity check failed, nothing written; `verify` found a mismatch |
| 5 | Output location refused or not writable; nothing kept |

`--out` is a parent directory. Output inside the input directory or inside the
tracked `examples/`, `data/`, `tests/`, `app/`, or `docs/` directories is
refused, as is any other repository location except the git-ignored
`output/` and `customer-data/` folders. Inputs inside the repository outside
those folders and `examples/` produce a warning. Input files are only read.

## Import profiles

Profiles are small TOML files (`schema_version = 1`). They are validated before
any data is read; every problem is reported at once with its key path, and
unknown keys are rejected with a spelling suggestion. The annotated example is
[`harbor-lane.toml`](../examples/ordixa/profiles/harbor-lane.toml).

| Section | Keys | Notes |
| --- | --- | --- |
| `[profile]` | `name`, `version`, `description` | name is lowercase-hyphenated; version is `MAJOR.MINOR.PATCH` |
| `[input]` | `formats`, `csv_encoding` | formats from `csv`, `xlsx`; encoding defaults to `utf-8-sig` |
| `[amounts]` | `currency`, `currency_symbols`, `thousands_separator`, `decimal_separator`, `max_decimal_places`, `negative_styles` | one ISO currency per profile; at most 2 decimal places; negatives as `minus` and/or `parentheses` |
| `[reconciliation]` | `missing_customer` | `warning` (default) or `error` |
| `[identifiers]` | `customer_id`, `order_id`, `payment_id` | `numeric` (default, Phase 1A) or `text`; see [Identifiers](#identifiers) |
| `[datasets.<role>]` | `required`, `files`, `sheet`, `optional_columns`, `date_formats`, `excluded_statuses`, `refund_amounts` | roles are `customers` (optional), `orders`, `payments` |
| `[datasets.<role>.columns]` | canonical field = source header | `name` may list several columns, joined with a space |
| `[datasets.<role>.id_prefixes]` | canonical id field = literal prefix | `#1001` → `1001`, `PMT-260801` → `260801`; the prefix must be present |
| `[datasets.payments.value_maps.status]` | source label = canonical status | targets: `paid`, `partial`, `pending`, `refunded` |
| `[datasets.payments.value_maps.transaction_type]` | source label = canonical type | targets: `payment`, `refund`, `adjustment` |

Canonical fields per role:

| Role | Required | Optional |
| --- | --- | --- |
| customers | `customer_id` | `name`, `email`, `phone` |
| orders | `order_id`, `date`, `total` | `customer_id`, `currency` |
| payments | `payment_id`, `order_id`, `amount`, `transaction_type`, `status` | `payment_date`, `currency` |

Deliberately unsupported: expressions, computed columns, conditional mappings,
multi-currency profiles, and fuzzy header matching. Value maps are matched
case- and whitespace-insensitively; a label that is not in the map is a
blocking error, never a guess. Without a value map only the canonical labels
themselves are accepted.

## Input requirements

- Files are found in the `--input` directory (not subdirectories) by each
  role's `files` patterns. Several files per role are concatenated, so two
  reporting periods can be processed in one run. A file matching two roles
  stops the run.
- The first row is the header. Mapped columns must exist exactly as named
  unless listed in `optional_columns`; a missing column stops the run and
  names the closest header found.
- CSV cells are read as text with no type inference. XLSX cells keep their
  native types: real date cells and numeric cells are used directly (they are
  not ambiguous), while text cells go through the same parsing as CSV. Only
  cached values of formula cells are read; formulas are never evaluated.
- Each input file is read into memory once (200 MB limit) so the recorded
  checksum covers exactly the bytes parsed. Split larger exports by period.
- Identifier rules depend on the profile's `[identifiers]` modes; see below.

## Identifiers

Each identifier family (`customer_id`, `order_id`, `payment_id`) has one mode
for the whole profile, so an order number is read identically in the orders
file and in payment references. Phase 1A profiles have no `[identifiers]`
table and keep the `numeric` mode; their results are byte-identical to
Phase 1A. The example [`juniper-row.toml`](../examples/ordixa/profiles/juniper-row.toml)
uses `text` for every family.

| | `numeric` (default) | `text` |
| --- | --- | --- |
| Accepted after prefix removal | digits only, positive, up to 2^63 − 1 | ASCII letters, digits, and `- _ . / : #`; first character a letter, digit, or `#`; at most 64 characters |
| Examples | `#1001` → `1001`, `00012345` → `12345` | `ORD-2026-00142`, `INV-A1049`, `PAY_8F3K2`, `SQ-2026-10-001`, `00012345` (all unchanged) |
| Leading zeros | dropped | kept |
| Letter case | n/a | kept; never folded |
| Rejected | letters, separators, decimals | spaces, commas, signs, non-ASCII, formula starters (`=`, `+`, `@`) |

Rules common to both modes:

- Only surrounding whitespace is removed, then the declared prefix, which must
  be present. Nothing else is stripped, folded, or reformatted.
- Identifiers never pass through floating point. A whole-number spreadsheet
  cell becomes its digits (`12345` or `12345.0` → `12345`); fractional,
  negative, boolean, or date cells, and floats above 2^53 that cannot be
  exact, are rejected with advice to format the column as Text.
- **Excel numeric cells.** If Excel already stored `00012345` as the number
  12345, the leading zeros are gone. Ordixa does not reconstruct them; in
  `text` mode the value is `12345`, will not match `00012345` in another file,
  and the mismatch is reported (`customer_not_found` or `order_not_in_run`).
  Ask for the column to be exported or formatted as Text. `inspect` warns
  about numeric identifier cells and leading zeros.
- **Collisions.** If two rows in a dataset spell their primary identifier
  differently but normalize to the same value (`#1001` and `#01001` under
  `numeric`), every such row is rejected as `identifier_collision`; dependent
  payments are rejected with `order_rejected`. A spreadsheet number and text
  with the same digits are the same spelling, not a collision.
- **Case variants.** In `text` mode `INV-A1049` and `inv-a1049` are different
  identifiers. Both rows are kept with an `identifier_case_variant` warning,
  and a payment or customer reference that matches only by case gets a hint
  naming the near match in its `order_not_in_run` or `customer_not_found`
  message.
- Ledgers sort identifiers naturally (`ORD-9` before `ORD-10`).

## Inspecting exports

`python -m app.ordixa inspect --input <file-or-directory>` describes CSV and
XLSX exports before a profile exists. It reports file formats, worksheet
names, headers, data-row counts, blank and distinct counts per column, and
**suggestions**: possible identifier, date (with the candidate `strptime`
formats that fit), amount (separator style, symbols, negative style, decimal
places), and personal-data (email, phone) columns. Notes flag leading zeros,
numeric identifier cells, numbers longer than Excel can hold, day/month
ambiguity, surrounding whitespace, and mixed values. File-level warnings
cover duplicate or blank headers, ragged rows, header-only files, likely
non-comma delimiters, wrong encodings, empty files, and corrupt, protected,
legacy (`.xls`), or unsupported files.

Suggestions are heuristics and are labelled as such; `inspect` never writes
a profile or guesses reconciliation rules. It is read-only, writes no files,
prints no cell values by default, scans at most `--max-rows` rows per table
(default 50,000) and files up to 200 MB, and gives identical output for
identical input. Options: `--encoding` for CSV text, `--json` for a
machine-readable report, and `--preview N` (opt-in, at most 10 rows) to show
rows redacted to character shapes (`Morgan Vale` → `Aaaaaa Aaaa`).

## Pilot preflight

`check-profile --profile <profile> --input <directory>` checks one batch of
exports against a profile before a real run. A failed `run` stops at the first
problem; preflight lists every problem at once so the customer can be asked
for everything in one message. It writes nothing and prints file names,
column headers, issue codes, and counts, never cell values.

| Check | Level |
| --- | --- |
| Input directory missing; required dataset missing; file matching two datasets | blocking |
| File above 200 MB, unreadable, missing the profile's worksheet, duplicate headers | blocking |
| Mapped column missing (closest header named) | blocking |
| Unused column whose header suggests card, bank, government ID, or credential data | blocking |
| Unused column whose header suggests personal data (address, email, notes, tags) | warning |
| File no dataset reads (renamed export, `.xls`, `.tsv`) | warning |
| More than 20% of a dataset's rows have values the profile cannot read | warning |
| Payments referencing an order not in the batch (`order_not_in_run`) | warning |
| Rows with a blank or different currency | warning |
| Other unused columns; optional dataset absent; no currency column mapped | note |

When nothing is blocking, the batch is processed in memory with the same code
as `run`, and preflight reports row outcomes per dataset, the order status
counts, and rejected, warned, and excluded rows grouped by dataset, field, and
issue code. The result is `BLOCKED` (exit 3), `REVIEW` (warnings, or rows that
would be rejected), or `READY` (exit 0 for both). `--json` prints the same
report as JSON.

Header checks are keyword heuristics: they can miss a sensitive column with
an unusual name and can flag a harmless one. A clean preflight is not proof
that the data is complete, minimized, or correct. Review the workbook and
verify totals independently after the run. The pilot walkthrough in
[pilot/SCENARIO.md](pilot/SCENARIO.md) shows preflight on three deliveries.

Add `--explain` to see how the profile will read the batch before trusting
it: each dataset's file patterns and worksheet, every source header mapped
to each canonical field (with identifier mode and removed prefix), date
formats, status and transaction-type labels, excluded statuses, refund
sign, amount rules, and the matching rules. It works with or without
`--input`, is included under `explain` with `--json`, prints no cell values,
and never changes the preflight's result or exit code.

## Normalization rules

**Dates.** Only the declared `strptime` formats are tried; four-digit years are
required. If a value parses under two declared formats to different dates
(`03/04/2026` with both `%m/%d/%Y` and `%d/%m/%Y`) it is rejected as ambiguous
and both readings are shown. Impossible dates (`2026-02-30`) are reported as
such. A timestamp with an offset keeps the calendar date written in the
export; nothing is shifted to UTC or another zone.

**Money.** Amounts are parsed into `Decimal` from text; floats are never used
for financial arithmetic. Thousands grouping must be well formed (`1,234.56`
is valid, `12,34` and `1.234,56` are not under a US profile), more decimal
places than allowed is an error, and only declared currency symbols are
stripped. A currency column that does not match the profile currency rejects
the row; currencies are never converted. A blank currency is a warning.

**Signs.** Payments and refunds must be positive after normalization;
adjustments are signed and must be non-zero. `refund_amounts = "negative"`
means refunds arrive as negative numbers and are stored as positive refund
amounts; a refund with the wrong sign is rejected.

## Validation severity

| Severity | Effect | Examples |
| --- | --- | --- |
| Error (blocking) | row rejected, not reconciled | invalid or missing identifier, amount, or order date; unmapped status or type; currency mismatch; refund sign; non-positive total; payment for a rejected order or an order not in this run; conflicting duplicate identifiers; malformed CSV row, including every line absorbed by an unbalanced quote |
| Warning | row kept and reconciled | malformed email, missing or non-10-digit phone, missing name, unknown or rejected customer reference (default), unreadable optional payment date, blank currency |
| Excluded | row omitted from totals on purpose | statuses listed in `excluded_statuses`, such as `Failed` or `Voided` |

**Behavior change versus the legacy pipeline.** In `main.py`, an invalid email
or phone quarantines the customer and, through it, every related order and
payment. In the Ordixa path those problems are warnings and the money is still
reconciled. `main.py` is unchanged because its PostgreSQL schema requires
non-null contact fields.

**Duplicates.** Duplicates are detected per dataset across every file in the
run, so a transaction exported in both monthly files is seen. Rows sharing an
identifier with identical content are counted once (the first by file name
and row is kept; later copies are listed as rejected). Rows sharing an
identifier with different content, such as an order whose total changed
between periods or one transaction ID exported as both a payment and a
refund, are all rejected, as are identifier collisions (above). Different
transaction IDs with identical amounts are separate events and both count;
Ordixa does not guess that they are duplicates.
The engine itself now refuses duplicate customer, order, or payment
identifiers, and every run cross-checks the ledger against independent sums of
accepted rows; a mismatch stops the run (exit code 4).

**Matching.** Payments are matched to orders only by the shared order
identifier. There is no date, amount, customer, or fuzzy matching.

**Customer relationships.** `customer_found` means the order's customer ID
joined to an accepted customer row; it does not depend on the customer
having a name. Missing names, emails, or phones are warnings and never stop
the money from being reconciled. A customer reference that does not join is
a warning or an error according to `missing_customer`.

## Financial correctness boundaries

- All parsing and arithmetic use `Decimal` at cent precision; amounts with
  more decimal places than the profile allows are rejected, never rounded.
- Single amounts are limited to 9,999,999,999.99; totals are exact at any size.
- Payments and refunds must be positive after the refund-sign rule;
  adjustments are signed and non-zero. Zero-value payments are rejected.
- Orders must have a positive total, so free (zero-total) orders are
  currently rejected rather than reconciled.
- Unknown statuses or transaction types are rejected; there is no default.
- Partial payments, overpayments, refunds, and negative adjustments are
  reflected in the status, outstanding balance, and overpayment columns.
- Every run cross-checks the ledger against independent sums of accepted
  rows and stops (exit 4) on any mismatch.

## Workbook

`ordixa_exceptions.xlsx` has four sheets:

| Sheet | Contents |
| --- | --- |
| Summary | a short guide to the sheets and row outcomes, order value, payments, refunds, signed adjustments, net received, amount reconciled to orders, outstanding balances, overpayments, exception counts, per-dataset row outcomes, processing metadata, input file fingerprints |
| Exceptions | unpaid, partially paid, and overpaid orders plus paid orders with discrepancy flags, with amounts, a plain-language "What to check" note, and transaction IDs |
| Rejected Rows | every issue: dataset, source file and row, record ID, outcome, whether it prevented processing, issue code, field, value, reason, original mapped values |
| Full Ledger | every reconciled order with payment, refund, and adjustment breakdown, net paid, balances, status, flags, transaction IDs, last payment date, and source location |

Totals are calculated in Python with `Decimal`, never by spreadsheet
formulas. Money is written as numeric cells with a two-decimal format, dates
as date cells holding the calendar date written in the export. Excel stores
numbers as binary doubles and the writer converts through float, so only
values with at most 15 significant digits are exact; any monetary value
longer than that (a total of 10,000,000,000,000.00 or more) is written as
exact right-aligned **text**, and the Summary sheet shows how many values
were stored this way. Order, customer, and record identifiers are always text
cells, so leading zeros and long numbers survive. Tables have frozen header
rows and auto-filters and stay usable when empty. Text from exports is stored
as string cells; values that could start a formula (`=`, `+`, `-`, `@`, tab,
CR) are prefixed with an apostrophe unless they are plain numbers such as
`-$89.00`, control characters are removed, and text longer than Excel's
32,767-character cell limit is truncated with a `[truncated]` marker (the CSV
artifacts keep the full text). The workbook contains no formulas, external
links, or macros.

Presentation follows [BRAND.md](BRAND.md): graphite headers, tinted status
chips with at least 7:1 contrast, wrapped free text with explicit row
heights, and a print layout one page wide with the header row repeated on
every page. Styling never changes a value.

## Run outputs and manifests

Each run creates `run-<UTC timestamp>-<random>` under `--out`. Files are
written into a hidden `.run-<id>.partial` directory that is renamed to
`run-<id>` only after the manifest is complete; if anything fails, the
staging directory is removed, so a `run-<id>` directory is always complete.

| File | Deterministic | Contents |
| --- | --- | --- |
| `ordixa_exceptions.xlsx` | no (run ID and timestamps) | workbook above |
| `reconciliation_ledger.csv` | yes | full ledger |
| `rejected_rows.csv` | yes | issues, without the original-values column |
| `manifest.json` | no | see below |

The manifest separates `result` (stable for identical inputs, profile, and
code: status, profile name/version/SHA-256 and identifier modes, code version
and git commit, input file names with the SHA-256 of the exact bytes parsed
and row counts, row and issue counts, row counts of each CSV artifact,
totals, and a `result_fingerprint` hashing the ledger, issues, and summary) from
`execution` (run ID, start/finish/reconciliation timestamps, directory name).
`outputs` lists each artifact's SHA-256 and whether it is deterministic. The
manifest holds no names, emails, phone numbers, or row values; it does hold
input file names, so avoid putting personal data in file names.

**Verification.** `verify_run()` in `app/ordixa/manifest.py` re-checks a run
directory against its manifest: each output's SHA-256 and size, the CSV row
counts, optionally each input's SHA-256, and the absence of workbook
formulas. The demo calls it as its last step, and operators can run it
before delivering a workbook:

```bash
python -m app.ordixa verify --run output/demo/run-<id> --input examples/ordixa/exports/demo
```

`verify` is read-only and prints one line per check. It exits 0 when every
check passes, 4 when any fails (do not deliver that run), and 3 when the run
directory has no manifest.

**Reruns.** Re-running the same inputs creates a new directory; earlier runs
are never overwritten. Two runs of identical inputs with the same profile and
code produce an identical `result` section and byte-identical CSV artifacts.

## Fictional fixtures

[`examples/ordixa`](../examples/ordixa) contains a clean baseline and a
problematic set, each split into August and September 2026 periods, a
text-identifier set, and the October 2026 demo month (CSV plus XLSX), all
with expected results computed by hand. Tests compare implementation output with
those hand-entered values in `tests/ordixa_golden.py`.

## Known limitations

- **No cross-run history.** Each run reconciles only the files it is given. A
  payment for an order from an earlier export is rejected as
  `order_not_in_run`; include both periods' order files in the same run
  instead. Nothing is persisted between runs.
- **Shared identifiers required.** Exports without a common order reference
  cannot be reconciled in this phase.
- **Not an accounting ledger.** A workbook covers only the files in its run.
- **Identifiers are exact.** Case, separators, and leading zeros must match
  between files in `text` mode; leading zeros lost by Excel are not restored.
- **Zero-total orders** (fully discounted orders) are rejected.
- **No duplicate guessing.** Repeated identical amounts with different
  transaction IDs both count; a vendor that re-issues IDs on re-export will
  double count and must be handled by supplying each transaction once.
- **Inspect heuristics.** `inspect` suggestions can be wrong; they never feed
  processing. Formula cells are read only through cached values.
- **File size and formats.** Inputs above 200 MB are refused; legacy `.xls`,
  `.xlsm`, `.xlsb`, `.ods`, and non-comma delimited files are unsupported.
- **One currency per run**, no conversion.
- **Profile import path does not write to PostgreSQL**, and the API and
  dashboard still read only from the legacy persisted data.
- The fixtures imitate vendor styles; real exports have more columns, more
  statuses, and edge cases these profiles have not seen.
- Source status mapping is per label; it cannot depend on transaction type.

## Data handling

Exports contain personal data. Ordixa runs locally, sends nothing over the
network, and does not modify inputs. The workbook and `rejected_rows.csv`
include customer names, emails, and offending values because reviewers need
them; store run directories with the same care as the exports. `output/` and
`customer-data/` are git-ignored. Do not commit real customer exports to this
repository. Operating guidance for real customer files is in
[DATA_HANDLING.md](DATA_HANDLING.md).

## History

Phase 1A (profile-driven imports) and Phase 1B (import hardening) are merged
into `main`. Phase 1A profiles work unchanged; their results are identical.
Interval 4 added the pilot preflight, the `verify` command, and the
[pilot kit](pilot/README.md); it did not change reconciliation results.
Interval 5 added the [spreadsheet `clean` and `compare`
workflows](SPREADSHEET_WORKFLOWS.md) with their own table profiles; it did
not change reconciliation results, manifests, or fingerprints.
Release 2.0.0 brought these together as one product and added the read-only
table preflight and `--explain`, the comparison result
(complete/warnings/incomplete), clearer CLI summaries, and release checks. It
did not change reconciliation, cleaning, or comparison results; see
[MIGRATING_TO_V2.md](MIGRATING_TO_V2.md) and the [changelog](../CHANGELOG.md).

## Roadmap (not implemented)

1. Starter profiles validated against real, consented sample exports.
2. Per-dataset currency.
3. Optional persisted import history so later payments can be matched to
   earlier orders explicitly and auditably, only if pilot evidence justifies
   it ([cross-run assessment](pilot/CROSS_RUN_ASSESSMENT.md)).
4. Workbook review loop: carry reviewer notes between runs.
5. Packaging (`pip install` entry point) once the CLI stabilizes.

Hosted multi-tenant service, accounts, billing, scheduling, and integrations
are out of scope until the import foundation is proven with real data.
