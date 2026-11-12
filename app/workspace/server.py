"""
the local workspace web server

it listens only on this computer's loopback address and serves one page plus
a small JSON API. Requests must carry the Host the server was started with
(which defeats DNS rebinding), the session cookie set when the page loads
(SameSite=Strict, so other sites cannot use it), and, for anything that
changes data, a per-launch token in a custom header (which browsers will not
send cross-site without permission this server never grants). The page loads
nothing from the internet, and the content security policy forbids it from
connecting anywhere but this server
"""

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import webbrowser
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.ordixa import __version__
from app.ordixa.cli import IGNORED_DIRS, PROJECT_ROOT, is_within
from app.ordixa.manifest import MANIFEST_NAME, verify_run
from app.ordixa.preflight import preflight
from app.ordixa.profile import ProfileError
from app.ordixa.table_preflight import table_preflight
from app.workspace import preview, profiles, results
from app.workspace.jobs import JobRunner
from app.workspace.store import (
    MAX_FILES,
    MAX_UPLOAD_BYTES,
    REQUIRED_ROLES,
    ROLES,
    Store,
    WorkspaceError,
)

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = PROJECT_ROOT / "customer-data" / "workspace"
COOKIE = "ordixa_workspace"
TOKEN_HEADER = "x-ordixa-token"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PROFILE_BYTES = 256 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# verify_run's check descriptions, reworded for the screen
CHECK_TEXT = (
    (
        re.compile(r"^(.+) matches its recorded SHA-256 and size$"),
        r"\1 is exactly the file the run wrote",
    ),
    (
        re.compile(r"^(.+) has the recorded (\d+) data rows$"),
        r"\1 still has the \2 rows the run recorded",
    ),
    (
        re.compile(r"^input (.+) is unchanged since the run$"),
        r"\1 is still the same file the run read",
    ),
    (
        re.compile(r"^(workbook|.+\.xlsx) contains no formulas$"),
        r"\1 contains no formulas",
    ),
)


def check_data_dir(data_dir):
    """
    refuses a data folder that could end up committed with the code
    :param data_dir: folder that will hold customer files and results
    :returns: resolved path
    """
    data_dir = Path(data_dir).resolve()
    if is_within(data_dir, PROJECT_ROOT) and not any(
        is_within(data_dir, PROJECT_ROOT / name) for name in IGNORED_DIRS
    ):
        raise OSError(
            f"{data_dir} is inside the repository but not in a git-ignored folder; "
            "use customer-data/ or a folder outside the repository"
        )
    return data_dir


def display_path(path):
    """
    shows a folder relative to the project when it is inside it
    :param path: absolute path
    :returns: text such as customer-data/workspace
    """
    path = Path(path)
    if is_within(path, PROJECT_ROOT):
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    return str(path)


def error_response(error):
    """
    turns a refusal into a JSON response
    :param error: workspace error
    :returns: response
    """
    return JSONResponse(
        {"error": error.message, "code": error.code}, status_code=error.status
    )


async def json_body(request, limit=MAX_JSON_BYTES):
    """
    reads a JSON request body with a size limit
    :param request: request
    :param limit: maximum bytes
    :returns: parsed value
    """
    data = await raw_body(request, limit)
    try:
        return json.loads(data or b"{}")
    except ValueError:
        raise WorkspaceError("The request could not be read.", 400) from None


async def raw_body(request, limit):
    """
    reads a request body, refusing more than limit bytes
    :param request: request
    :param limit: maximum bytes
    :returns: bytes
    """
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise WorkspaceError("The request is too large.", 413, "too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    data_dir=DEFAULT_DATA_DIR, port=DEFAULT_PORT, allowed_hosts=None, runner=None
):
    """
    builds the workspace application
    :param data_dir: folder holding sessions, files, and results
    :param port: port the server listens on, used for the Host check
    :param allowed_hosts: Host header values accepted, by default loopback names
    :param runner: job runner, replaceable in tests
    :returns: FastAPI application
    """
    store = Store(check_data_dir(data_dir))
    runner = runner or JobRunner()
    token = secrets.token_urlsafe(32)
    hosts = set(
        allowed_hosts or {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        runner.stop_all()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.runner = runner
    app.state.token = token
    # one run is started at a time, so two quick clicks cannot both pass the
    # "nothing is running" check
    starting = asyncio.Lock()

    @app.middleware("http")
    async def guard(request, call_next):
        host = request.headers.get("host", "")
        if host not in hosts:
            return Response("Unknown host.", status_code=421, headers=SECURITY_HEADERS)
        origin = request.headers.get("origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            return Response(
                "Cross-site request refused.", 403, headers=SECURITY_HEADERS
            )
        if request.headers.get("sec-fetch-site", "same-origin") not in {
            "same-origin",
            "none",
        }:
            return Response(
                "Cross-site request refused.", 403, headers=SECURITY_HEADERS
            )
        if request.url.path.startswith("/api/"):
            cookie = request.cookies.get(COOKIE, "")
            if not hmac.compare_digest(cookie, token):
                return JSONResponse(
                    {
                        "error": "Reload the workspace page to continue.",
                        "code": "session",
                    },
                    status_code=401,
                    headers=SECURITY_HEADERS,
                )
            if request.method not in ("GET", "HEAD") and not hmac.compare_digest(
                request.headers.get(TOKEN_HEADER, ""), token
            ):
                return JSONResponse(
                    {
                        "error": "Reload the workspace page to continue.",
                        "code": "token",
                    },
                    status_code=403,
                    headers=SECURITY_HEADERS,
                )
        try:
            response = await call_next(request)
        except WorkspaceError as error:
            response = error_response(error)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(WorkspaceError)
    async def refused(request, error):
        return error_response(error)

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = (STATIC / "index.html").read_text("utf-8")
        page = page.replace("{{TOKEN}}", token).replace("{{VERSION}}", __version__)
        response = HTMLResponse(page, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    # helpers bound to this application

    def describe_cached(session, role, entry):
        cache = store.session_dir(session["id"]) / "cache"
        cache.mkdir(exist_ok=True)
        path = cache / f"{entry['id']}.json"
        if path.is_file():
            return json.loads(path.read_text("utf-8"))
        folder = store.input_dir(session, role)
        description = preview.describe(folder / entry["name"])
        path.write_text(json.dumps(description, ensure_ascii=False), "utf-8")
        return description

    def first_descriptions(session):
        found = {}
        for role in ROLES[session["workflow"]]:
            entries = session["files"].get(role) or []
            if entries:
                found[role] = describe_cached(session, role, entries[0])
        return found

    def file_card(session, role, entry):
        description = describe_cached(session, role, entry)
        return {
            **entry,
            "role": role,
            "rows": description["rows"],
            "columns": len(description["header"]),
            "errors": description["errors"],
            "warnings": description["warnings"],
        }

    def file_notes(session):
        notes = []
        seen = {}
        for role in ROLES[session["workflow"]]:
            for entry in session["files"].get(role) or []:
                if entry["sha256"] in seen:
                    other = seen[entry["sha256"]]
                    notes.append(
                        f"{entry['original_name']} ({role}) has exactly the same "
                        f"contents as {other[1]} ({other[0]})."
                        + (
                            " Comparing a file with itself shows no changes."
                            if session["workflow"] == "compare"
                            else " Its rows would be counted once."
                        )
                    )
                else:
                    seen[entry["sha256"]] = (role, entry["original_name"])
        return notes

    def run_inputs(session):
        if session["workflow"] == "compare":
            return {
                "before": store.input_paths(session, "before")[0],
                "after": store.input_paths(session, "after")[0],
            }
        if session["workflow"] == "clean":
            return {"input": store.input_paths(session, "input")[0]}
        return {"input": store.input_dir(session, "orders")}

    def input_locations(session):
        if session["workflow"] == "reconcile":
            return [store.input_dir(session, "orders")]
        return [store.input_dir(session, role) for role in ROLES[session["workflow"]]]

    def view(session):
        job = runner.active(session["id"])
        runs = []
        for name in store.run_names(session):
            try:
                runs.append(results.summary(store.run_dir(session, name)))
            except OSError, KeyError, ValueError:
                runs.append({"run": name, "unreadable": True})
        return {
            "session": session,
            "files": {
                role: [file_card(session, role, e) for e in session["files"][role]]
                for role in ROLES[session["workflow"]]
            },
            "notes": file_notes(session),
            "runs": runs,
            "incomplete": store.incomplete_runs(session),
            "job": job.public() if job else None,
            "limits": {"bytes": MAX_UPLOAD_BYTES, "files": MAX_FILES},
        }

    def check(session, document):
        workflow = session["workflow"]
        missing = [
            role for role in REQUIRED_ROLES[workflow] if not session["files"].get(role)
        ]
        if missing:
            return {
                "ready": False,
                "readiness": "blocked",
                "problems": [
                    profiles.problem(f"Add the {role} file first.", f"files.{role}")
                    for role in missing
                ],
                "preflight": None,
            }, None
        descriptions = first_descriptions(session)
        broken = [
            profiles.problem(
                f"{session['files'][role][0]['original_name']} cannot be read: "
                + "; ".join(d["errors"]),
                f"files.{role}",
            )
            for role, d in descriptions.items()
            if d["errors"]
        ]
        if broken:
            return {
                "ready": False,
                "readiness": "blocked",
                "problems": broken,
                "preflight": None,
            }, None
        names = {
            role: [entry["name"] for entry in session["files"].get(role) or []]
            for role in ROLES[workflow]
        }
        document = profiles.for_run(workflow, document, names)
        headers = {role: d["header"] for role, d in descriptions.items()}
        problems, text, profile = profiles.validate(workflow, document, headers)
        outcome = {
            "problems": problems,
            "preflight": None,
            "ready": False,
            "readiness": "blocked",
        }
        if text is None:
            return outcome, None
        inputs = run_inputs(session)
        try:
            if workflow == "reconcile":
                report = preflight(profile, inputs["input"])
            else:
                pairs = [(role, path) for role, path in inputs.items()]
                report = table_preflight(profile, pairs)
        except (ProfileError, OSError, ValueError) as error:
            outcome["problems"].append(
                profiles.problem(f"The files could not be checked: {error}")
            )
            return outcome, None
        for item in report["findings"]:
            item.update({k: v for k, v in results.explain(item["code"]).items() if v})
        outcome["preflight"] = report
        outcome["readiness"] = report["readiness"]
        outcome["ready"] = report["readiness"] != "blocked"
        return outcome, text if outcome["ready"] else None

    # API

    @app.get("/api/status")
    def status():
        return {
            "version": __version__,
            "data_dir": display_path(store.root),
            "local_only": True,
            "limits": {"bytes": MAX_UPLOAD_BYTES, "files": MAX_FILES},
            "type_labels": profiles.TYPE_LABELS,
            "date_formats": profiles.DATE_FORMAT_LABELS,
            "field_labels": profiles.FIELD_LABELS,
            "dataset_fields": {
                role: {
                    "required": sorted(f["required"]),
                    "optional": sorted(f["optional"]),
                }
                for role, f in profiles.DATASET_FIELDS.items()
            },
        }

    @app.get("/api/samples")
    def samples():
        return [
            {
                "id": key,
                "workflow": item["workflow"],
                "title": item["title"],
                "description": item["description"],
            }
            for key, item in profiles.SAMPLES.items()
        ]

    @app.post("/api/samples/{sample_id}")
    def open_sample(sample_id: str):
        sample = profiles.SAMPLES.get(sample_id)
        if sample is None:
            raise WorkspaceError("That sample was not found.", 404, "not_found")
        session = store.create(sample["workflow"], sample["title"])
        for role, files in sample["files"].items():
            for name in files:
                store.copy_file(session["id"], role, profiles.EXAMPLES / name)
        session = store.get(session["id"])
        session["settings"] = profiles.sample_document(sample_id)
        session["sample"] = sample_id
        store.save(session)
        return view(session)

    @app.get("/api/sessions")
    def list_sessions():
        items = []
        for session in store.list():
            items.append(
                {
                    "id": session["id"],
                    "workflow": session["workflow"],
                    "title": session["title"],
                    "updated_at": session["updated_at"],
                    "files": sum(len(v) for v in session["files"].values()),
                    "runs": len(store.run_names(session)),
                    "incomplete": len(store.incomplete_runs(session)),
                }
            )
        return items

    @app.post("/api/sessions")
    async def create_session(request: Request):
        body = await json_body(request)
        session = store.create(str(body.get("workflow", "")), body.get("title"))
        return view(session)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        return view(store.get(session_id))

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, request: Request):
        body = await json_body(request)
        session = store.get(session_id)
        session["title"] = str(body.get("title", ""))[:80].strip()
        return view(store.save(session))

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str):
        if runner.active(session_id):
            raise WorkspaceError("Stop the run in progress first.", 409, "busy")
        store.delete(session_id)
        return {"deleted": session_id}

    @app.put("/api/sessions/{session_id}/files/{role}")
    async def add_file(session_id: str, role: str, request: Request, name: str = ""):
        if runner.active(session_id):
            raise WorkspaceError("Wait for the run in progress to finish.", 409, "busy")
        declared = request.headers.get("content-length")
        entry = await store.add_file(
            session_id,
            role,
            name,
            request.stream(),
            int(declared) if declared and declared.isdigit() else None,
        )
        session = store.get(session_id)
        # describing a large file takes a while; keep other requests moving
        card = await run_in_threadpool(file_card, session, role, entry)
        return {"file": card, "view": await run_in_threadpool(view, session)}

    @app.delete("/api/sessions/{session_id}/files/{role}/{file_id}")
    def remove_file(session_id: str, role: str, file_id: str):
        if runner.active(session_id):
            raise WorkspaceError("Wait for the run in progress to finish.", 409, "busy")
        store.remove_file(session_id, role, file_id)
        return view(store.get(session_id))

    @app.get("/api/sessions/{session_id}/files/{role}/{file_id}")
    def describe_file(session_id: str, role: str, file_id: str):
        session = store.get(session_id)
        entry, _ = store.file_entry(session, role, file_id)
        return {"file": entry, "role": role, **describe_cached(session, role, entry)}

    @app.get("/api/sessions/{session_id}/values")
    def column_values(session_id: str, role: str, column: str):
        session = store.get(session_id)
        if role not in ROLES[session["workflow"]]:
            raise WorkspaceError("That kind of file is not used here.", 400)
        return {
            "values": preview.distinct_values(store.input_paths(session, role), column)
        }

    @app.get("/api/sessions/{session_id}/suggestion")
    def suggestion(session_id: str):
        session = store.get(session_id)
        descriptions = first_descriptions(session)
        workflow = session["workflow"]
        first = next(iter(session["files"].values()), [])
        hint = Path(first[0]["original_name"]).stem if first else workflow
        if workflow == "reconcile":
            draft, _ = profiles.suggest_reconcile(descriptions, {}, hint)
            columns = draft["datasets"].get("payments", {}).get("columns", {})
            paths = store.input_paths(session, "payments")
            values = {
                field: preview.distinct_values(paths, columns[field])
                for field in ("transaction_type", "status")
                if columns.get(field)
            }
            draft, questions = profiles.suggest_reconcile(descriptions, values, hint)
        else:
            role = ROLES[workflow][0]
            if role not in descriptions or descriptions[role]["errors"]:
                raise WorkspaceError("Add a readable file first.", 409, "no_file")
            draft, questions = profiles.suggest_table(
                workflow, descriptions[role], hint
            )
        return {"settings": draft, "questions": questions}

    @app.put("/api/sessions/{session_id}/settings")
    async def save_settings(session_id: str, request: Request):
        body = await json_body(request)
        session = store.get(session_id)
        if not isinstance(body.get("settings"), dict):
            raise WorkspaceError("The settings are missing.", 400)
        session["settings"] = body["settings"]
        store.save(session)
        return {"saved": True}

    @app.post("/api/sessions/{session_id}/settings/import")
    async def import_settings(session_id: str, request: Request):
        session = store.get(session_id)
        data = await raw_body(request, MAX_PROFILE_BYTES)
        try:
            document = profiles.parse_toml(data.decode("utf-8"))
        except UnicodeDecodeError, ProfileError:
            raise WorkspaceError(
                "This is not an Ordixa profile (a .toml file).", 400, "bad_profile"
            ) from None
        kind = profiles.document_workflow(document)
        if kind != session["workflow"]:
            raise WorkspaceError(
                f"These settings are for {kind or 'another task'}, not "
                f"{session['workflow']}.",
                400,
                "wrong_profile",
            )
        session["settings"] = document
        store.save(session)
        return {"settings": document}

    @app.get("/api/sessions/{session_id}/settings.toml")
    def export_settings(session_id: str):
        session = store.get(session_id)
        if not session.get("settings"):
            raise WorkspaceError("There are no settings to save yet.", 409)
        try:
            text = profiles.to_toml(session["settings"])
        except Exception:
            raise WorkspaceError(
                "These settings cannot be saved as a profile.", 409
            ) from None
        name = profiles.slug(
            (session["settings"].get("profile") or {}).get("name", "settings")
        )
        return Response(
            text,
            media_type="application/toml",
            headers={"Content-Disposition": f'attachment; filename="{name}.toml"'},
        )

    @app.post("/api/sessions/{session_id}/check")
    async def check_settings(session_id: str, request: Request):
        body = await json_body(request)
        session = store.get(session_id)
        document = body.get("settings")
        if isinstance(document, dict):
            session["settings"] = document
            store.save(session)
        outcome, _ = await run_in_threadpool(check, session, document)
        return outcome

    @app.post("/api/sessions/{session_id}/runs")
    async def start_run(session_id: str, request: Request):
        body = await json_body(request)
        async with starting:
            session = store.get(session_id)
            if runner.active(session_id):
                raise WorkspaceError("A run is already in progress.", 409, "busy")
            document = body.get("settings")
            if isinstance(document, dict):
                session["settings"] = document
                store.save(session)
            outcome, text = await run_in_threadpool(
                check, session, session.get("settings")
            )
            if text is None:
                return JSONResponse(
                    {"error": "Fix the problems listed first.", "check": outcome}, 409
                )
            profile_path = store.save_profile(session, text)
            job = runner.start(
                session_id,
                session["workflow"],
                profile_path,
                run_inputs(session),
                store.runs_dir(session),
            )
        return {"job": job.public(), "check": outcome}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = runner.get(job_id)
        if job is None:
            raise WorkspaceError("That run was not found.", 404, "not_found")
        return job.public()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = runner.get(job_id)
        if job is None:
            raise WorkspaceError("That run was not found.", 404, "not_found")
        threading.Thread(target=runner.stop, args=(job,), daemon=True).start()
        return job.public()

    @app.delete("/api/sessions/{session_id}/incomplete")
    def clear_incomplete(session_id: str):
        session = store.get(session_id)
        if runner.active(session_id):
            raise WorkspaceError("Wait for the run in progress to finish.", 409, "busy")
        return {"removed": store.clear_incomplete(session)}

    def run_dir(session_id, run):
        session = store.get(session_id)
        return session, store.run_dir(session, run)

    @app.get("/api/sessions/{session_id}/runs/{run}")
    def run_summary(session_id: str, run: str):
        session, path = run_dir(session_id, run)
        data = results.summary(path)
        issues = next(
            (
                o["file"]
                for o in data["outputs"]
                if o["file"].endswith(("issues.csv", "rejected_rows.csv"))
            ),
            None,
        )
        data["attention"] = results.issue_groups(path, issues) if issues else []
        data["profile_saved"] = (
            store.profile_for(session, data["profile"].get("sha256")) is not None
        )
        return data

    @app.get("/api/sessions/{session_id}/runs/{run}/table/{name}")
    def run_table(
        session_id: str,
        run: str,
        name: str,
        offset: int = 0,
        limit: int = 100,
        column: str = "",
        value: str = "",
        exclude: bool = False,
    ):
        _, path = run_dir(session_id, run)
        rule = {column: value} if column else None
        try:
            if exclude:
                return results.table(path, name, offset, limit, exclude=rule)
            return results.table(path, name, offset, limit, rule)
        except KeyError:
            raise WorkspaceError(
                "That file is not part of this result.", 404, "not_found"
            ) from None

    @app.get("/api/sessions/{session_id}/runs/{run}/verify")
    def verify(session_id: str, run: str):
        session, path = run_dir(session_id, run)
        checks = []
        for description, passed in verify_run(path, input_locations(session)):
            text = description
            for pattern, replacement in CHECK_TEXT:
                text = pattern.sub(replacement, text)
            checks.append({"check": text, "passed": bool(passed)})
        return {"passed": all(c["passed"] for c in checks), "checks": checks}

    @app.get("/api/sessions/{session_id}/runs/{run}/download/{name}")
    def download(session_id: str, run: str, name: str):
        session, path = run_dir(session_id, run)
        manifest = results.read_manifest(path)
        listed = {o["file"] for o in manifest["outputs"]}
        if name == MANIFEST_NAME:
            return FileResponse(
                path / name, filename=name, media_type="application/json"
            )
        if name == "profile.toml":
            stored = store.profile_for(
                session, manifest["result"]["profile"].get("sha256")
            )
            if stored is None:
                raise WorkspaceError(
                    "The settings for this run are not stored here.", 404
                )
            return FileResponse(
                stored,
                filename=f"{manifest['result']['profile']['name']}.toml",
                media_type="application/toml",
            )
        if name not in listed:
            raise WorkspaceError(
                "That file is not part of this result.", 404, "not_found"
            )
        if not results.output_matches_manifest(path, name):
            raise WorkspaceError(
                "This file has changed since the run wrote it, so it is no longer the "
                "verified result. Run again to get a fresh copy.",
                409,
                "tampered",
            )
        media = (
            "text/csv"
            if name.endswith(".csv")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return FileResponse(path / name, filename=name, media_type=media)

    @app.get("/api/sessions/{session_id}/runs/{run}/download.zip")
    def download_all(session_id: str, run: str):
        session, path = run_dir(session_id, run)
        manifest = results.read_manifest(path)
        bad = [
            o["file"]
            for o in manifest["outputs"]
            if not results.output_matches_manifest(path, o["file"])
        ]
        if bad:
            raise WorkspaceError(
                f"{', '.join(bad)} changed since the run wrote it, so the result is no "
                "longer verified. Run again to get a fresh copy.",
                409,
                "tampered",
            )
        handle = tempfile.NamedTemporaryFile(
            dir=store.session_dir(session_id),
            prefix=".download-",
            suffix=".zip",
            delete=False,
        )
        handle.close()
        archive = Path(handle.name)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for item in manifest["outputs"]:
                bundle.write(path / item["file"], f"{path.name}/{item['file']}")
            bundle.write(path / MANIFEST_NAME, f"{path.name}/{MANIFEST_NAME}")
            stored = store.profile_for(
                session, manifest["result"]["profile"].get("sha256")
            )
            if stored is not None:
                bundle.write(stored, f"{path.name}-profile.toml")
        return FileResponse(
            archive,
            filename=f"ordixa-{path.name}.zip",
            media_type="application/zip",
            background=BackgroundTask(lambda: archive.unlink(missing_ok=True)),
        )

    return app


def serve(data_dir=DEFAULT_DATA_DIR, port=DEFAULT_PORT, open_browser=True):
    """
    starts the workspace on this computer only and opens it in the browser
    :param data_dir: folder holding sessions, files, and results
    :param port: loopback port
    :param open_browser: open the default browser once the server is up
    :returns: none
    """
    import uvicorn

    app = create_app(data_dir, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"ordixa: workspace running at {url}", flush=True)
    print(f"ordixa: files and results are kept in {app.state.store.root}", flush=True)
    print("ordixa: nothing leaves this computer; press Ctrl+C to stop", flush=True)
    if open_browser and not os.environ.get("ORDIXA_NO_BROWSER"):
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
