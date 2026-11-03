import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.ordixa import __version__
from app.ordixa.explain import (
    explain_reconciliation_profile,
    explain_table_profile,
    format_reconciliation_explanation,
    format_table_explanation,
)
from app.ordixa.inspection import (
    DEFAULT_MAX_ROWS,
    MAX_PREVIEW_ROWS,
    format_report,
    inspect_path,
)
from app.ordixa.issues import InputError
from app.ordixa.manifest import (
    MANIFEST_NAME,
    WORKBOOK_NAME,
    verify_run,
    write_run_outputs,
)
from app.ordixa.pipeline import ReconciliationIntegrityError, process
from app.ordixa.preflight import (
    PROHIBITED_HEADER,
    format_preflight,
    looks_personal,
    preflight,
)
from app.ordixa.profile import ProfileError, load_profile, read_profile_document
from app.ordixa.table_preflight import (
    format_table_preflight,
    read_headers,
    table_preflight,
)
from app.ordixa.tabular import (
    TabularIntegrityError,
    clean_table,
    compare_tables,
)
from app.ordixa.tabular_outputs import (
    CLEAN_ISSUES_NAME,
    CLEANED_NAME,
    REPORT_NAME,
    write_clean_run,
    write_compare_run,
)
from app.ordixa.tabular_profile import (
    is_table_profile_document,
    load_table_profile,
)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_INTEGRITY = 4
EXIT_OUTPUT = 5

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# tracked directories that a run must never write into
PROTECTED_DIRS = ("examples", "data", "tests", "app", "docs")
# git-ignored directories where customer files and run outputs may live
IGNORED_DIRS = ("output", "customer-data")


def report(message, stream=None):
    """
    prints a concise progress or error message
    :param message: text to print
    :param stream: output stream, defaults to standard output
    :returns: none
    """
    print(f"ordixa: {message}", file=stream or sys.stdout)


def is_within(path, parent):
    """
    checks whether a path is inside another directory
    :param path: candidate path
    :param parent: parent directory
    :returns: true when path is parent or a descendant
    """
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def check_output_location(out_dir, input_dir):
    """
    refuses output locations that would mix results with source data
    :param out_dir: requested output directory
    :param input_dir: input export directory
    :returns: none
    """
    if is_within(out_dir, input_dir):
        raise OSError(
            f"output directory {out_dir} is inside the input directory; "
            "choose a separate location"
        )
    for name in PROTECTED_DIRS:
        if is_within(out_dir, PROJECT_ROOT / name):
            raise OSError(
                f"output directory {out_dir} is inside the tracked {name}/ "
                "directory; choose a location such as output/"
            )
    if is_within(out_dir, PROJECT_ROOT) and not any(
        is_within(out_dir, PROJECT_ROOT / name) for name in IGNORED_DIRS
    ):
        raise OSError(
            f"output directory {out_dir} is inside the repository but not in a "
            "git-ignored folder; use output/ or a location outside the repository"
        )


def warn_about_input_location(input_dir):
    """
    warns when exports sit in the repository where they could be committed
    :param input_dir: input export directory
    :returns: none
    """
    if not is_within(input_dir, PROJECT_ROOT):
        return
    if any(
        is_within(input_dir, PROJECT_ROOT / name)
        for name in ("examples", "tests", *IGNORED_DIRS)
    ):
        return
    report(
        f"warning: {input_dir} is inside the repository and not git-ignored; keep "
        "customer exports outside the repository or in customer-data/",
        sys.stderr,
    )


def build_parser():
    """
    defines the command line interface
    :returns: argument parser
    """
    parser = argparse.ArgumentParser(
        prog="python -m app.ordixa",
        description=(
            "Ordixa: less busywork, more clarity. Profile-driven spreadsheet "
            "cleaning, dataset comparison, and order/payment reconciliation, "
            "with auditable run directories."
        ),
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ordixa {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name, help_text, description=None):
        return commands.add_parser(
            name,
            help=help_text,
            description=description or help_text[0].upper() + help_text[1:] + ".",
            epilog=EXAMPLES[name],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    run = command(
        "run",
        "reconcile an export directory and write a run directory",
        "Reads the orders, payments, and optional customers exports a "
        "reconciliation profile describes, reconciles payments to orders by "
        "exact order identifier, and writes an exceptions workbook, CSV "
        "artifacts, and a manifest into a new run directory. Inputs are only read.",
    )
    run.add_argument("--profile", required=True, type=Path, help="TOML profile")
    run.add_argument(
        "--input", required=True, type=Path, help="directory containing the exports"
    )
    run.add_argument(
        "--out",
        required=True,
        type=Path,
        help="parent directory; each run creates a new run-<id> folder inside it",
    )

    check = command(
        "check-profile",
        "validate a profile and preview it against exports, read-only",
        "Validates a profile of any kind. With --input (or --before/--after for "
        "a compare profile), also runs a read-only preflight that processes the "
        "files in memory and reports every missing file or column, unexpected "
        "or sensitive-looking header, normalization risk, and row-outcome "
        "count at once. --explain adds how the profile maps and normalizes each "
        "column. Nothing is written and no cell values are printed; exit code 3 "
        "means the preflight is blocked.",
    )
    check.add_argument("--profile", required=True, type=Path, help="TOML profile")
    check.add_argument(
        "--input",
        type=Path,
        help=(
            "export directory (reconciliation) or file (clean/compare) to "
            "preflight against the profile"
        ),
    )
    check.add_argument(
        "--before", type=Path, help="earlier export, to preview a comparison"
    )
    check.add_argument("--after", type=Path, help="later export, with --before")
    check.add_argument(
        "--explain",
        action="store_true",
        help=(
            "also explain the column mapping and normalization rules the profile "
            "applies; still read-only and never prints cell values"
        ),
    )
    check.add_argument(
        "--json", action="store_true", help="print the preflight report as JSON"
    )

    clean = command(
        "clean",
        "clean one CSV/XLSX table with a clean profile",
        "Maps, trims, and types every column the profile declares, removes "
        "exact duplicate rows, holds back rows with invalid or conflicting "
        "values, and writes cleaned_data.csv, cleaning_issues.csv, and a "
        "manifest into a new run directory. The input is only read.",
    )
    clean.add_argument("--profile", required=True, type=Path, help="TOML profile")
    clean.add_argument("--input", required=True, type=Path, help="CSV/XLSX file")
    clean.add_argument(
        "--out",
        required=True,
        type=Path,
        help="parent directory; each run creates a new run-<id> folder inside it",
    )

    compare = command(
        "compare",
        "compare two versions of a table by business key",
        "Cleans both files with the profile's rules, matches records by the "
        "declared business key only, and writes a change report workbook, CSV "
        "artifacts, and a manifest into a new run directory. Every comparison "
        "is assessed as complete, complete with warnings, or incomplete.",
    )
    compare.add_argument("--profile", required=True, type=Path, help="TOML profile")
    compare.add_argument("--before", required=True, type=Path, help="earlier export")
    compare.add_argument("--after", required=True, type=Path, help="later export")
    compare.add_argument(
        "--out",
        required=True,
        type=Path,
        help="parent directory; each run creates a new run-<id> folder inside it",
    )

    verify = command(
        "verify",
        "re-check a finished run directory against its manifest",
        "Read-only. Recomputes output checksums, sizes, CSV row counts, and "
        "optionally input checksums, and confirms workbooks have no formulas. "
        "Works for runs of every workflow, including runs written by earlier "
        "versions.",
    )
    verify.add_argument("--run", required=True, type=Path, help="run-<id> directory")
    verify.add_argument(
        "--input",
        type=Path,
        action="append",
        help=(
            "original export directory or file, to check inputs too; repeat it "
            "when a compare run read files from two places"
        ),
    )

    inspect = command(
        "inspect",
        "describe unfamiliar CSV/XLSX exports before writing a profile",
        "Read-only. Reports worksheets, headers, row counts, and suggested "
        "column types without printing cell values or writing any files.",
    )
    inspect.add_argument(
        "--input", required=True, type=Path, help="export file or directory"
    )
    inspect.add_argument(
        "--encoding", default="utf-8-sig", help="CSV text encoding (utf-8-sig)"
    )
    inspect.add_argument(
        "--max-rows",
        type=positive_int,
        default=DEFAULT_MAX_ROWS,
        help=f"data rows to scan per table (default {DEFAULT_MAX_ROWS})",
    )
    inspect.add_argument(
        "--preview",
        type=positive_int,
        default=0,
        metavar="ROWS",
        help=(
            f"show up to {MAX_PREVIEW_ROWS} rows redacted to character shapes "
            "(letters as A/a, digits as 9); off by default"
        ),
    )
    inspect.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


P = "examples/ordixa/profiles"
X = "examples/ordixa/exports"
EXAMPLES = {
    "inspect": f"""examples:
  python -m app.ordixa inspect --input {X}/text-ids
  python -m app.ordixa inspect --input {X}/inventory/inventory-before.csv --json""",
    "check-profile": f"""examples:
  python -m app.ordixa check-profile --profile {P}/harbor-lane.toml --explain
  python -m app.ordixa check-profile --profile {P}/harbor-lane.toml \\
      --input {X}/pilot/3-final-delivery
  python -m app.ordixa check-profile --profile {P}/inventory-clean.toml \\
      --input {X}/inventory/inventory-after.csv --explain
  python -m app.ordixa check-profile --profile {P}/inventory-compare.toml \\
      --before {X}/inventory/inventory-before.csv \\
      --after {X}/inventory/inventory-after.csv""",
    "run": f"""examples:
  python -m app.ordixa run --profile {P}/harbor-lane.toml \\
      --input {X}/problematic --out output/runs""",
    "clean": f"""examples:
  python -m app.ordixa clean --profile {P}/inventory-clean.toml \\
      --input {X}/inventory/inventory-before.csv --out output/clean""",
    "compare": f"""examples:
  python -m app.ordixa compare --profile {P}/inventory-compare.toml \\
      --before {X}/inventory/inventory-before.csv \\
      --after {X}/inventory/inventory-after.csv --out output/compare""",
    "verify": f"""examples:
  python -m app.ordixa verify --run output/runs/run-<id> --input {X}/problematic
  python -m app.ordixa verify --run output/compare/run-<id> \\
      --input {X}/inventory""",
}
MAIN_EPILOG = """workflows:
  reconcile  inspect -> check-profile --input DIR -> run -> verify
  clean      inspect -> check-profile --input FILE -> clean -> verify
  compare    check-profile --before A --after B -> compare -> verify

exit codes:
  0  success (rows may still have been rejected; read the summary)
  1  unexpected error; details withheld unless ORDIXA_DEBUG=1
  2  usage or profile error
  3  input error, or a blocked preflight
  4  integrity check failed; nothing was written
  5  output location refused or not writable

Run "python -m app.ordixa COMMAND --help" for examples. Docs: docs/ORDIXA.md
"""


def quoted(path):
    """
    formats a path for a command the operator can copy
    :param path: path
    :returns: text, double-quoted when it contains spaces
    """
    text = str(path)
    return f'"{text}"' if " " in text else text


def report_next_step(run_dir, inputs):
    """
    prints where the run is and the command that verifies it
    :param run_dir: published run directory
    :param inputs: input directories or files the run read
    :returns: none
    """
    locations = list(dict.fromkeys(inputs))
    report(f"run directory: {run_dir}")
    report(
        "verify before delivering: python -m app.ordixa verify --run "
        f"{quoted(run_dir)}"
        + "".join(f" --input {quoted(location)}" for location in locations)
    )


def positive_int(text):
    """
    parses a positive integer command line value
    :param text: argument text
    :returns: integer
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def inspect_command(args):
    """
    executes the read-only inspect subcommand
    :param args: parsed arguments
    :returns: exit code
    """
    try:
        reports = inspect_path(args.input, args.encoding, args.max_rows, args.preview)
    except LookupError:
        report(f"unknown encoding {args.encoding!r}", sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError:
        report(f"input error: {args.input} does not exist", sys.stderr)
        return EXIT_INPUT

    if args.json:
        print(json.dumps({"files": reports}, indent=2, ensure_ascii=False))
    else:
        print(format_report(reports, args.max_rows))
    return EXIT_INPUT if any(item["errors"] for item in reports) else EXIT_OK


def check_profile_command(args):
    """
    executes the check-profile subcommand and its optional preflight
    :param args: parsed arguments
    :returns: exit code
    """
    pair = (args.before, args.after)
    if any(pair) and not all(pair):
        report("--before and --after must be given together", sys.stderr)
        return EXIT_USAGE
    if all(pair) and args.input is not None:
        report("use either --input or --before/--after, not both", sys.stderr)
        return EXIT_USAGE
    if args.json and args.input is None and not all(pair) and not args.explain:
        report("--json needs --input, --before/--after, or --explain", sys.stderr)
        return EXIT_USAGE
    document, _ = read_profile_document(args.profile)
    if is_table_profile_document(document):
        return check_table_profile(args)
    if all(pair):
        report(
            "--before/--after preview a compare profile; this is a reconciliation "
            "profile, so use --input with the export directory",
            sys.stderr,
        )
        return EXIT_USAGE

    profile = load_profile(args.profile)
    explanation = explain_reconciliation_profile(profile) if args.explain else None
    if not args.json:
        # keeps --json output parseable as a single document
        report(f"profile {profile.name} {profile.version} is valid")
        if explanation is not None:
            print(format_reconciliation_explanation(explanation))
    if args.input is None:
        if args.json:
            print(json.dumps({"explain": explanation}, indent=2, ensure_ascii=False))
        return EXIT_OK

    warn_about_input_location(args.input)
    result = preflight(profile, args.input)
    if args.json:
        if explanation is not None:
            result["explain"] = explanation
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if explanation is not None:
            print()
        print(format_preflight(result))
    return EXIT_INPUT if result["readiness"] == "blocked" else EXIT_OK


def check_table_profile(args):
    """
    validates a clean/compare profile and optionally preflights it on files
    :param args: parsed check-profile arguments
    :returns: exit code
    """
    profile = load_table_profile(args.profile)
    if args.before is not None and profile.kind != "compare":
        report(
            f"--before/--after preview a comparison; this is a {profile.kind} "
            "profile, so use --input with one file",
            sys.stderr,
        )
        return EXIT_USAGE
    if args.before is not None:
        inputs = [("before", args.before), ("after", args.after)]
    elif args.input is not None:
        inputs = [("input", args.input)]
    else:
        inputs = []

    if not args.json:
        report(f"profile {profile.name} {profile.version} ({profile.kind}) is valid")
    for _, path in inputs:
        warn_about_input_location(path.parent)
    result = table_preflight(profile, inputs) if inputs else None
    explanation = None
    if args.explain:
        explanation = explain_table_profile(profile, read_headers(profile, inputs))

    if args.json:
        document = {}
        if args.input is not None and result["inputs"][0]["counts"] is not None:
            document = legacy_dry_run(profile, args.input)
        document.update(result or {})
        if explanation is not None:
            document["explain"] = explanation
        print(json.dumps(document, indent=2, ensure_ascii=False))
    else:
        if explanation is not None:
            print(format_table_explanation(explanation))
        if result is not None:
            if explanation is not None:
                print()
            print(format_table_preflight(result))
    if result is not None and result["readiness"] == "blocked":
        return EXIT_INPUT
    return EXIT_OK


def legacy_dry_run(profile, path):
    """
    builds the single-file JSON keys check-profile printed before 2.0.0
    :param profile: table profile
    :param path: input file
    :returns: row counts plus issue counts by code
    """
    result = clean_table(profile, path)
    issues = Counter(issue.code for issue in result.issues)
    return {**result.counts(), "issues": dict(sorted(issues.items()))}


def verify_command(args):
    """
    executes the read-only verify subcommand
    :param args: parsed arguments
    :returns: exit code
    """
    if not (args.run / MANIFEST_NAME).is_file():
        report(f"input error: {args.run / MANIFEST_NAME} does not exist", sys.stderr)
        report(
            "hint: --run takes one run-<id> directory, not its parent output folder",
            sys.stderr,
        )
        return EXIT_INPUT
    for location in args.input or ():
        if not location.exists():
            report(f"input error: {location} does not exist", sys.stderr)
            return EXIT_INPUT

    checks = verify_run(args.run, args.input)
    for description, passed in checks:
        print(f"  {'ok  ' if passed else 'FAIL'} {description}")
    failed = sum(1 for _, passed in checks if not passed)
    if failed:
        report(f"{failed} of {len(checks)} checks failed; do not deliver this run")
        return EXIT_INTEGRITY
    report(f"all {len(checks)} checks passed")
    if not args.input:
        report(
            "inputs were not checked; add --input with the original exports to "
            "confirm they are unchanged"
        )
    return EXIT_OK


def report_identical_inputs(inputs):
    """
    notes input files whose bytes are identical, such as a re-sent export

    their rows are exact duplicates, so each is counted once and the copies are
    listed as duplicate_row; the note explains why at the file level
    :param inputs: processed input files
    :returns: none
    """
    first = {}
    for item in inputs:
        key = (item.role, item.sha256)
        if key in first:
            report(
                f"note: {item.name} has exactly the same contents as "
                f"{first[key]}; its rows were counted once and listed as "
                "duplicate_row"
            )
        else:
            first[key] = item.name


def report_interrupted_runs(out_dir):
    """
    notes staging folders left by a run that was interrupted before publishing

    they are never results: a run is only published as run-<id> once every
    output and the manifest are complete
    :param out_dir: parent output directory
    :returns: none
    """
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return
    leftovers = sorted(
        path.name for path in out_dir.glob(".run-*.partial") if path.is_dir()
    )
    if leftovers:
        report(
            f"note: {len(leftovers)} incomplete folder(s) from an interrupted run "
            f"in {out_dir} ({', '.join(leftovers)}); they are not results and "
            "can be deleted",
            sys.stderr,
        )


def run_command(args):
    """
    executes the run subcommand
    :param args: parsed arguments
    :returns: exit code
    """
    started_at = datetime.now(timezone.utc)
    profile = load_profile(args.profile)
    report(f"profile {profile.name} {profile.version} is valid")

    if not args.input.is_dir():
        raise InputError(f"input directory not found: {args.input}")
    check_output_location(args.out, args.input)
    warn_about_input_location(args.input)
    result = process(profile, args.input)
    for item in result.inputs:
        report(f"read {item.role}: {item.name} ({item.data_rows} rows)")
    report_identical_inputs(result.inputs)
    if result.ignored_files:
        report(f"ignored {len(result.ignored_files)} file(s) matching no dataset")
    if "customers" not in result.records:
        report("no customers file provided; orders reconciled without customer data")

    report_interrupted_runs(args.out)
    run_dir, manifest = write_run_outputs(result, args.out, started_at)
    report_run(result, run_dir, manifest)
    report_next_step(run_dir, [args.input])
    return EXIT_OK


def warn_about_headers(header, profile):
    """
    warns, by column name only, about headers that suggest sensitive data

    this is the same header heuristic as the pilot preflight, not a privacy
    scan; unmapped columns are never copied into outputs
    :param header: source header names
    :param profile: table profile
    :returns: none
    """
    mapped = {column.source for column in profile.columns}
    for name in dict.fromkeys(header):
        if PROHIBITED_HEADER.search(name):
            where = (
                "it is mapped, so outputs will contain it"
                if name in mapped
                else "it was left out of the outputs"
            )
            report(
                f"warning: column {name!r} looks like card, bank, government ID, or "
                f"credential data; {where}. Ask for an export without it",
                sys.stderr,
            )
        elif name in mapped and looks_personal(name):
            report(
                f"note: mapped column {name!r} may hold personal data; outputs "
                "will contain it",
                sys.stderr,
            )


def prepare_table_run(args, kind, inputs):
    """
    loads a table profile and checks inputs and output location
    :param args: parsed arguments
    :param kind: clean or compare
    :param inputs: input file paths
    :returns: validated table profile
    """
    profile = load_table_profile(args.profile, kind)
    report(f"profile {profile.name} {profile.version} ({kind}) is valid")
    for path in inputs:
        if not path.is_file():
            raise InputError(f"input file not found: {path}")
        check_output_location(args.out, path.parent)
        warn_about_input_location(path.parent)
    return profile


def clean_command(args):
    """
    executes the clean subcommand
    :param args: parsed arguments
    :returns: exit code
    """
    started_at = datetime.now(timezone.utc)
    profile = prepare_table_run(args, "clean", [args.input])
    result = clean_table(profile, args.input)
    warn_about_headers(result.header, profile)
    counts = result.counts()
    report(f"read {result.input.name} ({counts['rows_read']} rows)")

    run_dir, manifest = write_clean_run(result, args.out, started_at)
    report(
        f"{counts['rows_written']} row(s) written, {counts['rows_rejected']} "
        f"rejected, {counts['duplicate_rows_removed']} exact duplicate(s) removed, "
        f"{counts['conflicting_keys']} conflicting key(s)"
    )
    report(f"cleaned data: {run_dir / CLEANED_NAME}")
    report(f"held-back rows and reasons: {run_dir / CLEAN_ISSUES_NAME}")
    report(f"manifest: {run_dir / MANIFEST_NAME} ({manifest['result']['status']})")
    report_next_step(run_dir, [args.input])
    return EXIT_OK


def compare_command(args):
    """
    executes the compare subcommand
    :param args: parsed arguments
    :returns: exit code
    """
    started_at = datetime.now(timezone.utc)
    profile = prepare_table_run(args, "compare", [args.before, args.after])
    result = compare_tables(profile, args.before, args.after)
    warn_about_headers(result.before.header + result.after.header, profile)
    for side in (result.before, result.after):
        report(
            f"read {side.input.role}: {side.input.name} ({side.input.data_rows} rows)"
        )

    run_dir, manifest = write_compare_run(result, args.out, started_at)
    counts = result.counts()
    report(
        f"{counts['added']} added, {counts['removed']} removed, "
        f"{counts['modified']} modified, {counts['unchanged']} unchanged"
    )
    report(
        f"{counts['ambiguous_keys']} ambiguous key(s), {counts['invalid_keys']} "
        f"invalid key(s), {counts['unkeyed_rejected_rows']} rejected row(s) "
        "without a key"
    )
    report_assessment(manifest["result"]["assessment"])
    report(f"change report: {run_dir / REPORT_NAME}")
    report(f"manifest: {run_dir / MANIFEST_NAME} ({manifest['result']['status']})")
    sources = (
        [args.before.parent]
        if args.before.parent.resolve() == args.after.parent.resolve()
        else [args.before, args.after]
    )
    report_next_step(run_dir, sources)
    return EXIT_OK


ASSESSMENT_TEXT = {
    "complete": "COMPLETE: every record was classified and every column compared",
    "complete_with_warnings": (
        "COMPLETE WITH WARNINGS: every record was classified; review the warnings"
    ),
    "incomplete": (
        "INCOMPLETE: some records or columns could not be compared; unchanged "
        "does not cover them"
    ),
}


def report_assessment(assessment):
    """
    prints how far a comparison can be trusted, without cell values
    :param assessment: dictionary from CompareResult.assessment
    :returns: none
    """
    report(f"comparison {ASSESSMENT_TEXT[assessment['level']]}")
    for item in assessment["findings"]:
        report(f"  {item['level']}: {item['message']}")


def report_run(result, run_dir, manifest):
    """
    prints the headline outcome of a finished run
    :param result: run result
    :param run_dir: run directory
    :param manifest: manifest dictionary
    :returns: none
    """
    profile = result.profile
    summary = result.summary
    counts = summary["status_counts"]
    currency = profile.amounts.currency
    report(
        f"{summary['orders']} orders reconciled: {counts['paid']} paid, "
        f"{counts['partial']} partial, {counts['unpaid']} unpaid, "
        f"{counts['overpaid']} overpaid"
    )
    report(
        f"net received {summary['net_received']} {currency}; outstanding "
        f"{summary['outstanding_balance']} {currency}"
    )
    report(
        f"{summary['rejected_rows']} row(s) rejected, {summary['warning_rows']} kept "
        f"with warnings, {summary['excluded_rows']} excluded by profile"
    )
    report(f"workbook: {run_dir / WORKBOOK_NAME}")
    report(f"manifest: {run_dir / MANIFEST_NAME} ({manifest['result']['status']})")


INPUT_HINTS = {
    "run": (
        "hint: check-profile --profile PROFILE --input DIR lists every problem in "
        "the batch at once, without writing anything"
    ),
    "clean": (
        "hint: check-profile --profile PROFILE --input FILE lists every problem "
        "at once, without writing anything"
    ),
    "compare": (
        "hint: check-profile --profile PROFILE --before FILE --after FILE lists "
        "every problem at once, without writing anything"
    ),
}


def main(argv=None):
    """
    command line entry point
    :param argv: optional argument list
    :returns: process exit code
    """
    args = build_parser().parse_args(argv)

    try:
        if args.command == "inspect":
            return inspect_command(args)
        if args.command == "check-profile":
            return check_profile_command(args)
        if args.command == "verify":
            return verify_command(args)
        if args.command == "clean":
            return clean_command(args)
        if args.command == "compare":
            return compare_command(args)
        return run_command(args)
    except ProfileError as error:
        report(str(error), sys.stderr)
        return EXIT_USAGE
    except InputError as error:
        report(f"input error: {error}", sys.stderr)
        hint = INPUT_HINTS.get(args.command)
        if hint:
            report(hint, sys.stderr)
        return EXIT_INPUT
    except (ReconciliationIntegrityError, TabularIntegrityError) as error:
        report(f"integrity check failed, no outputs written: {error}", sys.stderr)
        return EXIT_INTEGRITY
    except OSError as error:
        report(f"output error: {error}", sys.stderr)
        return EXIT_OUTPUT
    except Exception as error:  # last-resort guard so automation sees a code
        # library messages can quote cell values, so details are opt-in
        details = (
            f": {error}"
            if os.environ.get("ORDIXA_DEBUG")
            else " (details withheld because they may contain row values; set "
            "ORDIXA_DEBUG=1 to show them)"
        )
        report(f"unexpected error: {type(error).__name__}{details}", sys.stderr)
        report("no run directory was kept", sys.stderr)
        return EXIT_UNEXPECTED
