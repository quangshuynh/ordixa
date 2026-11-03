"""
regression tests for first customer pilot readiness: unbalanced CSV quotes,
repeat and interrupted processing, the end-to-end pilot workflow, and the
demo's review handoff
"""

import csv
import hashlib
import shutil
import time
from decimal import Decimal

from ordixa_helpers import (
    EXPORTS_DIR,
    PAYMENT_HEADER,
    PROFILE_PATH,
    ROOT,
    build_profile,
    copy_exports,
    write_exports,
)

from app.ordixa.cli import EXIT_OK, main
from app.ordixa.demo import main as demo_main
from app.ordixa.manifest import ISSUES_NAME, LEDGER_NAME, MANIFEST_NAME, verify_run
from app.ordixa.pipeline import process
from app.ordixa.tabular import clean_table
from app.ordixa.tabular_profile import load_table_profile

INVENTORY_PROFILE = ROOT / "examples" / "ordixa" / "profiles" / "inventory-clean.toml"


def order(ref, total, customer="7000101"):
    """
    builds one order row in ORDER_HEADER order
    :param ref: order reference such as #1001
    :param total: total text
    :param customer: customer id text
    :returns: list of cells
    """
    return [ref, "2026-11-02", customer, "USD", total]


def payment(pid, ref, amount, kind="Payment"):
    """
    builds one payment row in PAYMENT_HEADER order
    :param pid: transaction id
    :param ref: order reference
    :param amount: amount text
    :param kind: transaction type label
    :returns: list of cells
    """
    return [pid, ref, "11/03/2026", kind, "Paid", amount, "USD"]


def tree_digest(directory):
    """
    fingerprints every file under a directory
    :param directory: directory to fingerprint
    :returns: mapping of relative path to SHA-256
    """
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def payments_text(*lines):
    """
    builds a payments CSV with the edge-case header
    :param lines: raw data lines
    :returns: CSV text
    """
    return ",".join(PAYMENT_HEADER) + "\n" + "\n".join(lines) + "\n"


def three_orders(directory, payments):
    """
    writes three orders and a raw payments file
    :param directory: input directory
    :param payments: raw payments CSV text
    :returns: directory path
    """
    write_exports(
        directory,
        orders=[order("#1", "10.00"), order("#2", "20.00"), order("#3", "30.00")],
        payments=[],
    )
    (directory / "payments.csv").write_text(payments, encoding="utf-8")
    return directory


def test_unclosed_quote_at_end_of_file_is_reported_not_swallowed(tmp_path):
    """
    tests that a quote never closed does not silently absorb the rows after it

    without the safeguard the CSV reader puts lines 3 and 4 inside the amount
    cell of line 2, and two payments disappear without any issue
    :param tmp_path: pytest temporary directory
    :returns: none
    """
    directory = three_orders(
        tmp_path / "in",
        payments_text(
            'PMT-1,#1,11/03/2026,Payment,Paid,"10.00,USD',
            "PMT-2,#2,11/03/2026,Payment,Paid,20.00,USD",
            "PMT-3,#3,11/03/2026,Payment,Paid,30.00,USD",
        ),
    )

    result = process(build_profile(), directory)

    malformed = [i for i in result.issues if i.code == "malformed_row"]
    assert [(i.file, i.row, i.blocking) for i in malformed] == [
        ("payments.csv", 2, True)
    ]
    assert "lines 2-4" in malformed[0].message
    assert "none of these lines were used" in malformed[0].message
    assert result.summary["transactions"] == 0
    assert result.summary["net_received"] == Decimal("0.00")
    assert result.summary["status_counts"]["unpaid"] == 3


def test_quote_balanced_across_lines_in_an_unmapped_column_is_rejected(tmp_path):
    """
    tests the case where the cell count still matches the header

    a stray quote opened in one row's last column and closed in a later row's
    last column hides the row between them inside a single cell
    :param tmp_path: pytest temporary directory
    :returns: none
    """
    header = ",".join(PAYMENT_HEADER) + ",Notes"
    text = (
        header
        + "\n"
        + 'PMT-1,#1,11/03/2026,Payment,Paid,10.00,USD,"deposit\n'
        + "PMT-2,#2,11/03/2026,Payment,Paid,20.00,USD,\n"
        + 'PMT-3,#3,11/03/2026,Payment,Paid,30.00,USD,final"\n'
    )
    directory = three_orders(tmp_path / "in", text)

    result = process(build_profile(), directory)

    malformed = [i for i in result.issues if i.code == "malformed_row"]
    assert [i.row for i in malformed] == [2]
    assert "lines 2-4" in malformed[0].message
    assert result.summary["transactions"] == 0


def test_legitimate_multi_line_note_is_kept(tmp_path):
    """
    tests that a properly quoted note with a line break is still accepted
    :param tmp_path: pytest temporary directory
    :returns: none
    """
    header = ",".join(PAYMENT_HEADER) + ",Notes"
    text = (
        header
        + "\n"
        + 'PMT-1,#1,11/03/2026,Payment,Paid,10.00,USD,"left at\nfront desk"\n'
        + "PMT-2,#2,11/03/2026,Payment,Paid,20.00,USD,\n"
    )
    directory = three_orders(tmp_path / "in", text)

    result = process(build_profile(), directory)

    assert not [i for i in result.issues if i.code == "malformed_row"]
    assert result.summary["transactions"] == 2
    assert result.summary["net_received"] == Decimal("30.00")


def test_clean_workflow_reports_the_swallowed_line_range(tmp_path):
    """
    tests that the spreadsheet clean workflow shares the quote safeguard
    :param tmp_path: pytest temporary directory
    :returns: none
    """
    source = EXPORTS_DIR / "inventory" / "inventory-before.csv"
    lines = source.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace(",Hardware,", ',"Hardware,', 1)
    path = tmp_path / "inventory.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = clean_table(load_table_profile(INVENTORY_PROFILE, "clean"), path)

    malformed = [i for i in result.issues if i.code == "malformed_row"]
    assert len(malformed) == 1
    assert f"lines 2-{len(lines)}" in malformed[0].message


def test_reimporting_an_identical_file_counts_rows_once_and_says_why(tmp_path, capsys):
    """
    tests repeat processing of the same export within one batch
    :param tmp_path: pytest temporary directory
    :param capsys: pytest capture fixture
    :returns: none
    """
    directory = copy_exports("clean", tmp_path / "in")
    once = process(build_profile(), directory)
    shutil.copy(
        directory / "payments_2026-09.csv", directory / "payments_2026-09-resent.csv"
    )

    code = main(
        [
            "run",
            "--profile",
            str(PROFILE_PATH),
            "--input",
            str(directory),
            "--out",
            str(tmp_path / "out"),
        ]
    )
    out = capsys.readouterr().out
    twice = process(build_profile(), directory)

    assert code == EXIT_OK
    assert (
        "note: payments_2026-09.csv has exactly the same contents as "
        "payments_2026-09-resent.csv" in out
    )
    assert twice.summary["net_received"] == once.summary["net_received"]
    assert twice.summary["transactions"] == once.summary["transactions"]
    assert {i.code for i in twice.issues if i.blocking} == {"duplicate_row"}


def test_interrupted_run_leftovers_are_reported_and_never_published(tmp_path, capsys):
    """
    tests that a staging folder from a killed run is flagged and left alone
    :param tmp_path: pytest temporary directory
    :param capsys: pytest capture fixture
    :returns: none
    """
    out_dir = tmp_path / "out"
    leftover = out_dir / ".run-20261102T120000Z-abcdef.partial"
    leftover.mkdir(parents=True)
    (leftover / LEDGER_NAME).write_text("order_ref\n", encoding="utf-8")

    code = main(
        [
            "run",
            "--profile",
            str(PROFILE_PATH),
            "--input",
            str(EXPORTS_DIR / "clean"),
            "--out",
            str(out_dir),
        ]
    )
    err = capsys.readouterr().err

    assert code == EXIT_OK
    assert "1 incomplete folder(s) from an interrupted run" in err
    assert leftover.name in err
    assert (leftover / LEDGER_NAME).read_text(encoding="utf-8") == "order_ref\n"
    runs = list(out_dir.glob("run-*"))
    assert len(runs) == 1
    assert all(passed for _, passed in verify_run(runs[0], EXPORTS_DIR / "clean"))


def test_pilot_workflow_end_to_end_is_safe_and_deterministic(tmp_path, capsys):
    """
    tests import, validation, reconciliation, and export on the problem fixtures

    two runs over the same inputs must agree byte for byte on the deterministic
    artifacts, the sources must be unchanged, earlier runs must be untouched,
    and no exported cell may start a spreadsheet formula
    :param tmp_path: pytest temporary directory
    :param capsys: pytest capture fixture
    :returns: none
    """
    directory = copy_exports("problematic", tmp_path / "in")
    payments = directory / "payments_2026-09.csv"
    payments.write_text(
        payments.read_text(encoding="utf-8")
        + 'PMT-279999,#2001,09/30/2026,Payment,Paid,"=HYPERLINK(""x"")",USD,,Zoë\n',
        encoding="utf-8",
    )
    sources = tree_digest(directory)
    out_dir = tmp_path / "out"
    args = ["run", "--profile", str(PROFILE_PATH), "--input", str(directory)]

    assert main([*args, "--out", str(out_dir)]) == EXIT_OK
    first = next(out_dir.glob("run-*"))
    first_files = tree_digest(first)
    assert main([*args, "--out", str(out_dir)]) == EXIT_OK
    capsys.readouterr()
    second = next(path for path in out_dir.glob("run-*") if path != first)

    assert tree_digest(directory) == sources
    assert tree_digest(first) == first_files
    for name in (LEDGER_NAME, ISSUES_NAME):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    assert all(passed for _, passed in verify_run(second, directory))
    assert (MANIFEST_NAME in first_files) and (MANIFEST_NAME in tree_digest(second))

    for name in (LEDGER_NAME, ISSUES_NAME):
        with (second / name).open(newline="", encoding="utf-8") as handle:
            cells = [cell for row in csv.reader(handle) for cell in row]
        assert not [c for c in cells if c.startswith(("=", "@"))]
    with (second / ISSUES_NAME).open(newline="", encoding="utf-8") as handle:
        values = [row["value"] for row in csv.DictReader(handle)]
    assert '\'=HYPERLINK("x")' in values

    result = process(build_profile(), directory)
    ledger_total = sum(result.ledger["net_paid"], Decimal("0.00"))
    with (second / LEDGER_NAME).open(newline="", encoding="utf-8") as handle:
        exported = sum(
            (Decimal(row["net_paid"]) for row in csv.DictReader(handle)),
            Decimal("0.00"),
        )
    assert exported == ledger_total == result.summary["net_received"]


def test_large_realistic_batch_reconciles_exactly(tmp_path):
    """
    tests a year of trading for a small business in one batch

    totals are checked against an independent exact sum, and the run must stay
    well within an interactive time budget
    :param tmp_path: pytest temporary directory
    :returns: none
    """
    count = 20_000
    orders = [order(f"#{n}", f"{n % 997 + 0.37:,.2f}") for n in range(1, count + 1)]
    payments = []
    expected = Decimal("0.00")
    for n in range(1, count + 1):
        amount = Decimal(f"{n % 997 + 0.37:.2f}")
        if n % 5 == 0:
            amount -= Decimal("0.01")
        payments.append(payment(f"PMT-{n}", f"#{n}", f"${amount:,}"))
        expected += amount
    directory = write_exports(tmp_path / "in", orders=orders, payments=payments)

    started = time.perf_counter()
    result = process(build_profile(), directory)
    elapsed = time.perf_counter() - started

    assert result.summary["orders"] == count
    assert result.summary["transactions"] == count
    assert result.summary["net_received"] == expected
    assert result.summary["status_counts"]["partial"] == count // 5
    assert result.summary["outstanding_balance"] == Decimal("0.01") * (count // 5)
    assert elapsed < 60


def test_demo_explains_what_was_automated_and_what_needs_review(tmp_path, capsys):
    """
    tests the review handoff printed by the guided demo
    :param tmp_path: pytest temporary directory
    :param capsys: pytest capture fixture
    :returns: none
    """
    assert demo_main(["--out", str(tmp_path), "--skip-inspect"]) == EXIT_OK
    out = capsys.readouterr().out

    assert "Automated by Ordixa:" in out
    assert "reconciled 15 orders; 9 fully paid with no flags need no action" in out
    assert "Needs human review:" in out
    assert "6 order(s) on the Exceptions sheet (3 partial, 2 unpaid" in out
    assert "8 rejected row(s), not counted in any total:" in out
    assert "conflicting_duplicate_id 2" in out
    assert "1 row(s) excluded by the profile (moved no money)" in out
    assert "never matches a payment by amount, date, or customer" in out
