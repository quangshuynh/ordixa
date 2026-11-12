"""
security of the local workspace: it answers only this computer, only its own
page, never loads or sends anything elsewhere, never lets a path escape its
data folder, and never renders file contents as HTML
"""

import re

import pytest
from fastapi.testclient import TestClient
from workspace_helpers import BASE_URL, ROOT, Workspace

from app.ordixa.cli import build_parser
from app.workspace import server
from app.workspace.store import Store, WorkspaceError, safe_file_name

STATIC = ROOT / "app" / "workspace" / "static"


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path / "workspace")


def test_requests_for_another_host_name_are_refused(workspace):
    # a DNS-rebinding page reaches the port under its own host name
    client = TestClient(workspace.app, base_url="http://attacker.example:8765")
    assert client.get("/").status_code == 421
    assert client.get("/api/status").status_code == 421
    assert client.get("/assets/app.js").status_code == 421


def test_the_api_needs_the_cookie_from_the_page(workspace):
    stranger = TestClient(workspace.app, base_url=BASE_URL)
    response = stranger.get("/api/sessions")
    assert response.status_code == 401
    assert stranger.post("/api/sessions", json={"workflow": "clean"}).status_code == 401


def test_changes_need_the_page_token(workspace):
    response = workspace.client.post("/api/sessions", json={"workflow": "clean"})
    assert response.status_code == 403
    response = workspace.client.post(
        "/api/sessions", json={"workflow": "clean"}, headers={"X-Ordixa-Token": "guess"}
    )
    assert response.status_code == 403
    assert workspace.post("/sessions", {"workflow": "clean"}).status_code == 200


def test_cross_site_requests_are_refused(workspace):
    headers = {**workspace.headers, "Origin": "https://attacker.example"}
    assert (
        workspace.client.post(
            "/api/sessions", json={"workflow": "clean"}, headers=headers
        ).status_code
        == 403
    )
    headers = {**workspace.headers, "Sec-Fetch-Site": "cross-site"}
    assert workspace.client.get("/api/sessions", headers=headers).status_code == 403
    same = {**workspace.headers, "Origin": BASE_URL, "Sec-Fetch-Site": "same-origin"}
    assert (
        workspace.client.post(
            "/api/sessions", json={"workflow": "clean"}, headers=same
        ).status_code
        == 200
    )


def test_the_session_cookie_is_strict_and_hidden_from_scripts(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    cookie = workspace.client.get("/").headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    # each launch has a new token
    assert Workspace(tmp_path / "other").token != workspace.token


def test_every_response_carries_the_security_headers(workspace):
    for path in ("/", "/assets/app.js", "/api/status"):
        response = workspace.client.get(path)
        policy = response.headers["content-security-policy"]
        assert "default-src 'none'" in policy
        assert "connect-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert workspace.client.get("/api/status").headers["cache-control"] == "no-store"
    # no interactive API documentation is published
    assert workspace.client.get("/docs").status_code == 404
    assert workspace.client.get("/openapi.json").status_code == 404


def test_the_page_loads_nothing_from_the_internet():
    for path in STATIC.iterdir():
        text = path.read_text("utf-8")
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), path.name
        assert "@import" not in text and "fonts.googleapis" not in text


def test_file_contents_are_never_rendered_as_html():
    script = (STATIC / "app.js").read_text("utf-8")
    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert unsafe not in script, unsafe
    assert "textContent" in script or "createTextNode" in script
    page = (STATIC / "index.html").read_text("utf-8")
    assert (
        "<script>" not in page
        and " on"
        not in re.sub(r"<noscript>.*?</noscript>", "", page).split("<body>")[0]
    )


@pytest.mark.parametrize(
    "name, expected",
    [
        ("../../escape.csv", "escape.csv"),
        ("..\\..\\windows.csv", "windows.csv"),
        ("C:\\Users\\x\\orders*.csv", "orders_.csv"),
        ("orders[2026].csv", "orders_2026_.csv"),
        ("  .hidden.csv", "hidden.csv"),
        ("Résumé of sales.CSV", "Resume of sales.csv"),
        ("payments.XLSX", "payments.xlsx"),
        ("<script>.csv", "_script_.csv"),
    ],
)
def test_file_names_are_made_safe(name, expected):
    assert safe_file_name(name) == expected


def test_a_hostile_file_name_stays_inside_the_session(workspace):
    session_id = workspace.new_session("clean")
    response = workspace.upload(
        session_id, "input", "../../../../outside.csv", b"Code\nA-1\n"
    )
    assert response.status_code == 200
    session_folder = workspace.store.session_dir(session_id)
    stored = list((session_folder / "inputs").rglob("*.csv"))
    assert [path.name for path in stored] == ["outside.csv"]
    assert not (workspace.store.root / "outside.csv").exists()
    assert not (workspace.store.root.parent / "outside.csv").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/sessions/..%2F..%2Fetc",
        "/sessions/not-a-session",
        "/sessions/{sid}/files/input/..%2Fsession",
        "/sessions/{sid}/files/payments/abcdef12",
        "/sessions/{sid}/runs/..%2Fsession.json",
        "/sessions/{sid}/runs/run-20261112T000000Z-abcdef",
        "/sessions/{sid}/runs/{run}/download/..%2F..%2Fsession.json",
        "/sessions/{sid}/runs/{run}/download/session.json",
        "/sessions/{sid}/runs/{run}/table/..%2F..%2Fsession.json",
    ],
)
def test_paths_cannot_reach_outside_what_a_run_lists(workspace, path):
    view = workspace.sample("inventory-clean")
    session_id = view["session"]["id"]
    job = workspace.run(session_id)
    response = workspace.get(path.format(sid=session_id, run=job["run"]))
    assert response.status_code in (404, 400), (path, response.status_code)
    assert "workflow" not in response.text


def test_requests_are_size_limited(workspace):
    session_id = workspace.new_session("clean")
    big = b'{"settings": "' + b"x" * (server.MAX_JSON_BYTES + 10) + b'"}'
    response = workspace.client.put(
        f"/api/sessions/{session_id}/settings",
        content=big,
        headers={**workspace.headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    response = workspace.client.post(
        f"/api/sessions/{session_id}/settings/import",
        content=b"#" * (server.MAX_PROFILE_BYTES + 10),
        headers=workspace.headers,
    )
    assert response.status_code == 413


def test_imported_settings_must_be_a_profile_for_this_task(workspace):
    session_id = workspace.new_session("clean")
    path = f"/sessions/{session_id}/settings/import"
    headers = workspace.headers
    profile = (ROOT / "examples/ordixa/profiles/harbor-lane.toml").read_bytes()
    response = workspace.client.post(f"/api{path}", content=profile, headers=headers)
    assert response.status_code == 400 and "reconcile" in response.json()["error"]
    response = workspace.client.post(
        f"/api{path}", content=b"not = [toml", headers=headers
    )
    assert response.status_code == 400
    profile = (ROOT / "examples/ordixa/profiles/inventory-clean.toml").read_bytes()
    response = workspace.client.post(f"/api{path}", content=profile, headers=headers)
    assert response.status_code == 200
    assert response.json()["settings"]["table"]["key"] == ["sku"]


def test_the_data_folder_must_not_be_committed(tmp_path):
    with pytest.raises(OSError):
        server.check_data_dir(ROOT / "app" / "workspace-data")
    with pytest.raises(OSError):
        server.check_data_dir(ROOT / "examples" / "workspace")
    assert server.check_data_dir(ROOT / "customer-data" / "workspace-test-only")
    assert server.check_data_dir(tmp_path / "anywhere-else")


def test_the_server_listens_on_this_computer_only(tmp_path, monkeypatch):
    calls = {}

    def fake_run(app, **kwargs):
        calls.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    server.serve(tmp_path / "data", 8799, open_browser=False)
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8799


def test_the_cli_offers_the_workspace_without_changing_other_commands():
    parser = build_parser()
    args = parser.parse_args(["workspace", "--port", "8800", "--no-browser"])
    assert args.port == 8800 and args.no_browser and args.data_dir is None
    with pytest.raises(SystemExit):
        parser.parse_args(["workspace", "--port", "80"])
    with pytest.raises(SystemExit):
        parser.parse_args(["workspace", "--host", "0.0.0.0"])
    run = parser.parse_args(
        ["run", "--profile", "p.toml", "--input", "in", "--out", "out"]
    )
    assert run.command == "run"


def test_too_many_files_for_a_role_are_refused(workspace, monkeypatch):
    from app.workspace import store as store_module

    monkeypatch.setitem(store_module.MAX_FILES, "orders", 2)
    session_id = workspace.new_session("reconcile")
    for index in range(2):
        response = workspace.upload(
            session_id, "orders", f"orders-{index}.csv", b"Order\n1\n"
        )
        assert response.status_code == 200
    response = workspace.upload(session_id, "orders", "orders-3.csv", b"Order\n1\n")
    assert response.status_code == 409


def test_unknown_workflows_and_roles_are_refused(workspace, tmp_path):
    assert (
        workspace.post("/sessions", {"workflow": "delete-everything"}).status_code
        == 400
    )
    session_id = workspace.new_session("clean")
    assert (
        workspace.upload(session_id, "orders", "orders.csv", b"a\n1\n").status_code
        == 400
    )
    with pytest.raises(WorkspaceError):
        Store(tmp_path / "plain").session_dir("../../etc")
