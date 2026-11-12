"""
shared helpers for the local workspace tests

the client talks to the app exactly as the browser page does: the page is
loaded first to receive the session cookie, and every change carries the
per-launch token header
"""

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.workspace.jobs import JobRunner
from app.workspace.server import create_app

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "ordixa"
BASE_URL = "http://127.0.0.1:8765"
FINISHED = {"completed", "failed", "cancelled"}


class Workspace:
    """a workspace app and a browser-like client for it"""

    def __init__(self, data_dir, runner=None):
        self.app = create_app(data_dir, 8765, runner=runner)
        self.client = TestClient(self.app, base_url=BASE_URL)
        page = self.client.get("/")
        assert page.status_code == 200
        self.token = self.app.state.token
        self.headers = {"X-Ordixa-Token": self.token}
        self.store = self.app.state.store

    def get(self, path, **kwargs):
        return self.client.get(f"/api{path}", **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.client.post(
            f"/api{path}", json=json, headers=self.headers, **kwargs
        )

    def put(self, path, **kwargs):
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return self.client.put(f"/api{path}", headers=headers, **kwargs)

    def delete(self, path):
        return self.client.delete(f"/api{path}", headers=self.headers)

    def new_session(self, workflow):
        response = self.post("/sessions", {"workflow": workflow})
        assert response.status_code == 200, response.text
        return response.json()["session"]["id"]

    def sample(self, sample_id):
        response = self.post(f"/samples/{sample_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def upload(self, session_id, role, name, data):
        return self.put(
            f"/sessions/{session_id}/files/{role}",
            params={"name": name},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    def settings(self, session_id):
        return self.get(f"/sessions/{session_id}").json()["session"]["settings"]

    def suggest(self, session_id):
        response = self.get(f"/sessions/{session_id}/suggestion")
        assert response.status_code == 200, response.text
        return response.json()

    def check(self, session_id, settings=None):
        settings = settings if settings is not None else self.settings(session_id)
        response = self.post(f"/sessions/{session_id}/check", {"settings": settings})
        assert response.status_code == 200, response.text
        return response.json()

    def run(self, session_id, settings=None, timeout=120):
        settings = settings if settings is not None else self.settings(session_id)
        response = self.post(f"/sessions/{session_id}/runs", {"settings": settings})
        assert response.status_code == 200, response.text
        return self.wait(response.json()["job"]["id"], timeout)

    def wait(self, job_id, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get(f"/jobs/{job_id}").json()
            if job["state"] in FINISHED:
                return job
            time.sleep(0.1)
        raise AssertionError(f"job {job_id} did not finish")

    def run_dir(self, session_id, run_name):
        session = self.store.get(session_id)
        return self.store.run_dir(session, run_name)


class ScriptRunner(JobRunner):
    """a job runner whose child process is a small script instead of the CLI

    the script receives the runs folder as its only argument, so tests can
    imitate a slow run, a crash part-way through writing, or a failure exit
    """

    def __init__(self, script, timeout=60):
        super().__init__(timeout=timeout)
        self.script = script

    def command(self, workflow, profile_path, inputs, runs_dir):
        return [sys.executable, "-c", self.script, str(runs_dir)]


SLOW_PARTIAL_RUN = """
import pathlib, sys, time
staging = pathlib.Path(sys.argv[1]) / ".run-20261112T000000Z-abcdef.partial"
staging.mkdir()
(staging / "cleaned_data.csv").write_text("half written")
print("ordixa: started", flush=True)
time.sleep(60)
"""

FAILING_RUN = """
import pathlib, sys
staging = pathlib.Path(sys.argv[1]) / ".run-20261112T000000Z-abcdef.partial"
staging.mkdir()
print("ordixa: input error: orders.csv could not be parsed as CSV", flush=True)
sys.exit(3)
"""
