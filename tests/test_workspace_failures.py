"""
failure states: unusable files, settings that are not ready, stopped and
interrupted runs, failing runs, and duplicate records. None of them may leave
a partial result that could be mistaken for a finished one
"""

import time

import pytest
from workspace_helpers import (
    EXAMPLES,
    FAILING_RUN,
    SLOW_PARTIAL_RUN,
    ScriptRunner,
    Workspace,
)

from app.ordixa.cli import main as cli_main
from app.workspace import store as store_module

INVENTORY = EXAMPLES / "exports" / "inventory"


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path / "workspace")


def leftovers(folder):
    return [path.name for path in folder.rglob("*") if path.name.startswith(".upload-")]


def test_only_csv_and_xlsx_files_are_accepted(workspace):
    session_id = workspace.new_session("clean")
    response = workspace.upload(session_id, "input", "prices.xls", b"legacy")
    assert response.status_code == 415
    assert ".xlsx" in response.json()["error"]
    response = workspace.upload(session_id, "input", "notes.txt", b"hello")
    assert response.status_code == 415


def test_an_empty_file_is_refused_and_nothing_is_kept(workspace):
    session_id = workspace.new_session("clean")
    response = workspace.upload(session_id, "input", "empty.csv", b"")
    assert response.status_code == 400
    folder = workspace.store.session_dir(session_id)
    assert not leftovers(folder)
    assert workspace.get(f"/sessions/{session_id}").json()["files"]["input"] == []


def test_an_oversized_file_is_refused_while_streaming(workspace, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_UPLOAD_BYTES", 1000)
    session_id = workspace.new_session("clean")
    data = b"Code,Name\n" + b"A-1,widget\n" * 200
    response = workspace.upload(session_id, "input", "big.csv", data)
    assert response.status_code == 413
    assert "larger than" in response.json()["error"]
    folder = workspace.store.session_dir(session_id)
    assert not leftovers(folder)
    assert not list((folder / "inputs").rglob("*.csv"))


def test_a_declared_oversized_file_is_refused_before_reading(workspace):
    session_id = workspace.new_session("clean")
    response = workspace.put(
        f"/sessions/{session_id}/files/input",
        params={"name": "huge.csv"},
        content=b"a,b\n1,2\n",
        headers={"Content-Length": str(store_module.MAX_UPLOAD_BYTES + 1)},
    )
    assert response.status_code == 413


def test_a_damaged_workbook_is_shown_as_unreadable_and_cannot_run(workspace):
    session_id = workspace.new_session("clean")
    response = workspace.upload(
        session_id, "input", "stock.xlsx", b"PK\x03\x04 not really"
    )
    assert response.status_code == 200
    card = response.json()["file"]
    assert card["errors"] and "xlsx" in card["errors"][0]

    check = workspace.check(session_id, {"schema_version": 1})
    assert check["readiness"] == "blocked"
    assert "cannot be read" in check["problems"][0]["message"]
    response = workspace.post(f"/sessions/{session_id}/runs", {"settings": {}})
    assert response.status_code == 409
    assert not workspace.store.run_names(workspace.store.get(session_id))


def test_a_file_in_another_encoding_is_explained(workspace):
    session_id = workspace.new_session("clean")
    data = "Code,Name\nA-1,Café\n".encode("utf-16")
    card = workspace.upload(session_id, "input", "export.csv", data).json()["file"]
    assert card["errors"] and "encoding" in card["errors"][0]


def test_a_comparison_without_a_record_key_is_blocked(workspace):
    view = workspace.sample("inventory-compare")
    session_id = view["session"]["id"]
    settings = view["session"]["settings"]
    settings["table"]["key"] = []
    check = workspace.check(session_id, settings)
    assert check["readiness"] == "blocked" and not check["ready"]
    assert any("never by row position" in p["message"] for p in check["problems"])
    response = workspace.post(f"/sessions/{session_id}/runs", {"settings": settings})
    assert response.status_code == 409
    assert response.json()["check"]["readiness"] == "blocked"
    assert not workspace.store.run_names(workspace.store.get(session_id))


def test_required_files_must_be_added_before_checking(workspace):
    session_id = workspace.new_session("reconcile")
    check = workspace.check(session_id, {"schema_version": 1})
    assert check["readiness"] == "blocked"
    assert {p["where"] for p in check["problems"]} == {"files.orders", "files.payments"}


def test_a_blocking_preflight_finding_stops_the_run(workspace):
    session_id = workspace.new_session("clean")
    data = b"Code,Name,Card Number\nA-1,Widget,not-a-real-card\n"
    workspace.upload(session_id, "input", "payments.csv", data)
    settings = workspace.suggest(session_id)["settings"]
    # every column is kept by default, including the card-like one
    assert any(c["source"] == "Card Number" for c in settings["columns"].values())
    check = workspace.check(session_id, settings)
    assert check["readiness"] == "blocked", check
    blocking = [f for f in check["preflight"]["findings"] if f["level"] == "blocking"]
    assert blocking and "Card Number" in blocking[0]["message"]
    response = workspace.post(f"/sessions/{session_id}/runs", {"settings": settings})
    assert response.status_code == 409
    assert not workspace.store.run_names(workspace.store.get(session_id))


def test_identical_files_are_pointed_out(workspace):
    session_id = workspace.new_session("compare")
    data = (INVENTORY / "inventory-before.csv").read_bytes()
    workspace.upload(session_id, "before", "week-1.csv", data)
    response = workspace.upload(session_id, "after", "week-2.csv", data)
    notes = response.json()["view"]["notes"]
    assert notes and "exactly the same contents" in notes[0]
    assert "shows no changes" in notes[0]


def test_duplicate_records_are_held_back_not_guessed(workspace):
    session_id = workspace.new_session("clean")
    data = (
        "SKU,Name,Price\n"
        "A-1,Widget,1.00\n"
        "A-1,Widget,1.00\n"
        "A-2,Gadget,2.00\n"
        "A-2,Gadget,2.50\n"
    ).encode()
    workspace.upload(session_id, "input", "dupes.csv", data)
    settings = workspace.suggest(session_id)["settings"]
    assert settings["table"]["key"] == ["sku"]
    job = workspace.run(session_id, settings)
    summary = workspace.get(f"/sessions/{session_id}/runs/{job['run']}").json()
    assert summary["counts"]["rows_written"] == 1
    assert summary["counts"]["duplicate_rows_removed"] == 1
    assert summary["counts"]["conflicting_keys"] == 1
    codes = {group["code"]: group["rows"] for group in summary["attention"]}
    assert codes == {"conflicting_key": 2, "duplicate_row": 1}


def test_a_stopped_run_leaves_nothing_behind(tmp_path):
    workspace = Workspace(tmp_path / "workspace", runner=ScriptRunner(SLOW_PARTIAL_RUN))
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    job = workspace.post(
        f"/sessions/{session_id}/runs", {"settings": workspace.settings(session_id)}
    ).json()["job"]
    session = workspace.store.get(session_id)
    runs = workspace.store.runs_dir(session)
    deadline = time.monotonic() + 30
    while not list(runs.glob(".run-*.partial")) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert list(runs.glob(".run-*.partial")), "the slow run never started"

    # a second run is refused while one is in progress
    busy = workspace.post(
        f"/sessions/{session_id}/runs", {"settings": workspace.settings(session_id)}
    )
    assert busy.status_code == 409
    assert workspace.delete(f"/sessions/{session_id}").status_code == 409

    assert workspace.post(f"/jobs/{job['id']}/cancel").status_code == 200
    finished = workspace.wait(job["id"])
    assert finished["state"] == "cancelled"
    assert not list(runs.iterdir())
    assert workspace.get(f"/sessions/{session_id}").json()["runs"] == []


def test_a_run_that_takes_too_long_is_stopped(tmp_path):
    runner = ScriptRunner(SLOW_PARTIAL_RUN, timeout=1)
    workspace = Workspace(tmp_path / "workspace", runner=runner)
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    job = workspace.run(session_id)
    assert job["state"] == "cancelled"
    assert "too long" in job["note"]
    session = workspace.store.get(session_id)
    assert not list(workspace.store.runs_dir(session).iterdir())


def test_a_failed_run_explains_itself_and_is_cleaned_up(tmp_path):
    workspace = Workspace(tmp_path / "workspace", runner=ScriptRunner(FAILING_RUN))
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    job = workspace.run(session_id)
    assert job["state"] == "failed" and job["exit_code"] == 3
    assert "could not read the files" in job["headline"]
    assert job["advice"]
    assert any("could not be parsed" in line for line in job["messages"])
    # the CLI's prefix is removed for the screen
    assert not any(line.startswith("ordixa: ") for line in job["messages"])
    session = workspace.store.get(session_id)
    assert not list(workspace.store.runs_dir(session).iterdir())


def test_an_interrupted_run_is_reported_and_can_be_cleared(workspace):
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    session = workspace.store.get(session_id)
    staging = workspace.store.runs_dir(session) / ".run-20261112T000000Z-abcdef.partial"
    staging.mkdir()
    (staging / "cleaned_data.csv").write_text("half")

    view = workspace.get(f"/sessions/{session_id}").json()
    assert view["incomplete"] == [staging.name]
    assert view["runs"] == []
    listing = workspace.get("/sessions").json()
    assert listing[0]["incomplete"] == 1

    # the command line also refuses to treat it as a result
    assert cli_main(["verify", "--run", str(staging)]) == 3

    response = workspace.delete(f"/sessions/{session_id}/incomplete")
    assert response.json() == {"removed": 1}
    assert not staging.exists()


def test_the_child_process_never_inherits_debug_output(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDIXA_DEBUG", "1")
    script = "import os, sys; print('debug=' + str(os.environ.get('ORDIXA_DEBUG')))"
    workspace = Workspace(tmp_path / "workspace", runner=ScriptRunner(script))
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    job = workspace.run(session_id)
    assert "debug=None" in job["messages"]


def test_removing_a_session_deletes_its_files_and_results(workspace):
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    workspace.run(session_id)
    folder = workspace.store.session_dir(session_id)
    assert workspace.delete(f"/sessions/{session_id}").status_code == 200
    assert not folder.exists()
    assert workspace.get(f"/sessions/{session_id}").status_code == 404


def test_replacing_a_file_removes_the_previous_copy(workspace):
    session_id = workspace.new_session("clean")
    workspace.upload(session_id, "input", "first.csv", b"Code\nA-1\n")
    workspace.upload(session_id, "input", "second.csv", b"Code\nB-1\n")
    session = workspace.store.get(session_id)
    folder = workspace.store.input_dir(session, "input")
    assert sorted(path.name for path in folder.iterdir()) == ["second.csv"]
    assert [item["original_name"] for item in session["files"]["input"]] == [
        "second.csv"
    ]
