# Ordixa: handling customer files

Operating guidance for a controlled pilot in which Ordixa processes real
customer exports as a managed service. It describes conservative defaults,
not a compliance programme. Nothing here is a legal guarantee or a claim of
certification (SOC 2, ISO 27001, PCI DSS, GDPR, or otherwise). Items marked
**Owner decision** need an explicit choice by the business owner before the
first real file arrives, ideally recorded in writing with the customer.
They are collected, with suggested defaults, in the approval checklist
[pilot/DATA_DECISIONS.md](pilot/DATA_DECISIONS.md).

## What Ordixa does and does not do with data

- Runs locally on the operator's machine. It makes no network calls and has no
  upload endpoint, accounts, or database for profile runs.
- Reads exports and never modifies them.
- Writes one `run-<id>` directory per run: the workbook and `reconciliation_ledger.csv`
  contain customer names and emails; `rejected_rows.csv` contains offending
  source values; `manifest.json` contains file names, checksums, counts, and
  totals but no row values.
- Prints file names, counts, totals, and issue codes. It never prints row
  values unless `inspect --preview` (redacted to character shapes) or
  `ORDIXA_DEBUG=1` is used.
- For the spreadsheet [`clean` and `compare`](SPREADSHEET_WORKFLOWS.md)
  workflows, `cleaned_data.csv` holds every mapped column, the issue CSVs
  hold offending source values, and the comparison report and CSVs hold
  both the previous and the current value of every changed field. Treat
  these run directories exactly like reconciliation runs. Columns the
  profile does not map are never copied into any output, so leave
  unneeded columns unmapped. Their manifests hold no row values.

The [local workspace](WORKSPACE.md) follows the same rules. It listens
only on `127.0.0.1`, and keeps copies of the files you add, the settings,
and every run in its data directory (by default the git-ignored
`customer-data/workspace/`). Treat that directory like any folder of
customer exports and run outputs: the storage, retention, and deletion
guidance below applies to it, and *Remove* in the workspace deletes a piece
of work's folder.

## Receiving files

1. Agree in writing which exports are needed, for which period, and why.
   Ask for orders, payments, and (only if needed) customers. Nothing else.
2. Ask the customer to remove columns Ordixa does not use before sending:
   card details, full addresses, notes, tags, marketing consent, and tax IDs.
   Run `python -m app.ordixa inspect --input <file>` on arrival; it flags
   email and phone columns so unexpected personal data is noticed early.
   Then run `check-profile --profile <profile> --input <directory>`: the
   [pilot preflight](ORDIXA.md#pilot-preflight) flags unused columns whose
   headers suggest personal data, and blocks on headers that suggest card,
   bank, government ID, or credential data. For a clean or compare profile,
   `check-profile --input <file>` (or `--before`/`--after`) applies the same
   header checks and also blocks when such a header is mapped. These are
   header heuristics; they do not prove a file is minimized.
3. Accept files only through a channel with access control and encryption in
   transit, such as a shared folder restricted to named people or an
   encrypted archive whose password is sent through a different channel.
   Do not accept exports as ordinary email attachments or chat uploads.
   **Owner decision:** the approved transfer channel(s).
4. Never accept payment card numbers, bank account numbers, passwords, or
   government identifiers. If a file contains them, follow
   [Unexpected personal data](#unexpected-personal-data).

## Storing files

- Keep each customer's files in its own directory **outside the repository**,
  or in the repository's git-ignored `customer-data/<customer>/` folder.
  `customer-data/` and `output/` are in `.gitignore` and `.dockerignore`.
  The CLI refuses output inside the repository unless it is in `output/` or
  `customer-data/`, and warns when inputs sit in an unignored repository folder.
- Store input files and run directories on an encrypted disk (BitLocker,
  FileVault, or LUKS) with a screen lock and an individual operating-system
  account. **Owner decision:** whether full-disk encryption alone is
  sufficient or per-customer encrypted volumes are required.
- Restrict access to the people doing the work. Do not sync customer folders
  to personal cloud drives.
- **Owner decision:** backups. The conservative default is *no separate
  backups* of customer exports: the customer keeps the originals and can
  resend. If backups are needed, they must be encrypted, access-restricted,
  and covered by the same deletion schedule.

## Git and logs

- Never commit customer files, run directories, or screenshots of them.
  Use only fictional data in tests and `examples/`. Before every commit,
  check `git status` for unexpected CSV/XLSX/JSON files.
- Do not paste row values into issue trackers, chat, AI tools, or commit
  messages. Refer to rows by file name, row number, and issue code, which is
  what the workbook and `rejected_rows.csv` already provide.
- Do not keep terminal transcripts of runs that used `--preview` or
  `ORDIXA_DEBUG=1`.

## Delivering results

- Send the workbook through the same approved channel used to receive the
  exports. The workbook contains names, emails, and source values.
- Send only what the customer needs: usually the workbook alone. The CSV
  artifacts and manifest stay with the operator as the audit record.
- Before sending, run `python -m app.ordixa verify --run <run-dir> --input
  <directory>`; do not deliver a run with any failed check. The
  [delivery checklist](pilot/DELIVERY_CHECKLIST.md) covers the rest.
- State clearly that the workbook reconciles only the files in that run. It is
  not an accounting ledger, does not include earlier periods unless their
  orders were supplied, and does not replace the customer's bookkeeping.

## Retention and deletion

- Conservative default: delete input exports and run directories **30 days**
  after the workbook is delivered, unless the customer agrees otherwise in
  writing. Keep `manifest.json` (no row values) longer if an audit trail of
  what was processed is useful. **Owner decision:** the retention period and
  whether manifests are retained.
- Delete by removing the files and emptying the recycle bin or trash; on an
  encrypted disk this is a reasonable default. Record the deletion date per
  customer.
- When a customer ends the pilot, delete their files and confirm in writing.

## Failed runs

- A failed run leaves no `run-<id>` directory: outputs are written to a hidden
  `.run-<id>.partial` directory that is removed on failure. If a crash or
  power loss leaves a `.run-*.partial` directory behind, treat it as
  incomplete and delete it.
- Exit codes: 2 profile problem, 3 input problem, 4 integrity check failure,
  5 output location or disk problem, 1 unexpected error. Error messages name
  the file, row, column, and issue code without quoting values.
- An exit code of 4 means totals failed an independent cross-check. Do not
  deliver anything; keep the inputs and report the problem for investigation.
- To see details of an unexpected error, rerun privately with
  `ORDIXA_DEBUG=1`, then clear the terminal.

## Unexpected personal data

If an export contains data that was not requested (card numbers, bank
details, government IDs, health information, or many more people than
expected):

1. Stop processing that file. Do not open it further or forward it.
2. Tell the customer what was received, without repeating the values, and
   ask for a corrected export.
3. Delete the file and any copies, including run directories made from it.
4. Record what happened, when, and what was deleted.
5. **Owner decision:** when an incident must be escalated to legal advice or
   reported to the customer's own customers or a regulator. Get that advice
   before the pilot rather than during an incident.

## Checklist before the first real customer file

- [ ] Every owner decision in [pilot/DATA_DECISIONS.md](pilot/DATA_DECISIONS.md) approved and dated
- [ ] Transfer channel chosen and tested with a fictional file
- [ ] Encrypted storage location prepared, outside the repository or in `customer-data/`
- [ ] Retention period and backup decision agreed with the customer in writing
- [ ] Customer told which columns to remove before sending
- [ ] Export checked with `inspect` and the batch with `check-profile --input` (and `--explain`)
- [ ] Escalation contact for unexpected personal data identified
