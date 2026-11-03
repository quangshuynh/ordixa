"""
deterministic cleaning and keyed comparison of one table

cleaning maps source columns onto the profile's output columns, trims and
normalizes values by declared type, and holds back every row it cannot use
as-is with a reason; comparison cleans both versions with the same rules and
matches records only by the explicit business key
"""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.ordixa.adapters import malformed_message, read_table
from app.ordixa.issues import InputError
from app.ordixa.normalize import (
    MAX_EXACT_FLOAT_ID,
    NormalizationError,
    identifier_sort_key,
    identifier_spelling,
    parse_amount,
    parse_date,
    parse_text_identifier,
)

REJECTED = "Rejected"
DUPLICATE_REMOVED = "Removed duplicate"
NOTE = "Note"
OUTCOMES = (REJECTED, DUPLICATE_REMOVED, NOTE)

ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"
UNCHANGED = "unchanged"
AMBIGUOUS = "ambiguous"
INVALID = "invalid"
CHANGE_KINDS = (ADDED, REMOVED, MODIFIED, UNCHANGED, AMBIGUOUS, INVALID)

# how far a comparison can be trusted: complete means every record was
# classified and every configured column compared; warnings mean the result is
# complete but has differences the categories alone do not show; incomplete
# means some records or columns could not be compared, so "unchanged" does not
# cover them
COMPLETE = "complete"
COMPLETE_WITH_WARNINGS = "complete_with_warnings"
INCOMPLETE = "incomplete"
ASSESSMENT_LEVELS = (COMPLETE, COMPLETE_WITH_WARNINGS, INCOMPLETE)

# report order: single-file runs, then the earlier and later export
SIDE_ORDER = {"input": 0, "before": 1, "after": 2, "both": 3}
KEY_SEPARATOR = " / "
INTEGER_PATTERN = re.compile(r"^-?[0-9]+$")

# explanations never quote cell values; the offending value, when one is
# needed to fix the source, is kept in the issue's separate value field
MESSAGES = {
    "missing_value": "required value is blank",
    "invalid_identifier": (
        "not a valid identifier (letters, digits, and - _ . / : #, starting with "
        "a letter, digit, or #; no spaces)"
    ),
    "invalid_amount": "not an amount in the profile's declared format",
    "too_many_decimal_places": "has more decimal places than the profile allows",
    "amount_out_of_range": "amount is outside the supported range",
    "invalid_integer": "not a whole number (digits with an optional leading minus)",
    "invalid_date": "does not match any declared date format",
    "ambiguous_date": "matches several declared date formats with different dates",
    "invalid_calendar_date": "is not a real calendar date",
    "malformed_row": "row has a different number of cells than the header; skipped",
}


class TabularIntegrityError(RuntimeError):
    """raised when cleaning or comparison counts do not add up"""


@dataclass(frozen=True)
class TableIssue:
    """
    a finding about one source row, or about the whole file when row is 0

    rejected rows are left out of the cleaned output and the comparison;
    removed duplicates were identical to an earlier row that was kept
    """

    side: str
    file: str
    row: int
    outcome: str
    code: str
    message: str
    key: str = ""
    column: str = ""
    source_column: str = ""
    value: str = ""

    def sort_key(self):
        """
        provides a deterministic report order
        :returns: tuple used for sorting
        """
        return (
            SIDE_ORDER.get(self.side, 9),
            self.file,
            self.row,
            self.code,
            self.column,
        )


@dataclass(frozen=True)
class Record:
    """one accepted row: typed values by output column, and its business key"""

    row: int
    values: dict
    key: tuple | None


@dataclass(frozen=True)
class InputFile:
    """what was read from one input file"""

    role: str
    name: str
    sha256: str
    data_rows: int


@dataclass
class CleanResult:
    """outcome of cleaning one file"""

    profile: object
    input: InputFile
    header: tuple
    records: list
    issues: list
    unmapped_columns: list
    missing_columns: list
    duplicate_keys: set = field(default_factory=set)
    conflicting_keys: set = field(default_factory=set)
    rejected_keys: set = field(default_factory=set)
    unkeyed_rejections: int = 0
    stats: dict = field(default_factory=dict)
    # (column, token) -> cells blanked; kept out of counts() so result
    # fingerprints match runs written before it existed
    blank_hits: Counter = field(default_factory=Counter)

    @property
    def inputs(self):
        """
        lists the files read, for manifests
        :returns: list of input files
        """
        return [self.input]

    @property
    def unresolved_keys(self):
        """
        business keys that cannot be used as one authoritative record
        :returns: set of keys with conflicting or rejected rows
        """
        return self.conflicting_keys | self.rejected_keys

    def counts(self):
        """
        summarizes row outcomes without any cell values
        :returns: dictionary of counts
        """
        rejected_rows = {i.row for i in self.issues if i.outcome == REJECTED}
        return {
            "rows_read": self.input.data_rows,
            "rows_written": len(self.records),
            "rows_rejected": len(rejected_rows),
            "duplicate_rows_removed": sum(
                1 for i in self.issues if i.outcome == DUPLICATE_REMOVED
            ),
            "duplicate_keys": len(self.duplicate_keys),
            "conflicting_keys": len(self.conflicting_keys),
            "unmapped_columns": len(self.unmapped_columns),
            "missing_optional_columns": len(self.missing_columns),
            **self.stats,
        }


@dataclass(frozen=True)
class ValueChange:
    """one column whose value differs between the two versions"""

    key: tuple
    column: str
    before: object
    after: object
    difference: Decimal | int | None


@dataclass
class CompareResult:
    """outcome of comparing two versions of a table"""

    profile: object
    before: CleanResult
    after: CleanResult
    compared_columns: list
    skipped_columns: list
    status: dict
    added: list
    removed: list
    changes: list
    issues: list
    ignored_differences: dict = field(default_factory=dict)
    unicode_changes: int = 0
    unicode_key_pairs: int = 0

    @property
    def inputs(self):
        """
        lists the files read, for manifests
        :returns: list of input files
        """
        return [self.before.input, self.after.input]

    def assessment(self):
        """
        says whether the comparison is complete, complete with warnings, or
        incomplete, and why, using counts and column names only
        :returns: dictionary with level and findings
        """
        return assess_comparison(self)

    def keys_with(self, kind):
        """
        lists business keys in one change category, in natural key order
        :param kind: a CHANGE_KINDS value
        :returns: list of keys
        """
        return [key for key, value in self.status.items() if value == kind]

    def counts(self):
        """
        summarizes the comparison without any cell values
        :returns: dictionary of counts
        """
        totals = {kind: 0 for kind in CHANGE_KINDS}
        for kind in self.status.values():
            totals[kind] += 1
        by_column = defaultdict(int)
        for change in self.changes:
            by_column[change.column] += 1
        return {
            "before_rows": self.before.input.data_rows,
            "after_rows": self.after.input.data_rows,
            "before_records": len(self.before.records),
            "after_records": len(self.after.records),
            "added": totals[ADDED],
            "removed": totals[REMOVED],
            "modified": totals[MODIFIED],
            "unchanged": totals[UNCHANGED],
            "ambiguous_keys": totals[AMBIGUOUS],
            "invalid_keys": totals[INVALID],
            "unkeyed_rejected_rows": (
                self.before.unkeyed_rejections + self.after.unkeyed_rejections
            ),
            "changed_values": len(self.changes),
            "changed_values_by_column": {
                column: by_column[column]
                for column in self.compared_columns
                if by_column[column]
            },
        }


def key_text(key):
    """
    formats a business key for reports
    :param key: tuple of key parts
    :returns: display text such as SKU-0042 or NORTH / 000123
    """
    return "" if key is None else KEY_SEPARATOR.join(key)


def key_sort(key):
    """
    orders keys naturally so SKU-9 sorts before SKU-10, part by part
    :param key: tuple of key parts
    :returns: comparable tuple
    """
    return tuple(identifier_sort_key(part) for part in key)


def as_text(value):
    """
    gives the canonical text of a cleaned value, used for output and comparison
    :param value: typed cleaned value
    :returns: text, empty for blank
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def parse_integer(value):
    """
    parses a whole number such as a stock quantity without float rounding
    :param value: raw cell value
    :returns: integer
    """
    if isinstance(value, bool):
        raise NormalizationError("invalid_integer", "not a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) <= 2**53:
            return int(value)
        raise NormalizationError("invalid_integer", "not a whole number")
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        raise NormalizationError("invalid_integer", "not a whole number")
    text = str(value).strip()
    if not INTEGER_PATTERN.match(text):
        raise NormalizationError("invalid_integer", "not a whole number")
    return int(text)


def source_text(value):
    """
    converts a raw cell into text for a text column
    :param value: raw cell value
    :returns: text, keeping whole-number spreadsheet cells free of .0
    """
    if isinstance(value, float) and abs(value) > MAX_EXACT_FLOAT_ID:
        return repr(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return identifier_spelling(value) if not isinstance(value, str) else value


def normalize_value(raw, column, profile, stats, blank_hits=None):
    """
    applies the profile's whitespace, blank, and type rules to one cell
    :param raw: raw cell value
    :param column: column specification
    :param profile: table profile
    :param stats: counters updated with the transformations applied
    :param blank_hits: optional counter of (column, token) blank-token matches
    :returns: typed value, or none for blank
    """
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return None

    if isinstance(raw, str):
        text = raw.strip()
        if column.whitespace == "collapse":
            text = " ".join(text.split())
        if text != raw:
            stats["whitespace_normalized"] += 1
        if not text:
            return None
        if text in profile.blank_values:
            stats["blank_tokens_normalized"] += 1
            if blank_hits is not None:
                blank_hits[(column.name, text)] += 1
            return None
        value = text
    else:
        value = raw

    if column.type == "text":
        return source_text(value)
    if column.type == "identifier":
        return parse_text_identifier(value)
    if column.type == "integer":
        parsed = parse_integer(value)
    elif column.type == "decimal":
        parsed = parse_amount(value, profile.amounts)
    else:
        parsed = parse_date(value, profile.date_formats)
    if isinstance(value, str) and as_text(parsed) != value:
        stats["values_reformatted"] += 1
    return parsed


def read_input(path, profile, role):
    """
    reads one CSV/XLSX file with the shared readers and size limit
    :param path: input file path
    :param profile: table profile
    :param role: input, before, or after
    :returns: source table
    """
    path = Path(path)
    if not path.is_file():
        raise InputError(f"{role} file not found: {path}")
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in profile.formats:
        raise InputError(
            f"{path.name} is not one of the profile's accepted formats "
            f"{sorted(profile.formats)}"
        )
    return read_table(path, profile.read_spec(role), profile)


def clean_table(profile, path, role="input"):
    """
    cleans one CSV/XLSX file with a table profile; the file is only read
    :param profile: table profile
    :param path: input file path
    :param role: input, before, or after, used in reports
    :returns: clean result
    """
    table = read_input(path, profile, role)
    header = table.header
    name = table.path.name
    sources = {column.source for column in profile.columns}
    missing = [c.name for c in profile.columns if c.source not in header]
    unmapped = [h for h in header if h and h not in sources]
    stats = {
        "whitespace_normalized": 0,
        "blank_tokens_normalized": 0,
        "values_reformatted": 0,
    }
    issues = [
        TableIssue(
            role,
            name,
            row,
            REJECTED,
            "malformed_row",
            malformed_message(table, row, MESSAGES["malformed_row"]),
        )
        for row in table.malformed_rows
    ]
    for column in missing:
        issues.append(
            TableIssue(
                role,
                name,
                0,
                NOTE,
                "optional_column_missing",
                "optional column is not in this file; its values are blank",
                column=column,
                source_column=profile.column(column).source,
            )
        )
    if unmapped:
        issues.append(
            TableIssue(
                role,
                name,
                0,
                NOTE,
                "unmapped_columns",
                f"{len(unmapped)} source column(s) not in the profile were left "
                f"out of the outputs: {', '.join(unmapped)}",
            )
        )

    records = []
    rejected = []
    blank_hits = Counter()
    for source in table.rows:
        values = {}
        problems = []
        for column in profile.columns:
            raw = source.values.get(column.source)
            try:
                value = normalize_value(raw, column, profile, stats, blank_hits)
            except NormalizationError as error:
                problems.append((column, error.code, raw))
                value = None
            else:
                if value is None and column.required:
                    problems.append((column, "missing_value", None))
            values[column.name] = value

        key = None
        failed = {column.name for column, _, _ in problems}
        if profile.key and not failed & set(profile.key):
            key = tuple(as_text(values[name]) for name in profile.key)

        if problems:
            rejected.append(key)
            for column, code, raw in problems:
                issues.append(
                    TableIssue(
                        role,
                        name,
                        source.row,
                        REJECTED,
                        code,
                        MESSAGES.get(code, "value could not be used"),
                        key=key_text(key),
                        column=column.name,
                        source_column=column.source,
                        value="" if raw is None else source_text(raw),
                    )
                )
            continue
        records.append(Record(source.row, values, key))

    result = CleanResult(
        profile=profile,
        input=InputFile(
            role, name, table.sha256, len(table.rows) + len(table.malformed_rows)
        ),
        header=header,
        records=records,
        issues=issues,
        unmapped_columns=unmapped,
        missing_columns=missing,
        stats=stats,
        blank_hits=blank_hits,
    )
    result.rejected_keys = {key for key in rejected if key is not None}
    result.unkeyed_rejections = sum(1 for key in rejected if key is None) + len(
        table.malformed_rows
    )
    remove_duplicates(result, rejected)
    result.issues.sort(key=TableIssue.sort_key)
    return result


def remove_duplicates(result, rejected):
    """
    drops exact duplicate rows and holds back every row of a conflicting key

    a row identical to an earlier one is removed and the first is kept; rows
    sharing a business key but differing in any value are all rejected, as
    are accepted rows whose key also appears on a rejected row, so no copy is
    ever chosen as authoritative
    :param result: clean result, updated in place
    :param rejected: business keys of rejected rows, none where unreadable
    :returns: none
    """
    profile = result.profile
    role, name = result.input.role, result.input.name
    columns = [column.name for column in profile.columns]
    first_seen = {}
    unique = []
    for record in result.records:
        signature = tuple(as_text(record.values[column]) for column in columns)
        if signature in first_seen:
            result.issues.append(
                TableIssue(
                    role,
                    name,
                    record.row,
                    DUPLICATE_REMOVED,
                    "duplicate_row",
                    f"identical to source row {first_seen[signature]}, which was kept",
                    key=key_text(record.key),
                )
            )
            continue
        first_seen[signature] = record.row
        unique.append(record)

    if not profile.key:
        result.records = unique
        return

    seen_keys = defaultdict(int)
    for key in [record.key for record in result.records] + rejected:
        if key is not None:
            seen_keys[key] += 1
    result.duplicate_keys = {key for key, count in seen_keys.items() if count > 1}

    groups = defaultdict(list)
    for record in unique:
        groups[record.key].append(record)
    kept = []
    for record in unique:
        group = groups[record.key]
        if len(group) > 1:
            result.conflicting_keys.add(record.key)
            others = [other.row for other in group if other is not record]
            differing = [
                column
                for column in columns
                if len({as_text(other.values[column]) for other in group}) > 1
            ]
            result.issues.append(
                TableIssue(
                    role,
                    name,
                    record.row,
                    REJECTED,
                    "conflicting_key",
                    f"business key is also on source row(s) "
                    f"{', '.join(map(str, others))} with different "
                    f"{', '.join(differing)}; no copy was chosen",
                    key=key_text(record.key),
                )
            )
        elif record.key in result.rejected_keys:
            result.issues.append(
                TableIssue(
                    role,
                    name,
                    record.row,
                    REJECTED,
                    "key_on_rejected_row",
                    "business key also appears on a rejected row; held back until "
                    "the source is fixed",
                    key=key_text(record.key),
                )
            )
        else:
            kept.append(record)
    result.records = kept

    expected = len(unique) - len(kept)
    held = sum(
        1
        for issue in result.issues
        if issue.code in ("conflicting_key", "key_on_rejected_row")
    )
    if expected != held:
        raise TabularIntegrityError("held-back rows do not match their issues")


def difference(column, before, after):
    """
    computes the signed change of a numeric column
    :param column: column specification
    :param before: previous typed value
    :param after: new typed value
    :returns: after minus before for decimal and integer columns, else none
    """
    if column.type not in ("decimal", "integer") or before is None or after is None:
        return None
    return after - before


def compare_tables(profile, before_path, after_path):
    """
    compares two versions of a table by business key
    :param profile: compare profile
    :param before_path: earlier export
    :param after_path: later export
    :returns: compare result
    """
    before = clean_table(profile, before_path, "before")
    after = clean_table(profile, after_path, "after")
    issues = sorted(before.issues + after.issues, key=TableIssue.sort_key)

    skipped = sorted(set(before.missing_columns) | set(after.missing_columns))
    compared = [
        column
        for column in profile.columns
        if column.name not in profile.key
        and column.name not in profile.ignore
        and column.name not in skipped
    ]
    for name in skipped:
        if name in profile.key or name in profile.ignore:
            continue
        issues.append(
            TableIssue(
                "both",
                "",
                0,
                NOTE,
                "column_not_compared",
                "column is missing from at least one file, so it was not compared",
                column=name,
                source_column=profile.column(name).source,
            )
        )

    ignored = [name for name in profile.ignore if name not in skipped]
    ignored_differences = Counter()
    before_index = {record.key: record for record in before.records}
    after_index = {record.key: record for record in after.records}
    keys = (
        set(before_index)
        | set(after_index)
        | before.unresolved_keys
        | after.unresolved_keys
    )

    status = {}
    added, removed, changes = [], [], []
    for key in sorted(keys, key=key_sort):
        if key in before.conflicting_keys or key in after.conflicting_keys:
            status[key] = AMBIGUOUS
        elif key in before.rejected_keys or key in after.rejected_keys:
            status[key] = INVALID
        elif key not in before_index:
            status[key] = ADDED
            added.append(after_index[key])
        elif key not in after_index:
            status[key] = REMOVED
            removed.append(before_index[key])
        else:
            old, new = before_index[key].values, after_index[key].values
            found = [
                ValueChange(
                    key,
                    column.name,
                    old[column.name],
                    new[column.name],
                    difference(column, old[column.name], new[column.name]),
                )
                for column in compared
                if as_text(old[column.name]) != as_text(new[column.name])
            ]
            status[key] = MODIFIED if found else UNCHANGED
            changes.extend(found)
            for name in ignored:
                if as_text(old[name]) != as_text(new[name]):
                    ignored_differences[name] += 1

    unicode_changes = [
        change
        for change in changes
        if isinstance(change.before, str)
        and isinstance(change.after, str)
        and nfc(change.before) == nfc(change.after)
    ]
    for change in unicode_changes:
        issues.append(
            TableIssue(
                "both",
                "",
                0,
                NOTE,
                "unicode_form_differs",
                "before and after differ only in Unicode form (such as composed "
                "and decomposed accents); reported as modified because the "
                "stored text differs",
                key=key_text(change.key),
                column=change.column,
                source_column=profile.column(change.column).source,
            )
        )
    pairs = unicode_key_pairs(added, removed)
    for key in pairs:
        issues.append(
            TableIssue(
                "both",
                "",
                0,
                NOTE,
                "key_unicode_form_differs",
                "this added or removed key matches a key in the other export "
                "except for Unicode form; it is probably the same record "
                "re-encoded, not a real addition or removal",
                key=key_text(key),
            )
        )

    result = CompareResult(
        profile=profile,
        before=before,
        after=after,
        compared_columns=[column.name for column in compared],
        skipped_columns=skipped,
        status=status,
        added=added,
        removed=removed,
        changes=changes,
        issues=issues,
        ignored_differences={
            name: ignored_differences[name]
            for name in ignored
            if ignored_differences[name]
        },
        unicode_changes=len(unicode_changes),
        unicode_key_pairs=len(pairs) // 2,
    )
    check_comparison(result)
    return result


def nfc(text):
    """
    gives the canonical composed Unicode form, used only to detect lookalikes
    :param text: text value
    :returns: NFC-normalized text
    """
    return unicodedata.normalize("NFC", text)


def unicode_key_pairs(added, removed):
    """
    finds added and removed keys that are equal once Unicode is normalized

    nothing is merged: the records stay added and removed, and each key of a
    pair gets a note so a reviewer can see the likely re-encoding
    :param added: added records
    :param removed: removed records
    :returns: sorted list of the keys involved, added and removed
    """
    removed_by_form = defaultdict(list)
    for record in removed:
        removed_by_form[tuple(nfc(part) for part in record.key)].append(record.key)
    involved = set()
    for record in added:
        matches = removed_by_form.get(tuple(nfc(part) for part in record.key), [])
        if len(matches) == 1:
            involved.add(record.key)
            involved.add(matches[0])
    return sorted(involved, key=key_sort)


def check_comparison(result):
    """
    cross-checks comparison counts before anything is written
    :param result: compare result
    :returns: none
    """
    counts = result.counts()
    before_keys = {record.key for record in result.before.records}
    after_keys = {record.key for record in result.after.records}
    clean_before = before_keys - result.after.unresolved_keys
    clean_after = after_keys - result.before.unresolved_keys
    if counts["removed"] + counts["modified"] + counts["unchanged"] != len(
        clean_before
    ):
        raise TabularIntegrityError("before records do not add up")
    if counts["added"] + counts["modified"] + counts["unchanged"] != len(clean_after):
        raise TabularIntegrityError("after records do not add up")
    if counts["modified"] != len({change.key for change in result.changes}):
        raise TabularIntegrityError("modified records do not match value changes")


def token_is_value(token, column, profile):
    """
    checks whether a blank token is also a real value of a typed column
    :param token: configured blank token
    :param column: column specification
    :param profile: table profile
    :returns: true when the token would parse as a decimal, integer, or date
    """
    try:
        if column.type == "decimal":
            parse_amount(token, profile.amounts)
        elif column.type == "integer":
            parse_integer(token)
        elif column.type == "date":
            parse_date(token, profile.date_formats)
        else:
            return False
    except NormalizationError:
        return False
    return True


def assessment_finding(level, code, count, message):
    """
    builds one comparison assessment finding
    :param level: incomplete or warning
    :param code: stable machine-readable code
    :param count: records, keys, rows, or values involved
    :param message: explanation naming columns, never cell values
    :returns: finding dictionary
    """
    return {"level": level, "code": code, "count": count, "message": message}


def missing_column_findings(result, matched):
    """
    reports configured columns that could not be compared
    :param result: compare result
    :param matched: records found in both exports
    :returns: list of findings
    """
    profile = result.profile
    before_missing = set(result.before.missing_columns)
    after_missing = set(result.after.missing_columns)
    findings = []
    for name in result.skipped_columns:
        if name in profile.ignore:
            continue
        source = profile.column(name).source
        if name in before_missing and name in after_missing:
            findings.append(
                assessment_finding(
                    "warning",
                    "column_missing_from_both",
                    0,
                    f"column {name} (source {source!r}) is in neither export, so "
                    "it was not compared",
                )
            )
            continue
        side = "earlier" if name in before_missing else "later"
        findings.append(
            assessment_finding(
                INCOMPLETE,
                "column_missing_from_one_export",
                matched,
                f"column {name} (source {source!r}) is missing from the {side} "
                f"export, so it was not compared; the {matched} matched "
                "record(s) may have unreported changes in it",
            )
        )
    return findings


def header_findings(result):
    """
    reports source headers present in only one of the two exports
    :param result: compare result
    :returns: list of findings
    """
    before_header = [name for name in result.before.header if name]
    after_header = [name for name in result.after.header if name]
    only_before = [name for name in before_header if name not in after_header]
    only_after = [name for name in after_header if name not in before_header]
    if not (only_before or only_after):
        return []
    parts = []
    if only_before:
        parts.append(f"only in the earlier export: {', '.join(only_before)}")
    if only_after:
        parts.append(f"only in the later export: {', '.join(only_after)}")
    return [
        assessment_finding(
            "warning",
            "source_headers_differ",
            len(only_before) + len(only_after),
            "the exports have different headers (" + "; ".join(parts) + "); "
            "confirm no mapped column was renamed",
        )
    ]


def blank_token_findings(profile, sides):
    """
    reports blank tokens that erased real decimal, integer, or date values
    :param profile: table profile
    :param sides: clean results whose blank-token matches are checked
    :returns: list of findings
    """
    erased = Counter()
    for side in sides:
        for (name, token), count in side.blank_hits.items():
            if token_is_value(token, profile.column(name), profile):
                erased[name] += count
    return [
        assessment_finding(
            "warning",
            "blank_token_is_a_value",
            count,
            f"{count} cell(s) in {name} matched a blank token that is also a "
            f"valid {profile.column(name).type} value and were treated as blank",
        )
        for name, count in sorted(erased.items())
    ]


def assess_comparison(result):
    """
    judges how far a comparison can be trusted, from counts and column names

    incomplete findings mean some records were not classified or some
    configured columns were not compared, so unchanged does not cover them;
    warnings mean the categories are complete but hide something a reviewer
    should know about
    :param result: compare result
    :returns: dictionary with level and findings
    """
    counts = result.counts()
    matched = counts["modified"] + counts["unchanged"]
    findings = missing_column_findings(result, matched)

    unclassified = counts["ambiguous_keys"] + counts["invalid_keys"]
    if unclassified:
        findings.append(
            assessment_finding(
                INCOMPLETE,
                "unclassified_keys",
                unclassified,
                f"{unclassified} business key(s) could not be classified "
                f"({counts['ambiguous_keys']} ambiguous, {counts['invalid_keys']} "
                "invalid); they are in no change category until the source rows "
                "are fixed",
            )
        )
    if counts["unkeyed_rejected_rows"]:
        findings.append(
            assessment_finding(
                INCOMPLETE,
                "rejected_rows_without_key",
                counts["unkeyed_rejected_rows"],
                f"{counts['unkeyed_rejected_rows']} rejected row(s) had no usable "
                "business key and are in no change category",
            )
        )

    for name, count in result.ignored_differences.items():
        findings.append(
            assessment_finding(
                "warning",
                "ignored_column_differs",
                count,
                f"{count} matched record(s) differ in {name}, which the profile "
                "ignores; those differences are not reported as changes",
            )
        )
    if result.unicode_changes:
        findings.append(
            assessment_finding(
                "warning",
                "unicode_only_changes",
                result.unicode_changes,
                f"{result.unicode_changes} changed value(s) differ only in Unicode "
                "form; see unicode_form_differs in the issues",
            )
        )
    if result.unicode_key_pairs:
        findings.append(
            assessment_finding(
                "warning",
                "unicode_key_pairs",
                result.unicode_key_pairs,
                f"{result.unicode_key_pairs} added key(s) match a removed key "
                "except for Unicode form; they may be the same records re-encoded",
            )
        )
    duplicates = sum(
        side.counts()["duplicate_rows_removed"]
        for side in (result.before, result.after)
    )
    if duplicates:
        findings.append(
            assessment_finding(
                "warning",
                "duplicate_rows_removed",
                duplicates,
                f"{duplicates} exact duplicate row(s) were removed before comparing",
            )
        )
    findings += header_findings(result)
    findings += blank_token_findings(result.profile, (result.before, result.after))

    levels = {item["level"] for item in findings}
    if INCOMPLETE in levels:
        level = INCOMPLETE
    elif findings:
        level = COMPLETE_WITH_WARNINGS
    else:
        level = COMPLETE
    findings.sort(key=lambda item: item["level"] != INCOMPLETE)
    return {"level": level, "findings": findings}
