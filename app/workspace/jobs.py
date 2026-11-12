"""
runs Ordixa commands for the workspace in a child process

every run is the unchanged command line (python -m app.ordixa clean, compare,
or run), so the run directory, manifest, checksums, atomic publication, and
exit codes are exactly those of the CLI. A child process can also be stopped
at any moment: the CLI only publishes run-<id> once every output is written,
so a stopped run leaves at most a hidden .run-<id>.partial staging folder,
which is removed here
"""

import os
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.workspace.store import RUN_NAME

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMMANDS = {"clean": "clean", "compare": "compare", "reconcile": "run"}
MAX_OUTPUT_LINES = 200
# a run that has not finished in this long is stopped; the largest supported
# inputs finish in a few minutes
RUN_TIMEOUT_SECONDS = 60 * 60

QUEUED = "queued"
RUNNING = "running"
CANCELLING = "cancelling"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = {COMPLETED, FAILED, CANCELLED}

# what each CLI exit code means for a person, in the CLI's own terms
EXIT_EXPLANATIONS = {
    1: (
        "Something unexpected went wrong, so nothing was saved.",
        "Try again. If it keeps happening, keep these files aside and ask the "
        "person who set Ordixa up.",
    ),
    2: (
        "These settings could not be used, so nothing was saved.",
        "Go back to the column settings, fix what is listed, and check again.",
    ),
    3: (
        "Ordixa could not read the files, so nothing was saved.",
        "Check that each file is the export you meant to use, then add it again.",
    ),
    4: (
        "Ordixa stopped because its own totals did not add up, so nothing was saved.",
        "This protects you from a wrong result. Keep the files and report it.",
    ),
    5: (
        "The results could not be saved on this computer.",
        "Make sure there is free disk space and the workspace folder is writable.",
    ),
}


def now():
    """
    current UTC time
    :returns: datetime
    """
    return datetime.now(timezone.utc)


class Job:
    """one run of the Ordixa command line"""

    def __init__(self, session_id, workflow, argv, runs_dir):
        self.id = secrets.token_hex(8)
        self.session_id = session_id
        self.workflow = workflow
        self.argv = argv
        self.runs_dir = Path(runs_dir)
        self.state = QUEUED
        self.started_at = now()
        self.finished_at = None
        self.exit_code = None
        self.output = []
        self.run_name = None
        self.cancel_requested = False
        self.note = ""
        self.process = None
        self.lock = threading.Lock()

    def public(self):
        """
        describes the job for the screen
        :returns: dictionary
        """
        explanation = (
            EXIT_EXPLANATIONS.get(self.exit_code) if self.state == FAILED else None
        )
        return {
            "id": self.id,
            "session_id": self.session_id,
            "workflow": self.workflow,
            "state": self.state,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (
                self.finished_at.isoformat(timespec="seconds")
                if self.finished_at
                else None
            ),
            "seconds": round(
                ((self.finished_at or now()) - self.started_at).total_seconds(), 1
            ),
            "exit_code": self.exit_code,
            "run": self.run_name,
            "messages": list(self.output),
            "headline": explanation[0] if explanation else "",
            "advice": explanation[1] if explanation else "",
            "note": self.note,
        }


def clean_line(line):
    """
    drops the CLI's message prefix
    :param line: one line of output
    :returns: text
    """
    line = line.rstrip()
    return line[len("ordixa: ") :] if line.startswith("ordixa: ") else line


class JobRunner:
    """starts, watches, and stops workspace runs"""

    def __init__(self, python=None, timeout=RUN_TIMEOUT_SECONDS):
        self.python = python or sys.executable
        self.timeout = timeout
        self.jobs = {}
        self.lock = threading.Lock()

    def command(self, workflow, profile_path, inputs, runs_dir):
        """
        builds the CLI command for a run
        :param workflow: clean, compare, or reconcile
        :param profile_path: TOML profile on disk
        :param inputs: {"input": path} or {"before":, "after":} or {"dir": path}
        :param runs_dir: parent output folder
        :returns: argument list
        """
        argv = [self.python, "-m", "app.ordixa", COMMANDS[workflow]]
        argv += ["--profile", str(profile_path)]
        if workflow == "compare":
            argv += ["--before", str(inputs["before"]), "--after", str(inputs["after"])]
        else:
            argv += ["--input", str(inputs["input"])]
        return argv + ["--out", str(runs_dir)]

    def active(self, session_id):
        """
        finds a run that has not finished for a session
        :param session_id: identifier
        :returns: job or none
        """
        with self.lock:
            for job in self.jobs.values():
                if job.session_id == session_id and job.state not in FINISHED:
                    return job
        return None

    def get(self, job_id):
        """
        looks up a job
        :param job_id: identifier
        :returns: job or none
        """
        with self.lock:
            return self.jobs.get(str(job_id))

    def start(self, session_id, workflow, profile_path, inputs, runs_dir):
        """
        starts a run in the background
        :param session_id: identifier
        :param workflow: clean, compare, or reconcile
        :param profile_path: TOML profile on disk
        :param inputs: input paths, see command()
        :param runs_dir: parent output folder
        :returns: job
        """
        argv = self.command(workflow, profile_path, inputs, runs_dir)
        job = Job(session_id, workflow, argv, runs_dir)
        with self.lock:
            self.jobs[job.id] = job
        thread = threading.Thread(target=self.execute, args=(job,), daemon=True)
        thread.start()
        return job

    def execute(self, job):
        """
        runs the child process and records how it ended
        :param job: job
        :returns: none
        """
        before = self.published(job.runs_dir)
        env = {key: value for key, value in os.environ.items() if key != "ORDIXA_DEBUG"}
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            with job.lock:
                if job.cancel_requested:
                    self.finish(job, CANCELLED)
                    return
                job.process = subprocess.Popen(
                    job.argv,
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                job.state = RUNNING
            timer = threading.Timer(self.timeout, self.stop, args=(job, "timeout"))
            timer.daemon = True
            timer.start()
            try:
                for line in job.process.stdout:
                    if len(job.output) < MAX_OUTPUT_LINES:
                        job.output.append(clean_line(line))
                code = job.process.wait()
            finally:
                timer.cancel()
        except OSError as error:
            job.output.append(f"could not start Ordixa: {error.strerror}")
            job.exit_code = 1
            self.finish(job, FAILED)
            return

        job.exit_code = code
        created = sorted(self.published(job.runs_dir) - before)
        if code == 0 and created:
            job.run_name = created[-1]
            if job.cancel_requested:
                job.note = "The run finished before it could be stopped; its results are complete."
            self.finish(job, COMPLETED)
            return
        self.remove_staging(job.runs_dir)
        if job.cancel_requested:
            self.finish(job, CANCELLED)
            return
        if code == 0:
            job.exit_code = 1
        self.finish(job, FAILED)

    def finish(self, job, state):
        """
        records the final state
        :param job: job
        :param state: completed, failed, or cancelled
        :returns: none
        """
        job.state = state
        job.finished_at = now()

    def stop(self, job, reason="cancel"):
        """
        stops a run; nothing partial is ever published
        :param job: job
        :param reason: cancel or timeout
        :returns: none
        """
        with job.lock:
            if job.state in FINISHED:
                return
            job.cancel_requested = True
            if reason == "timeout":
                job.note = "The run took too long and was stopped."
            if job.process is not None and job.process.poll() is None:
                job.state = CANCELLING
                job.process.terminate()
                try:
                    job.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    job.process.kill()

    def cancel(self, job_id):
        """
        asks a run to stop
        :param job_id: identifier
        :returns: job or none
        """
        job = self.get(job_id)
        if job is not None:
            self.stop(job)
        return job

    def published(self, runs_dir):
        """
        lists published run directories
        :param runs_dir: parent output folder
        :returns: set of names
        """
        runs_dir = Path(runs_dir)
        if not runs_dir.is_dir():
            return set()
        return {
            path.name
            for path in runs_dir.iterdir()
            if RUN_NAME.match(path.name) and (path / "manifest.json").is_file()
        }

    def remove_staging(self, runs_dir):
        """
        removes staging folders a stopped or failed run left behind
        :param runs_dir: parent output folder
        :returns: none
        """
        for path in Path(runs_dir).glob(".run-*.partial"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def stop_all(self):
        """
        stops every unfinished run, used when the workspace shuts down
        :returns: none
        """
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            self.stop(job)
