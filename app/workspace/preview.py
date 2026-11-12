"""
describes a chosen file for the screen: columns, a few rows, and hints

column descriptions come from the read-only `inspect` command's analysis
(app.ordixa.inspection); the first rows are shown exactly as stored so the
person can recognise their own data. Nothing here decides how a value is
parsed: the hints only pre-fill choices the person confirms
"""

import csv
import zipfile
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from app.ordixa.inspection import (
    DAY_MONTH_PAIRS,
    ID_HEADER,
    ColumnStats,
    clean_header,
    inspect_file,
    is_blank,
)

PREVIEW_ROWS = 25
HINT_ROWS = 5000
MAX_DISTINCT = 40
MAX_CELL_TEXT = 300
BLANK_CANDIDATES = ("N/A", "n/a", "NA", "#N/A", "-", "--", "null", "NULL", "none")


def display(value):
    """
    turns a stored cell into the text a spreadsheet would show
    :param value: CSV text or typed XLSX value
    :returns: text, shortened when very long
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    elif isinstance(value, datetime):
        text = (
            value.date().isoformat()
            if value.time() == time(0, 0)
            else value.isoformat(sep=" ")
        )
    elif isinstance(value, date):
        text = value.isoformat()
    elif isinstance(value, float):
        text = repr(value)
    elif isinstance(value, Decimal):
        text = str(value)
    else:
        text = str(value)
    return text if len(text) <= MAX_CELL_TEXT else text[:MAX_CELL_TEXT] + "…"


def read_rows(path, encoding="utf-8-sig", limit=HINT_ROWS):
    """
    reads the header and up to limit non-blank rows of the first table
    :param path: CSV or XLSX file
    :param encoding: CSV text encoding
    :param limit: maximum data rows
    :returns: tuple of header list, row lists, and worksheet names
    """
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding=encoding) as handle:
            reader = csv.reader(handle)
            header = [clean_header(cell) for cell in next(reader, [])]
            rows = []
            for cells in reader:
                if not any(cell.strip() for cell in cells):
                    continue
                rows.append(cells)
                if len(rows) >= limit:
                    break
        return header, rows, []

    if not zipfile.is_zipfile(path):
        return [], [], []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = workbook.sheetnames
        iterator = workbook.worksheets[0].iter_rows(values_only=True)
        header = [clean_header(cell) for cell in next(iterator, ()) or ()]
        while header and not header[-1]:
            header.pop()
        rows = []
        for cells in iterator:
            cells = list(cells or ())
            if all(is_blank(cell) for cell in cells):
                continue
            rows.append(cells)
            if len(rows) >= limit:
                break
    finally:
        workbook.close()
    return header, rows, sheets


def column_hints(header, rows):
    """
    gathers structured observations that pre-fill the column choices
    :param header: header names
    :param rows: sampled rows
    :returns: dictionary of hints by column name
    """
    stats = [ColumnStats(name, index) for index, name in enumerate(header, 1)]
    blanks = [Counter() for _ in header]
    for cells in rows:
        for index, column in enumerate(stats):
            cell = cells[index] if index < len(cells) else None
            column.observe(cell)
            if isinstance(cell, str) and cell.strip() in BLANK_CANDIDATES:
                blanks[index][cell.strip()] += 1

    hints = {}
    for column, tokens in zip(stats, blanks):
        summary = column.summary(len(rows))
        formats = sorted(column.date_hits) if column.date_values else []
        ambiguous = [
            [us, eu] for us, eu in DAY_MONTH_PAIRS if us in formats and eu in formats
        ]
        hints[column.name] = {
            "basic_type": summary["basic_type"],
            "suggestions": summary["suggestions"],
            "notes": summary["notes"],
            "values": column.values,
            "blank": column.blanks,
            "distinct": summary["distinct"],
            "unique": summary["distinct"] is not None
            and summary["distinct"] == column.values
            and column.values > 0,
            "leading_zeros": column.leading_zeros,
            "id_like_header": bool(ID_HEADER.search(column.name)),
            "date_formats": formats,
            "date_cells": column.kinds["date cell"],
            "ambiguous_date_formats": ambiguous,
            "amount_styles": sorted(column.amount_styles),
            "currency_symbols": sorted(column.symbols),
            "negative_styles": sorted(column.negatives),
            "max_decimals": column.max_decimals,
            "number_cells": column.kinds["number cell"],
            "blank_tokens": sorted(tokens),
            "personal": bool(column.kinds["email"] or column.kinds["phone"]),
        }
    return hints


def describe(path, encoding="utf-8-sig"):
    """
    builds everything the column screen shows for one file
    :param path: stored file
    :param encoding: CSV text encoding
    :returns: dictionary
    """
    report = inspect_file(path, encoding)
    result = {
        "file": report["file"],
        "bytes": report["bytes"],
        "errors": list(report["errors"]),
        "warnings": [],
        "rows": None,
        "rows_truncated": False,
        "header": [],
        "preview": [],
        "columns": [],
        "worksheets": [],
    }
    if report["errors"] or not report["tables"]:
        if not result["errors"]:
            result["errors"].append("no table was found in this file")
        return result

    table = report["tables"][0]
    result["rows"] = table["data_rows"]
    result["rows_truncated"] = table["rows_truncated"]
    result["warnings"] = list(table["warnings"])
    try:
        header, rows, sheets = read_rows(path, encoding)
    except (UnicodeDecodeError, csv.Error, OSError) as error:
        result["errors"].append(f"could not be read: {type(error).__name__}")
        return result
    if len(sheets) > 1:
        result["warnings"].append(
            f"this workbook has {len(sheets)} worksheets; Ordixa reads only the "
            f"first one, {sheets[0]!r}"
        )
    result["worksheets"] = sheets
    result["header"] = header
    result["preview"] = [
        [
            display(row[index] if index < len(row) else None)
            for index in range(len(header))
        ]
        for row in rows[:PREVIEW_ROWS]
    ]
    hints = column_hints(header, rows)
    result["columns"] = [
        {"name": name, "position": index, **hints.get(name, {})}
        for index, name in enumerate(header, 1)
        if name
    ]
    return result


def distinct_values(paths, column, encoding="utf-8-sig"):
    """
    lists the different values one column holds across several files
    :param paths: stored files
    :param column: header name
    :param encoding: CSV text encoding
    :returns: list of {value, count}, most common first, at most MAX_DISTINCT
    """
    counts = Counter()
    for path in paths:
        try:
            header, rows, _ = read_rows(path, encoding, limit=200_000)
        except UnicodeDecodeError, csv.Error, OSError:
            continue
        if column not in header:
            continue
        index = header.index(column)
        for row in rows:
            cell = row[index] if index < len(row) else None
            if is_blank(cell):
                continue
            counts[display(cell).strip()] += 1
    return [
        {"value": value, "count": count}
        for value, count in counts.most_common(MAX_DISTINCT)
    ]
