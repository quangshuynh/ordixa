# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Dates are UTC.

## [Unreleased]

### Fixed

- A CSV quote that is never closed, or one closed several lines later, no
  longer absorbs the following rows into a single cell. The affected lines
  are rejected together as `malformed_row` with their line range, in `run`,
  `check-profile --input`, `clean`, and `compare`.

### Added

- `run` notes input files with identical contents (their rows are counted
  once) and staging folders left by an interrupted run.
- The guided demo ends its workbook step with what Ordixa settled
  automatically and what still needs human review.

## [2.0.0] - 2026-10-17

Ordixa 2.0.0 expands the original financial reconciliation engine into a
broader business-data automation toolkit: profile-driven reconciliation,
spreadsheet cleaning, and dataset comparison, each with auditable run
directories. It is the first release of the Ordixa engine (`python -m
app.ordixa`); v1.0.0 contained only the original Business Data Automation
pipeline. Upgrade notes are in [docs/MIGRATING_TO_V2.md](docs/MIGRATING_TO_V2.md).

### Added

Introduced on `main` since v1.0.0 and first released here:

- **Profile-driven reconciliation** (Phase 1A, [#1]): versioned TOML import
  profiles with strict validation and spelling hints; CSV/XLSX adapters with
  several files per dataset; exact `Decimal` amounts; paid, partial, unpaid,
  and overpaid classification with refunds and adjustments; an Excel
  exceptions workbook; checksummed run manifests; `run` and `check-profile`.
- **Import hardening** (Phase 1B, [#2]): exact-text identifiers alongside
  numeric ones, identifier-collision and case-variant safeguards, the
  read-only `inspect` command, atomic run directories, and the
  [data-handling guide](docs/DATA_HANDLING.md).
- **Commercial demo** (Interval 3, [#3]): the Ordixa visual identity in the
  workbook, the guided `python -m app.ordixa.demo` on the fictional Harbor
  Lane dataset, and screenshots rendered from real runs.
- **Pilot readiness** (Interval 4, [#4]): the read-only pilot preflight
  (`check-profile --input`), the `verify` command, and the
  [pilot kit](docs/pilot/README.md).
- **Spreadsheet workflows** (Interval 5, [#5]): `clean` and `compare` with
  versioned table profiles, explicit column mapping, typed values,
  duplicate and conflicting-key handling, a five-sheet change report, and
  the guided inventory demo. See
  [docs/SPREADSHEET_WORKFLOWS.md](docs/SPREADSHEET_WORKFLOWS.md).

New in this release:

- **Profile preview for clean and compare profiles.** `check-profile --input
  FILE` is now a full read-only preflight: every missing configured column,
  unexpected headers, sensitive-looking headers, optional columns absent from
  the file, blank tokens that are also real numbers or dates, blank tokens in
  key columns, ambiguous date formats, decomposed Unicode, high rejection
  rates, and a `READY`/`REVIEW`/`BLOCKED` result.
- **Comparison preview.** `check-profile --before A --after B` runs the
  comparison in memory and reports its counts and result without writing
  anything.
- **`check-profile --explain`** for every profile kind: source-to-output
  column mappings, which configured headers each file has, and the active
  normalization rules. It prints no cell values and never changes the exit
  code.
- **Comparison result.** Every comparison is assessed as complete, complete
  with warnings, or incomplete, with findings that name columns and counts.
  It leads the change report's Summary sheet, is printed by `compare`, and is
  recorded in the manifest as `result.assessment`.
- **Release checks**: `python -m scripts.release_check` (version and tag
  consistency, changelog, documentation links, documented commands, secrets
  and customer data in tracked files) runs in CI, and a validation-only
  [release workflow](.github/workflows/release.yml) adds a clean
  installation, the demos from that installation, and a dependency audit.
  See [docs/RELEASING.md](docs/RELEASING.md).

### Improved

- Every command's `--help` ends with tested, copyable examples; the
  top-level help lists the three workflows and every exit code.
- `run`, `clean`, and `compare` finish with the run directory and the exact
  `verify` command for it; `clean` also names its issue report.
- Input errors point to the read-only preflight; `verify` explains what
  `--run` expects and says when inputs were not checked.
- The comparison change report's Summary sheet opens with the comparison
  result, written as wrapped text that stays inside the printed page.
- The inventory demo prints the comparison result.
- `python -m app.ordixa --version` reports `2.0.0` from the single
  `__version__` in `app/ordixa/__init__.py`.
- The README, reference guides, pilot checklist, and data-handling guide
  describe all three workflows; new [migration guide](docs/MIGRATING_TO_V2.md).

### Fixed

- A configured comparison column missing from one export was skipped with
  only a note, so records could be reported as unchanged without that column
  being compared; the comparison is now marked incomplete.
- Text values or keys differing only in Unicode form were reported as plain
  changes, or as an unexplained removed/added pair; both now carry notes
  (`unicode_form_differs`, `key_unicode_form_differs`) and a warning. Nothing
  is merged.
- Differences in columns a compare profile ignores were invisible; they are
  now counted in the comparison result.
- A mistyped `run --input` directory produced a misleading
  repository-location warning before the input error.
- The personal-data header note fired on names of things such as
  `Product Name`.

### Security and data handling

- `check-profile` blocks a clean or compare preflight when a header that
  suggests card, bank, government ID, or credential data is present, mapped
  or not.
- Preflight, `--explain`, and the comparison result print file names,
  headers, column names, issue codes, and counts, never cell values; tests
  plant sentinel values to check this.
- The release check refuses tracked `.env` files, `customer-data/`, run
  outputs, spreadsheets outside the example and test folders, common secret
  formats, and email addresses outside reserved example domains in tracked
  data.
- Unchanged from the engine's design: local processing with no network
  access, read-only inputs, formula-injection protection in every CSV and
  workbook cell, refused output paths inside tracked folders, atomic run
  publication, and withheld error details unless `ORDIXA_DEBUG=1`. Real
  customer files still require the owner-approved decisions in
  [docs/pilot/DATA_DECISIONS.md](docs/pilot/DATA_DECISIONS.md).

### Compatibility

- Reconciliation, clean, and compare profiles valid before 2.0.0 are valid
  and produce the same results; there is no profile schema change.
- `manifest_version` stays 1. Runs written before 2.0.0 still verify, and
  the same inputs reproduce their result fingerprints, deterministic CSV
  artifacts, and workbook values (pinned by
  `tests/test_ordixa_compatibility.py`). Compare manifests add
  `result.assessment`, outside the fingerprint.
- Command names, existing options, and exit code meanings are unchanged; the
  single-file `check-profile --json` report keeps its earlier keys.
- Behavior changes: a clean/compare preflight now exits 3 for
  sensitive-looking headers, and its text output is a preflight report.
- Against v1.0.0: the original `main.py` pipeline, API, and dashboard are
  still included. Since v1.0.0, `build_report` refuses duplicate identifiers
  instead of counting them twice, `customer_found` is true for matched
  customers with a blank name, and `openpyxl` is a runtime dependency.

### Known limitations

- Runs from a source checkout; not published to PyPI and no installer,
  container image, or executable for the Ordixa CLI.
- Exercised only on fictional data; no validated customer deployments.
- Reconciliation needs a shared order reference and covers only the files in
  one run.
- Clean and compare handle one table per run, match by exact business key
  only, and never resolve conflicting rows.
- Header checks are keyword heuristics, not a privacy scanner.

## [1.0.0] - 2026-08-20

First release, as **Business Data Automation**: a Python pipeline
(`python main.py`) that validates customer, order, and payment CSVs,
quarantines invalid records with reasons, reconciles payments, refunds, and
adjustments with cent-precision `Decimal` arithmetic, derives paid, partial,
unpaid, and overpaid statuses, writes reconciliation and quarantine CSV
reports, optionally persists to PostgreSQL, and serves a FastAPI service and
dashboard, with Docker Compose support, Ruff, GitHub Actions CI, and 52
tests. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

[2.0.0]: https://github.com/quangshuynh/ordixa/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/quangshuynh/ordixa/releases/tag/v1.0.0
[#1]: https://github.com/quangshuynh/ordixa/pull/1
[#2]: https://github.com/quangshuynh/ordixa/pull/2
[#3]: https://github.com/quangshuynh/ordixa/pull/3
[#4]: https://github.com/quangshuynh/ordixa/pull/4
[#5]: https://github.com/quangshuynh/ordixa/pull/5
