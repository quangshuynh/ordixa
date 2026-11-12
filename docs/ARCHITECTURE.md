# Architecture and legacy pipeline

This repository contains two entry points that share one reconciliation
engine:

- **Ordixa** (`python -m app.ordixa`): profile-driven CSV/XLSX imports, an
  Excel exceptions workbook, and run manifests. It is documented in
  [ORDIXA.md](ORDIXA.md) and demonstrated in [DEMO.md](DEMO.md).
- **The original Business Data Automation pipeline** (`python main.py`): fixed
  CSV inputs, quarantine files, optional PostgreSQL persistence, a read-only
  FastAPI service, and a dashboard. It is unchanged and documented below.

Ordixa does not write to PostgreSQL. The API and dashboard read only data that
the legacy pipeline persisted.

## Module layout

```mermaid
flowchart LR
    Exports[CSV/XLSX exports] --> Ordixa[Ordixa CLI and import profile]
    Ordixa --> Reconciliation
    Ordixa --> Workbook[Exceptions workbook and manifest]
    CSV[Customer, order, and payment CSVs] --> CLI[Legacy batch pipeline]
    CLI --> Validation[Validation and normalization]
    Validation -->|invalid records| Quarantine[Quarantine CSVs]
    Validation -->|valid records| Reconciliation[Reconciliation rules]
    Reconciliation --> Report[Reconciliation CSV]
    Validation -->|DATABASE_URL configured| Database[(PostgreSQL)]
    Database --> Service[Reconciliation service]
    Service --> Reconciliation
    Database --> API[FastAPI]
    Service --> API
    API --> Dashboard[Reconciliation dashboard]
```

Application responsibilities are separated by runtime role:

```text
app/
├── api/             HTTP routes, dependencies, and response schemas
├── db/              SQLAlchemy models, sessions, and persistence
├── ordixa/          profile-driven imports, spreadsheet clean/compare, workbooks, manifests, CLI
├── workspace/       local browser workspace over the Ordixa CLI (server, storage, static page)
├── reconciliation/  financial calculations and report construction
├── services/        database-to-business-logic coordination
├── validators/      structural and record-level validation
└── config.py        environment configuration

main.py              CSV batch entry point and pipeline orchestration
```

`app/ordixa` adds a second batch entry point (`python -m app.ordixa run`, plus
the guided `python -m app.ordixa.demo`) that reuses the validators' primitives
and the same reconciliation engine; it does not change `main.py` behavior.
Within it, `profile.py` validates profiles, `adapters.py` reads files,
`mapping.py` and `normalize.py` turn source rows into canonical rows,
`validation.py` applies cross-row rules, `pipeline.py` reconciles and
cross-checks totals, `workbook.py` and `manifest.py` write the run directory
(and back the read-only `verify` command), `inspection.py` backs the read-only
`inspect` command, and `preflight.py` backs `check-profile --input` by reusing
discovery, header checks, and the pipeline in memory.

The spreadsheet [`clean` and `compare` workflows](SPREADSHEET_WORKFLOWS.md)
sit beside reconciliation rather than inside it: `tabular_profile.py`
validates table profiles with the same TOML helpers, `tabular.py` cleans one
table and compares two by business key using the shared readers and exact
parsers, and `tabular_outputs.py` and `change_report.py` write run
directories through the same atomic publication, manifest, and workbook
helpers that `verify` already checks. `tabular.py` also assesses each
comparison as complete, complete with warnings, or incomplete.
`table_preflight.py` backs `check-profile` for table profiles (one file, or a
before/after pair processed in memory), and `explain.py` backs
`check-profile --explain` for both profile kinds. `inventory_demo.py` is
their guided demo. None of them touch the reconciliation engine.

`app/workspace` is the [local workspace](WORKSPACE.md), started with
`python -m app.ordixa workspace`. It is a layer over the engine, not a second
implementation: `server.py` (FastAPI, loopback only) serves a plain
HTML/CSS/JavaScript page from `static/`; `store.py` keeps each piece of work
in its own folder under the git-ignored data directory; `preview.py`
describes added files using `inspection.py`; `profiles.py` builds profile
documents from the choices on screen and validates them through TOML with
the engine's own loaders (`toml_writer.py` writes the TOML); the check step
calls `preflight` and `table_preflight`; `jobs.py` runs the unchanged CLI
(`clean`, `compare`, `run`) in a child process so runs can be stopped and
every run directory is the CLI's; and `results.py` reads finished runs back
from their manifests and CSV artifacts, with `verify_run` behind the
verification badge.

```mermaid
flowchart LR
    Browser[Browser page] -->|127.0.0.1 only| Server[Workspace server]
    Server --> Store[(Session folders)]
    Server --> Inspect[inspection]
    Server --> Profiles[Profile loaders]
    Server --> Preflight[preflight / table_preflight]
    Server --> Jobs[Child process: python -m app.ordixa clean / compare / run]
    Jobs --> Runs[run-id directories and manifests]
    Server --> Verify[verify_run]
    Verify --> Runs
```

`scripts/capture_screenshots.py` is a maintainer tool that regenerates the
documentation images from a real demo run. `scripts/release_check.py` is the
release gate described in [RELEASING.md](RELEASING.md).

The application version lives in one place, `__version__` in
`app/ordixa/__init__.py`; `--version`, manifests, and workbooks read it from
there.

The API and batch workflow share the same reconciliation implementation. Database code stays outside `main.py`, while API routes delegate cross-layer reconciliation work to a focused service rather than duplicating calculations.

## Legacy pipeline: validation strategy

```text
load CSV data
    ↓
validate dataset structure
    ↓
normalize and validate records
    ↓
quarantine invalid records
    ↓
persist valid records when configured
    ↓
build reconciliation report
    ↓
write report and quarantine outputs
```

Structural failures stop the run because the dataset cannot be interpreted safely:

- Missing required columns
- Duplicate customer, order, or payment IDs

Record-level failures are quarantined so valid business data can continue:

- Invalid email or phone
- Missing customer name
- Malformed order date
- Invalid order total
- Invalid transaction amount, type, or source status
- Missing or quarantined foreign-key parent

Invalid records remain in CSV quarantine files rather than PostgreSQL. Each rejected record is emitted in its normalized form with a `validation_errors` explanation, while validated records continue through the pipeline.

## Legacy pipeline: financial reconciliation

The calculated `financial_status` is derived from the order total and net transaction amount. It is never copied from the source payment `status`.

| Net paid amount | Financial status |
| --- | --- |
| Zero or less | `unpaid` |
| Greater than zero but less than the order total | `partial` |
| Equal to the order total | `paid` |
| Greater than the order total | `overpaid` |

Each payment record has a required `transaction_type`:

| Transaction type | Amount rule | Reconciliation effect |
| --- | --- | --- |
| `payment` | positive | increases net paid |
| `refund` | positive | decreases net paid |
| `adjustment` | non-zero and signed | applies its sign directly |

The report includes the net `amount_paid`, signed `balance_due`, non-negative `outstanding_balance`, `overpayment_amount`, `payment_count`, `financial_status`, discrepancy flags, and a UTC reconciliation timestamp.

Current discrepancy flags identify:

- Orders with no payments
- Overpaid orders
- Negative net transaction amounts
- Source payments marked paid when the net amount is unpaid or partial

## Legacy pipeline: example result

The included sample data contains a completed payment followed by a refund:

```text
order total:       129.99
payment:          +129.99
refund:            -10.00
net amount paid:   119.99
outstanding:        10.00
financial status: partial
```

Runtime-generated files are written under `output/`:

```text
output/reconciliation_report.csv
output/invalid_customers.csv
output/invalid_orders.csv
output/invalid_payments.csv
```

The runtime directory is ignored by Git because the reconciliation timestamp changes on every run. Stable examples from the included input data are committed under [`examples/output`](../examples/output).

## Legacy pipeline: running the CSV workflow

Prerequisites:

- Python 3.14
- PowerShell commands below, or equivalent commands for your shell

Create an environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run the CSV-only pipeline without configuring a database:

```powershell
python main.py
```

This validates the files under `data/` and writes results under `output/`.

## PostgreSQL setup (legacy pipeline)

PostgreSQL is optional for the batch workflow and required for API data endpoints.

1. Create a PostgreSQL database and application user.
2. Copy the environment template:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Replace the placeholder credentials in `.env`. The connection must use the Psycopg SQLAlchemy dialect:

   ```text
   DATABASE_URL=postgresql+psycopg://business_data_user:your_password@localhost:5432/business_data_automation
   ```

4. Run the pipeline:

   ```powershell
   python main.py
   ```

When configured, the pipeline creates missing tables and persists valid customers, orders, and transactions in one database transaction. Existing primary keys are merged, making repeated imports safe from duplicate-key failures. Database failures roll back and are logged without preventing CSV report generation.

Persistence uses incremental primary-key upserts. It does not treat each import as a complete snapshot, so records absent from a later input file are not automatically deleted.

To initialize empty tables without processing CSV files:

```powershell
python -c "from app.db.database import initialize_database; initialize_database()"
```

Secrets are read from `DATABASE_URL`; `.env` is ignored by Git and `.env.example` contains placeholders only.

### Existing database migration note

The payment transaction model requires `payments.transaction_type` and associated check constraints. SQLAlchemy `create_all()` creates the correct schema for new databases but does not alter an existing table. Existing databases must be migrated or recreated when their data is disposable.

For disposable Docker development data:

```powershell
docker compose down --volumes
docker compose up --build
```

## FastAPI

With `DATABASE_URL` configured, start the API:

```powershell
uvicorn app.api.api:api --reload
```

Interactive OpenAPI documentation is available at `http://127.0.0.1:8000/docs`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | application liveness check |
| GET | `/ready` | database readiness check |
| GET | `/customers` | list persisted customers |
| GET | `/orders` | list persisted orders |
| GET | `/payments` | list persisted financial transactions |
| GET | `/reconciliation` | calculate reconciliation from persisted data |

The liveness endpoint does not require PostgreSQL. The readiness and data endpoints return HTTP 503 when configuration or database access is unavailable.

The API returns customer contact data and has no authentication. Treat it as a local demonstration service until authentication and authorization are added.

## Dashboard (legacy pipeline)

FastAPI serves a lightweight dashboard at `http://127.0.0.1:8000/dashboard/`,
and the application root redirects there.

Dashboard features include:

- Total orders, net amount paid, outstanding balance, and flagged-order summaries
- Paid, partial, unpaid, and overpaid counts
- Responsive reconciliation table
- Order and customer search
- Financial-status and flagged-order filters
- Discrepancy messages and connection-error handling

The dashboard is plain HTML, CSS, and JavaScript served by the backend. This
keeps the demonstration easy to run without adding a separate Node.js toolchain.

<p align="center">
  <img src="images/dashboard.png" width="900"
       alt="Legacy reconciliation dashboard with summary cards, status counts, and a filterable reconciliation table">
</p>
<p align="center"><em>Legacy dashboard served by FastAPI from data persisted by <code>main.py</code>.
It does not display Ordixa runs.</em></p>

## Docker

Docker Compose starts PostgreSQL and FastAPI, waits for the database health check, initializes missing tables, and stores PostgreSQL data in a named volume. Required database variables fail fast when they are absent.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open `http://localhost:8000/docs` after startup.

Open `http://localhost:8000/dashboard/` for the dashboard.

Load the sample CSV data and regenerate the mounted output files:

```powershell
docker compose run --rm app python main.py
```

Stop containers while preserving database data:

```powershell
docker compose down
```

Local Python development remains supported without Docker.

## Testing

Install the development dependencies once:

```powershell
python -m pip install -r requirements-dev.txt
```

Run the complete suite and the style checks:

```powershell
python -m pytest
ruff check .
ruff format --check .
```

The suite covers:

- Dataset and record validation
- Quarantine behavior and validation messages
- Payment, refund, and adjustment reconciliation
- Full, partial, and overpayment edge cases
- SQLAlchemy models and database constraints
- Transaction rollback and primary-key upsert behavior
- FastAPI endpoints and expected failures
- Complete CSV-to-output pipeline integration
- Ordixa profile validation, date/amount normalization, CSV and XLSX inputs,
  text and numeric identifiers, identifier collisions, warning-versus-error
  severity, duplicate and referential safeguards, the inspect command,
  workbook precision and contents, formula-injection protection, manifests,
  failed-run cleanup, reruns, and CLI exit codes, with financial totals
  checked against hand-computed fixtures
- The fictional demo dataset against hand-computed results, the guided demo
  command, run verification (including tampered outputs and inputs), and the
  workbook's contrast ratios, guidance text, fonts, and print layout
- The pilot preflight and `verify` command, and the fictional pilot scenario
  against hand-computed results, including a bounded two-month window and
  scans that keep example data and tracked files fictional
- Spreadsheet cleaning and comparison: table profiles, trimming, blank
  tokens, column mapping, exact duplicates versus conflicting keys, leading
  zeros, exact decimals, dates, Unicode, reordered rows and columns, compound
  keys, null-versus-empty rules, formula injection, deterministic outputs and
  fingerprints, atomic runs, verification, and a 20,000-row comparison, with
  the fictional inventory comparison checked against hand-entered results
- Comparison integrity (missing, ignored, and blank-token columns, Unicode
  lookalikes, header changes, and the complete/warnings/incomplete result),
  the read-only table preflight and `--explain`, CLI help examples, summaries,
  and exit codes
- Compatibility with runs written before 2.0.0: stored run directories in
  `tests/fixtures/runs-0.1.0` must still verify, and the current engine must
  reproduce their fingerprints, deterministic artifacts, and workbook values
- The local workspace: workspace runs reproduce engine runs exactly (result
  fingerprints and deterministic checksums), saved settings work with the
  CLI, downloads are the verified bytes and changed outputs are refused,
  amounts stay exact text, profiles round-trip through TOML and the engine
  loaders, refused, oversized, damaged, and duplicate files, blocked checks,
  stopped, timed-out, failed, and interrupted runs, and the security model
  (host check, cookie and token, cross-site refusal, headers, no external
  resources, no HTML rendering of file contents, safe file names, path
  confinement, size limits, loopback binding)
- The release gate: version and tag consistency, changelog, documentation
  links and commands, and tracked-file checks for secrets and customer data

Database and API integration tests use isolated in-memory SQLite databases, so PostgreSQL and Docker are not required to run the standard suite. A live PostgreSQL/Compose smoke test remains a deliberate next step.

Ruff formatting, Ruff linting, pytest, the release checks, and CLI and demo smoke tests run in [GitHub Actions](../.github/workflows/ci.yml) on every push and pull request. [Release validation](../.github/workflows/release.yml) reruns CI for release branches and tags and adds a clean runtime installation, the demos from that installation, and a dependency audit; it has read-only permissions and never publishes.

## Engineering decisions

- Structural errors fail fast; isolated record errors are quarantined
- Financial truth comes from transaction amounts, not source status labels
- Refunds are positive source amounts with a negative reconciliation effect
- Financial values are validated and reconciled with `Decimal` values at cent precision
- The reconciliation engine refuses duplicate identifiers that a merge would count twice
- Valid records are persisted transactionally; invalid records remain explainable CSV artifacts
- PostgreSQL persistence is optional so the original batch workflow stays useful
- ORM, API, validation, reconciliation, and orchestration responsibilities remain separate
- The design avoids repository interfaces, dependency-injection frameworks, and class hierarchies that the current complexity does not justify

## Legacy pipeline: limitations and future improvements

Ordixa-specific limitations (no cross-run order history, shared order
identifiers required, single currency, zero-total orders rejected) are listed in
[ORDIXA.md](ORDIXA.md#known-limitations).

- Payments do not yet include transaction dates, so last-payment-date reporting is unavailable
- Schema changes currently require manual migration or database recreation; Alembic would be the next database maturity step
- The read-only API has no authentication, pagination, or authorization and is intended for local demonstration
- API reconciliation currently loads complete tables into Pandas and is best suited to small datasets
- Persistence performs upserts rather than full snapshot synchronization or import history
- Standard tests use SQLite; live PostgreSQL, Docker runtime, and browser behavior are not yet tested in CI
- Docker runtime is verified locally, but full Docker and live PostgreSQL integration are not yet exercised in CI
- Dashboard summaries currently use a fixed USD display currency
