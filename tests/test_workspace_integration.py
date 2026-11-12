"""
the workspace must produce exactly what the command line produces: the same
run directories, fingerprints, verified files, and exact decimal totals
"""

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from workspace_helpers import EXAMPLES, Workspace

from app.ordixa.cli import main as cli_main
from app.ordixa.manifest import MANIFEST_NAME, verify_run, write_run_outputs
from app.ordixa.pipeline import process
from app.ordixa.profile import load_profile
from app.ordixa.tabular import clean_table, compare_tables
from app.ordixa.tabular_outputs import write_clean_run, write_compare_run
from app.ordixa.tabular_profile import load_table_profile

INVENTORY = EXAMPLES / "exports" / "inventory"
DEMO = EXAMPLES / "exports" / "demo"
PROFILES = EXAMPLES / "profiles"


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path / "workspace")


def engine_run(sample, out_dir):
    """
    runs the engine directly, as the CLI does, for the same fictional files
    """
    started = datetime.now(timezone.utc)
    if sample == "inventory-clean":
        profile = load_table_profile(PROFILES / "inventory-clean.toml")
        result = clean_table(profile, INVENTORY / "inventory-before.csv")
        return write_clean_run(result, out_dir, started)
    if sample == "inventory-compare":
        profile = load_table_profile(PROFILES / "inventory-compare.toml")
        result = compare_tables(
            profile,
            INVENTORY / "inventory-before.csv",
            INVENTORY / "inventory-after.csv",
        )
        return write_compare_run(result, out_dir, started)
    profile = load_profile(PROFILES / "harbor-lane.toml")
    return write_run_outputs(process(profile, DEMO), out_dir, started)


@pytest.mark.parametrize(
    "sample", ["inventory-clean", "inventory-compare", "harbor-lane"]
)
def test_a_workspace_run_matches_the_engine_run_exactly(workspace, tmp_path, sample):
    view = workspace.sample(sample)
    session_id = view["session"]["id"]
    job = workspace.run(session_id)
    assert job["state"] == "completed", job
    run_dir = workspace.run_dir(session_id, job["run"])
    ours = json.loads((run_dir / MANIFEST_NAME).read_text("utf-8"))

    _, theirs = engine_run(sample, tmp_path / "engine")
    assert (
        ours["result"]["result_fingerprint"] == theirs["result"]["result_fingerprint"]
    )
    assert ours["result"]["artifact_rows"] == theirs["result"]["artifact_rows"]
    for mine, other in zip(ours["outputs"], theirs["outputs"]):
        assert mine["file"] == other["file"]
        if mine["deterministic"]:
            assert mine["sha256"] == other["sha256"]

    # the CLI's own verify accepts the run, inputs included
    summary = workspace.get(f"/sessions/{session_id}/runs/{job['run']}/verify").json()
    assert summary["passed"] and len(summary["checks"]) >= 5
    session = workspace.store.get(session_id)
    locations = [
        str(workspace.store.input_dir(session, role)) for role in session["files"]
    ]
    argv = ["verify", "--run", str(run_dir)]
    for location in dict.fromkeys(locations):
        argv += ["--input", location]
    assert cli_main(argv) == 0


def test_the_settings_a_run_used_work_with_the_command_line(workspace, tmp_path):
    session_id = workspace.sample("inventory-compare")["session"]["id"]
    job = workspace.run(session_id)
    profile = workspace.get(
        f"/sessions/{session_id}/runs/{job['run']}/download/profile.toml"
    )
    assert profile.status_code == 200
    saved = tmp_path / "saved.toml"
    saved.write_bytes(profile.content)
    out = tmp_path / "cli-runs"
    code = cli_main(
        [
            "compare",
            "--profile",
            str(saved),
            "--before",
            str(INVENTORY / "inventory-before.csv"),
            "--after",
            str(INVENTORY / "inventory-after.csv"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    cli_manifest = json.loads(next(out.glob("run-*/manifest.json")).read_text("utf-8"))
    ours = json.loads(
        (workspace.run_dir(session_id, job["run"]) / MANIFEST_NAME).read_text("utf-8")
    )
    assert (
        cli_manifest["result"]["profile"]["sha256"]
        == ours["result"]["profile"]["sha256"]
    )
    assert (
        cli_manifest["result"]["result_fingerprint"]
        == ours["result"]["result_fingerprint"]
    )


def test_downloads_are_the_exact_verified_files(workspace):
    session_id = workspace.sample("harbor-lane")["session"]["id"]
    job = workspace.run(session_id)
    run_dir = workspace.run_dir(session_id, job["run"])
    manifest = json.loads((run_dir / MANIFEST_NAME).read_text("utf-8"))
    base = f"/sessions/{session_id}/runs/{job['run']}"
    for output in manifest["outputs"]:
        response = workspace.get(f"{base}/download/{output['file']}")
        assert response.status_code == 200
        assert response.content == (run_dir / output["file"]).read_bytes()
        assert "attachment" in response.headers["content-disposition"]

    bundle = zipfile.ZipFile(io.BytesIO(workspace.get(f"{base}/download.zip").content))
    names = set(bundle.namelist())
    for output in manifest["outputs"]:
        member = f"{run_dir.name}/{output['file']}"
        assert member in names
        assert bundle.read(member) == (run_dir / output["file"]).read_bytes()
    assert f"{run_dir.name}/{MANIFEST_NAME}" in names
    assert f"{run_dir.name}-profile.toml" in names
    # the temporary archive is not left behind in the session folder
    assert not list(workspace.store.session_dir(session_id).glob(".download-*"))


def test_a_changed_output_is_never_offered_as_the_verified_result(workspace):
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    job = workspace.run(session_id)
    run_dir = workspace.run_dir(session_id, job["run"])
    cleaned = run_dir / "cleaned_data.csv"
    cleaned.write_bytes(cleaned.read_bytes().replace(b"68.12", b"86.12"))
    base = f"/sessions/{session_id}/runs/{job['run']}"

    assert workspace.get(f"{base}/download/cleaned_data.csv").status_code == 409
    assert workspace.get(f"{base}/download.zip").status_code == 409
    verify = workspace.get(f"{base}/verify").json()
    assert not verify["passed"]
    assert any(
        not c["passed"] and "cleaned_data.csv" in c["check"] for c in verify["checks"]
    )
    # other outputs are still served
    assert workspace.get(f"{base}/download/cleaning_issues.csv").status_code == 200


def test_amounts_reach_the_screen_as_exact_text(workspace):
    session_id = workspace.sample("harbor-lane")["session"]["id"]
    job = workspace.run(session_id)
    base = f"/sessions/{session_id}/runs/{job['run']}"
    summary = workspace.get(base).json()
    # hand-computed totals for the fictional October demo (docs/DEMO.md)
    assert summary["totals"]["order_value"] == "4837.40"
    assert summary["totals"]["net_received"] == "4063.65"
    assert summary["totals"]["outstanding_balance"] == "922.75"
    assert summary["currency"] == "USD"

    page = workspace.get(
        f"{base}/table/reconciliation_ledger.csv", params={"limit": 500}
    ).json()
    total = page["header"].index("order_total")
    assert all(isinstance(cell, str) for row in page["rows"] for cell in row)
    assert "1240.00" in [row[total] for row in page["rows"]]
    run_dir = workspace.run_dir(session_id, job["run"])
    text = (run_dir / "reconciliation_ledger.csv").read_text("utf-8")
    for row in page["rows"]:
        assert ",".join(row[:3]) in text


def test_table_pages_filter_and_never_leave_the_listed_files(workspace):
    session_id = workspace.sample("harbor-lane")["session"]["id"]
    job = workspace.run(session_id)
    base = f"/sessions/{session_id}/runs/{job['run']}"
    exceptions = workspace.get(
        f"{base}/table/reconciliation_ledger.csv",
        params={"column": "financial_status", "value": "paid", "exclude": "true"},
    ).json()
    statuses = {
        row[exceptions["header"].index("financial_status")]
        for row in exceptions["rows"]
    }
    assert exceptions["total"] == 6 and "paid" not in statuses
    issues = workspace.get(
        f"{base}/table/rejected_rows.csv",
        params={"column": "code", "value": "duplicate_row"},
    ).json()
    assert issues["total"] == 2
    assert workspace.get(f"{base}/table/manifest.json").status_code == 404
    assert workspace.get(f"{base}/table/..%2Fsession.json").status_code == 404


def test_results_explain_held_back_rows_in_plain_language(workspace):
    session_id = workspace.sample("inventory-compare")["session"]["id"]
    job = workspace.run(session_id)
    summary = workspace.get(f"/sessions/{session_id}/runs/{job['run']}").json()
    groups = {group["code"]: group for group in summary["attention"]}
    assert groups["conflicting_key"]["rows"] == 2
    assert "share this ID" in groups["conflicting_key"]["what"]
    assert groups["missing_value"]["fix"]
    assert summary["assessment"]["level"] == "incomplete"
    assert summary["counts"]["added"] == 6


def test_reruns_keep_every_earlier_result(workspace):
    session_id = workspace.sample("inventory-clean")["session"]["id"]
    first = workspace.run(session_id)
    second = workspace.run(session_id)
    assert first["run"] != second["run"]
    view = workspace.get(f"/sessions/{session_id}").json()
    assert [run["run"] for run in view["runs"]] == sorted(
        [first["run"], second["run"]], reverse=True
    )
    for name in (first["run"], second["run"]):
        assert verify_run(workspace.run_dir(session_id, name))


def test_a_new_session_from_uploaded_files_runs_with_suggested_settings(workspace):
    session_id = workspace.new_session("compare")
    for role, name in (
        ("before", "inventory-before.csv"),
        ("after", "inventory-after.csv"),
    ):
        response = workspace.upload(
            session_id, role, name, (INVENTORY / name).read_bytes()
        )
        assert response.status_code == 200, response.text
    suggestion = workspace.suggest(session_id)
    settings = suggestion["settings"]
    settings["table"]["date_formats"].append("%m/%d/%Y")
    check = workspace.check(session_id, settings)
    assert check["ready"], check
    job = workspace.run(session_id, settings)
    assert job["state"] == "completed", job
    summary = workspace.get(f"/sessions/{session_id}/runs/{job['run']}").json()
    assert summary["counts"]["added"] + summary["counts"]["removed"] > 0
    # the input copies are byte-for-byte what was chosen
    session = workspace.store.get(session_id)
    stored = workspace.store.input_paths(session, "after")[0]
    assert stored.read_bytes() == (INVENTORY / "inventory-after.csv").read_bytes()


def test_spreadsheet_formulas_in_a_file_are_neutralized_in_the_outputs(workspace):
    session_id = workspace.new_session("clean")
    data = (
        "Code,Name,Price\n"
        'A-1,=HYPERLINK("http://example.com"),1.00\n'
        "A-2,+SUM(A1:A9),2.00\n"
        "A-3,<script>alert(1)</script>,3.00\n"
    ).encode()
    assert (
        workspace.upload(session_id, "input", "formulas.csv", data).status_code == 200
    )
    settings = workspace.suggest(session_id)["settings"]
    job = workspace.run(session_id, settings)
    assert job["state"] == "completed", job
    page = workspace.get(
        f"/sessions/{session_id}/runs/{job['run']}/table/cleaned_data.csv"
    ).json()
    names = [row[page["header"].index("name")] for row in page["rows"]]
    assert names == [
        '\'=HYPERLINK("http://example.com")',
        "'+SUM(A1:A9)",
        "<script>alert(1)</script>",
    ]
