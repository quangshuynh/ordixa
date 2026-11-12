"""
reads finished run directories back for the screen

everything shown comes from the files the CLI wrote: the manifest for counts
and totals, and the CSV artifacts for rows. Values are passed through as the
exact text in those files (amounts are never turned into floating point), and
the screen renders them as plain text
"""

import csv
import json
from pathlib import Path

from app.ordixa.adapters import file_sha256
from app.ordixa.manifest import MANIFEST_NAME

MAX_PAGE = 500
OUTPUT_DESCRIPTIONS = {
    "cleaned_data.csv": "The cleaned table: every row that was safe to keep.",
    "cleaning_issues.csv": "Every row held back or removed, with the reason.",
    "comparison_report.xlsx": "The change report workbook to read or share.",
    "added_records.csv": "Records that are new in the later file.",
    "removed_records.csv": "Records that are gone from the later file.",
    "modified_values.csv": "Every value that changed, before and after.",
    "comparison_issues.csv": "Rows that could not be compared, with the reason.",
    "ordixa_exceptions.xlsx": "The exceptions workbook to read or share.",
    "reconciliation_ledger.csv": "Every order with what was paid and what is owed.",
    "rejected_rows.csv": "Rows held back or kept with a warning, with the reason.",
    "manifest.json": "The run record: inputs, settings, counts, and checksums.",
}

# plain-language explanations of the engine's row codes: what happened and
# what to do about it
EXPLANATIONS = {
    "missing_value": (
        "A required value is empty.",
        "Fill it in, or mark the column as not required.",
    ),
    "missing_identifier": ("The ID is empty.", "Every row needs its ID filled in."),
    "missing_date": ("The date is empty.", "Fill in the date in the source file."),
    "missing_amount": (
        "The amount is empty.",
        "Fill in the amount in the source file.",
    ),
    "missing_currency": ("The currency is empty.", "Fill in the currency code."),
    "invalid_identifier": (
        "This doesn't look like an ID.",
        "IDs use letters, numbers, and - _ . / : # with no spaces.",
    ),
    "invalid_amount": (
        "This isn't an amount Ordixa can read exactly.",
        "Check for text, extra symbols, or a different decimal style.",
    ),
    "invalid_transaction_amount": (
        "This amount doesn't fit the kind of transaction.",
        "Payments and refunds must not be zero; check the sign.",
    ),
    "too_many_decimal_places": (
        "This amount has more decimal places than allowed.",
        "Round it in the source, or check it was exported correctly.",
    ),
    "amount_out_of_range": (
        "This amount is too large.",
        "Check it was exported correctly.",
    ),
    "invalid_integer": (
        "This isn't a whole number.",
        "Use digits only, with an optional minus sign.",
    ),
    "invalid_date": (
        "This date isn't written in a format you chose.",
        "Add its format in the date settings, or fix the date.",
    ),
    "ambiguous_date": (
        "This date could be read two ways.",
        "Keep only one of the day-first or month-first formats.",
    ),
    "invalid_calendar_date": (
        "This date doesn't exist.",
        "Fix it, for example 31 April.",
    ),
    "malformed_row": (
        "This row is broken: it has the wrong number of cells.",
        "Often a stray quote mark or comma. Fix the row and export again.",
    ),
    "duplicate_row": (
        "This row is an exact copy of an earlier one.",
        "It was counted once. Nothing to do unless the copy was intended.",
    ),
    "conflicting_key": (
        "Two or more rows share this ID but disagree.",
        "None of them were used. Decide which is right and keep only that row.",
    ),
    "conflicting_duplicate_id": (
        "Two or more rows share this ID but disagree.",
        "None of them were used. Decide which is right and keep only that row.",
    ),
    "key_on_rejected_row": (
        "Another row with this ID was held back.",
        "Fix the held-back row so this record can be used as one.",
    ),
    "identifier_collision": (
        "Two different IDs read as the same one.",
        "For example 007 and 7. Use exact text IDs.",
    ),
    "identifier_case_variant": (
        "This ID differs from another only in upper/lower case.",
        "Check whether they are the same record.",
    ),
    "currency_mismatch": (
        "This row is in a different currency.",
        "Ordixa handles one currency per run.",
    ),
    "non_positive_total": ("The order total is zero or negative.", "Check the order."),
    "excluded_status": (
        "Left out on purpose: this status moved no money.",
        "Nothing to do. Failed or voided payments are listed, not counted.",
    ),
    "order_not_in_run": (
        "This payment is for an order that isn't in these files.",
        "Add the export that holds the order, or check the order reference.",
    ),
    "order_rejected": (
        "This payment's order was held back.",
        "Fix the order row first.",
    ),
    "missing_customer_reference": (
        "The customer for this order isn't in the customer file.",
        "The order was still matched. Add the customer if needed.",
    ),
    "refund_sign": ("This refund's sign is unusual.", "Check it is a refund."),
    "invalid_email": ("The email address looks wrong.", "Kept; fix it at the source."),
    "invalid_phone": ("The phone number looks wrong.", "Kept; fix it at the source."),
    "missing_phone": ("The phone number is empty.", "Kept; nothing to do."),
    "missing_email": ("The email address is empty.", "Kept; nothing to do."),
    "optional_column_missing": (
        "A column you chose is not in this file.",
        "Its values are blank. Check you added the right export.",
    ),
    "unmapped_columns": (
        "Some columns were left out of the results on purpose.",
        "Only the columns you chose are kept.",
    ),
    "column_not_compared": (
        "A column is missing from one file, so it was not compared.",
        "Changes in it are not shown.",
    ),
    "unicode_form_differs": (
        "Two values look the same but are stored differently.",
        "Usually an accent typed two ways; usually safe to ignore.",
    ),
}


def explain(code):
    """
    explains a row code in plain language
    :param code: engine issue code
    :returns: dictionary with what happened and what to do
    """
    what, fix = EXPLANATIONS.get(code, ("", ""))
    return {"code": code, "what": what, "fix": fix}


def read_manifest(run_dir):
    """
    reads a run's manifest
    :param run_dir: run-<id> directory
    :returns: dictionary
    """
    return json.loads((Path(run_dir) / MANIFEST_NAME).read_text("utf-8"))


def workflow_of(manifest):
    """
    names a run's workflow; reconciliation manifests predate the field
    :param manifest: manifest dictionary
    :returns: clean, compare, or reconcile
    """
    return manifest["result"].get("workflow", "reconcile")


def summary(run_dir):
    """
    describes a finished run from its manifest
    :param run_dir: run-<id> directory
    :returns: dictionary
    """
    manifest = read_manifest(run_dir)
    result = manifest["result"]
    workflow = workflow_of(manifest)
    outputs = [
        {
            "file": item["file"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
            "description": OUTPUT_DESCRIPTIONS.get(item["file"], ""),
        }
        for item in manifest["outputs"]
    ]
    outputs.append(
        {
            "file": MANIFEST_NAME,
            "bytes": (Path(run_dir) / MANIFEST_NAME).stat().st_size,
            "sha256": None,
            "description": OUTPUT_DESCRIPTIONS[MANIFEST_NAME],
        }
    )
    data = {
        "run": Path(run_dir).name,
        "workflow": workflow,
        "status": result["status"],
        "profile": result["profile"],
        "inputs": result["inputs"],
        "started_at": manifest["execution"]["started_at"],
        "finished_at": manifest["execution"]["finished_at"],
        "code_version": result["code_version"],
        "fingerprint": result["result_fingerprint"],
        "artifact_rows": result["artifact_rows"],
        "outputs": outputs,
        "issue_counts": result["issue_counts"],
    }
    if workflow == "clean":
        data["counts"] = result["row_counts"]
    elif workflow == "compare":
        data["counts"] = result["comparison"]
        data["sides"] = result["row_counts"]
        data["assessment"] = result.get("assessment")
        data["compared_columns"] = result["compared_columns"]
        data["ignored_columns"] = result["ignored_columns"]
    else:
        data["counts"] = result["row_counts"]
        data["totals"] = result["totals"]
        data["currency"] = result["currency"]
    return data


def csv_outputs(run_dir):
    """
    lists the CSV artifacts a manifest records
    :param run_dir: run-<id> directory
    :returns: set of file names
    """
    manifest = read_manifest(run_dir)
    return {o["file"] for o in manifest["outputs"] if o["file"].endswith(".csv")}


def table(run_dir, name, offset=0, limit=100, match=None, exclude=None):
    """
    reads a page of rows from one CSV artifact, exactly as written
    :param run_dir: run-<id> directory
    :param name: artifact file name listed in the manifest
    :param offset: rows to skip
    :param limit: rows to return, at most MAX_PAGE
    :param match: optional {column: value} filters, all of which must match
    :param exclude: optional {column: value} filters, none of which may match
    :returns: dictionary with header, rows, and the matching row count
    """
    if name not in csv_outputs(run_dir):
        raise KeyError(name)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_PAGE))
    rows, total = [], 0
    with (Path(run_dir) / name).open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        positions = {
            header.index(column): value
            for column, value in (match or {}).items()
            if column in header
        }
        excluded = {
            header.index(column): value
            for column, value in (exclude or {}).items()
            if column in header
        }
        for cells in reader:
            if any(cells[i] != value for i, value in positions.items()):
                continue
            if any(cells[i] == value for i, value in excluded.items()):
                continue
            if offset <= total < offset + limit:
                rows.append(cells)
            total += 1
    return {
        "file": name,
        "header": header,
        "rows": rows,
        "total": total,
        "offset": offset,
    }


def issue_groups(run_dir, name):
    """
    groups an issue CSV by code for the "needs attention" summary
    :param run_dir: run-<id> directory
    :param name: issue artifact name
    :returns: list of groups, largest first
    """
    groups = {}
    with (Path(run_dir) / name).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            code = row.get("code", "")
            outcome = row.get("outcome", "")
            key = (outcome, code)
            group = groups.setdefault(
                key, {"outcome": outcome, "code": code, "rows": 0, **explain(code)}
            )
            group["rows"] += 1
    order = {"Rejected": 0, "Kept with warning": 1, "Removed duplicate": 2}
    return sorted(
        groups.values(),
        key=lambda g: (order.get(g["outcome"], 3), -g["rows"], g["code"]),
    )


def output_matches_manifest(run_dir, name):
    """
    confirms a file about to be downloaded is still the file the run wrote
    :param run_dir: run-<id> directory
    :param name: output file name
    :returns: true when its checksum and size match the manifest
    """
    manifest = read_manifest(run_dir)
    for item in manifest["outputs"]:
        if item["file"] == name:
            path = Path(run_dir) / name
            return (
                path.is_file()
                and path.stat().st_size == item["bytes"]
                and file_sha256(path) == item["sha256"]
            )
    return False
