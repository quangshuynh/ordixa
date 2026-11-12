"""
on-disk storage for workspace sessions

each session is one piece of work (clean one file, compare two versions, or
reconcile orders and payments) kept in its own folder:

    <data dir>/sessions/<id>/
        session.json        what was chosen: workflow, files, draft settings
        inputs/             the files the person added, never modified
        profiles/<sha>.toml every profile a run used, named by its checksum
        runs/               run-<id>/ directories written by the Ordixa CLI

the data directory is git-ignored by default (customer-data/workspace), and
nothing in it is sent anywhere
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app.ordixa.adapters import MAX_INPUT_BYTES

WORKFLOWS = ("clean", "compare", "reconcile")
ROLES = {
    "clean": ("input",),
    "compare": ("before", "after"),
    "reconcile": ("orders", "payments", "customers"),
}
REQUIRED_ROLES = {
    "clean": ("input",),
    "compare": ("before", "after"),
    "reconcile": ("orders", "payments"),
}
# reconciliation batches often hold one export per month
MAX_FILES = {"orders": 12, "payments": 12, "customers": 4}
SUFFIXES = (".csv", ".xlsx")
MAX_UPLOAD_BYTES = MAX_INPUT_BYTES
MAX_SESSIONS = 200
SESSION_ID = re.compile(r"^[0-9a-f]{12}$")
FILE_ID = re.compile(r"^[0-9a-f]{8}$")
RUN_NAME = re.compile(r"^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._ ()+-]")
MAX_TITLE = 80


class WorkspaceError(Exception):
    """a request the workspace refuses, with a message fit to show a person"""

    def __init__(self, message, status=400, code="refused"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def now_text():
    """
    formats the current UTC time for session records
    :returns: ISO 8601 text
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_file_name(name):
    """
    turns a browser-supplied file name into a plain local file name

    folders are dropped, and anything other than letters, digits, spaces, and
    . _ ( ) + - becomes an underscore, so names can never escape the session
    folder or act as file patterns in a profile
    :param name: file name as chosen by the person
    :returns: safe file name
    """
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = UNSAFE_NAME_CHARACTERS.sub("_", base).strip(" .")
    base = re.sub(r"_+", "_", base)
    if not base.lower().endswith(SUFFIXES):
        raise WorkspaceError(
            "Only CSV (.csv) and Excel (.xlsx) files can be used. Older .xls "
            "workbooks can be re-saved as .xlsx from Excel.",
            415,
            "unsupported_file",
        )
    stem, suffix = base[: base.rfind(".")], base[base.rfind(".") :].lower()
    stem = stem.strip(" .") or "file"
    return stem[:100] + suffix


def write_json(path, data):
    """
    writes JSON atomically so a crash never leaves half a session record
    :param path: destination
    :param data: serializable value
    :returns: none
    """
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    os.replace(temporary, path)


class Store:
    """sessions kept in one local data directory"""

    def __init__(self, root):
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    # sessions

    def session_dir(self, session_id):
        """
        locates a session folder, refusing anything that is not a session id
        :param session_id: identifier from a URL
        :returns: path
        """
        if not SESSION_ID.match(str(session_id)):
            raise WorkspaceError("That piece of work was not found.", 404, "not_found")
        path = self.sessions_dir / session_id
        if not (path / "session.json").is_file():
            raise WorkspaceError("That piece of work was not found.", 404, "not_found")
        return path

    def create(self, workflow, title=None):
        """
        starts a new session
        :param workflow: clean, compare, or reconcile
        :param title: optional name shown in the list of recent work
        :returns: session record
        """
        if workflow not in WORKFLOWS:
            raise WorkspaceError("Choose clean, compare, or reconcile.", 400)
        with self.lock:
            if len(self.list()) >= MAX_SESSIONS:
                raise WorkspaceError(
                    f"There are already {MAX_SESSIONS} pieces of work saved. Remove "
                    "some you no longer need, then try again.",
                    409,
                    "too_many_sessions",
                )
            session_id = secrets.token_hex(6)
            path = self.sessions_dir / session_id
            path.mkdir()
            for name in ("inputs", "profiles", "runs"):
                (path / name).mkdir()
            session = {
                "id": session_id,
                "workflow": workflow,
                "title": (title or "").strip()[:MAX_TITLE],
                "created_at": now_text(),
                "updated_at": now_text(),
                "files": {role: [] for role in ROLES[workflow]},
                "settings": None,
                "sample": None,
            }
            write_json(path / "session.json", session)
            return session

    def get(self, session_id):
        """
        reads a session record
        :param session_id: identifier
        :returns: session record
        """
        path = self.session_dir(session_id)
        return json.loads((path / "session.json").read_text("utf-8"))

    def save(self, session):
        """
        stores a changed session record
        :param session: session record
        :returns: the record
        """
        with self.lock:
            path = self.session_dir(session["id"])
            session["updated_at"] = now_text()
            write_json(path / "session.json", session)
            return session

    def list(self):
        """
        lists sessions, most recently changed first
        :returns: list of session records
        """
        sessions = []
        for path in self.sessions_dir.iterdir():
            record = path / "session.json"
            if SESSION_ID.match(path.name) and record.is_file():
                try:
                    sessions.append(json.loads(record.read_text("utf-8")))
                except ValueError, OSError:
                    continue
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)

    def delete(self, session_id):
        """
        permanently removes a session folder: its files, settings, and runs
        :param session_id: identifier
        :returns: none
        """
        with self.lock:
            shutil.rmtree(self.session_dir(session_id))

    # files

    def input_dir(self, session, role):
        """
        names the folder holding one role's files

        reconciliation reads every dataset from one folder, as `run --input`
        does; clean and compare keep each role apart so a before and after
        export may share a file name
        :param session: session record
        :param role: input role
        :returns: path
        """
        base = self.session_dir(session["id"]) / "inputs"
        return base if session["workflow"] == "reconcile" else base / role

    def input_paths(self, session, role):
        """
        lists the stored files of one role
        :param session: session record
        :param role: input role
        :returns: list of paths
        """
        folder = self.input_dir(session, role)
        return [folder / item["name"] for item in session["files"].get(role, [])]

    def file_entry(self, session, role, file_id):
        """
        finds one stored file
        :param session: session record
        :param role: input role
        :param file_id: identifier
        :returns: tuple of entry and path
        """
        if role not in ROLES[session["workflow"]] or not FILE_ID.match(str(file_id)):
            raise WorkspaceError("That file was not found.", 404, "not_found")
        for item in session["files"][role]:
            if item["id"] == file_id:
                return item, self.input_dir(session, role) / item["name"]
        raise WorkspaceError("That file was not found.", 404, "not_found")

    def unique_name(self, session, role, name):
        """
        avoids two stored files sharing one name in the same folder
        :param session: session record
        :param role: input role
        :param name: safe file name
        :returns: unused file name
        """
        folder = self.input_dir(session, role)
        stem, suffix = name[: name.rfind(".")], name[name.rfind(".") :]
        candidate, number = name, 2
        while (folder / candidate).exists():
            candidate = f"{stem}-{number}{suffix}"
            number += 1
        return candidate

    async def add_file(self, session_id, role, original_name, chunks, declared=None):
        """
        stores a file the person chose, streaming it to disk with a size limit

        the file is written to a hidden temporary name and only renamed into
        place once it is complete, so an interrupted upload leaves nothing
        that could be mistaken for an input
        :param session_id: identifier
        :param role: input role
        :param original_name: file name from the browser
        :param chunks: async iterator of bytes
        :param declared: Content-Length, when the browser sent one
        :returns: file entry
        """
        session = self.get(session_id)
        if role not in ROLES[session["workflow"]]:
            raise WorkspaceError("That kind of file is not used here.", 400)
        name = safe_file_name(original_name)
        limit_text = f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        if declared is not None and declared > MAX_UPLOAD_BYTES:
            raise WorkspaceError(
                f"This file is larger than {limit_text}, the most Ordixa reads at "
                "once. Export a shorter date range or split the file.",
                413,
                "too_large",
            )
        multiple = role in MAX_FILES
        if multiple and len(session["files"][role]) >= MAX_FILES[role]:
            raise WorkspaceError(
                f"Up to {MAX_FILES[role]} {role} files can be added at once.",
                409,
                "too_many_files",
            )

        folder = self.input_dir(session, role)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".upload-{secrets.token_hex(6)}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise WorkspaceError(
                            f"This file is larger than {limit_text}, the most "
                            "Ordixa reads at once. Export a shorter date range or "
                            "split the file.",
                            413,
                            "too_large",
                        )
                    digest.update(chunk)
                    handle.write(chunk)
            if size == 0:
                raise WorkspaceError("This file is empty.", 400, "empty_file")
            with self.lock:
                session = self.get(session_id)
                replaced = [] if multiple else list(session["files"][role])
                for item in replaced:
                    (folder / item["name"]).unlink(missing_ok=True)
                session["files"][role] = [] if not multiple else session["files"][role]
                final = self.unique_name(session, role, name)
                os.replace(temporary, folder / final)
                entry = {
                    "id": secrets.token_hex(4),
                    "name": final,
                    "original_name": str(original_name)[:200],
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                    "added_at": now_text(),
                }
                session["files"][role].append(entry)
                self.save(session)
                return entry
        finally:
            temporary.unlink(missing_ok=True)

    def copy_file(self, session_id, role, source):
        """
        copies a local file, such as a fictional sample, into a session
        :param session_id: identifier
        :param role: input role
        :param source: path of the file to copy
        :returns: file entry
        """
        with self.lock:
            session = self.get(session_id)
            folder = self.input_dir(session, role)
            folder.mkdir(parents=True, exist_ok=True)
            final = self.unique_name(session, role, safe_file_name(source.name))
            shutil.copyfile(source, folder / final)
            data = (folder / final).read_bytes()
            entry = {
                "id": secrets.token_hex(4),
                "name": final,
                "original_name": source.name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "added_at": now_text(),
            }
            session["files"][role].append(entry)
            self.save(session)
            return entry

    def remove_file(self, session_id, role, file_id):
        """
        removes one stored file from a session
        :param session_id: identifier
        :param role: input role
        :param file_id: identifier
        :returns: none
        """
        with self.lock:
            session = self.get(session_id)
            entry, path = self.file_entry(session, role, file_id)
            path.unlink(missing_ok=True)
            session["files"][role] = [
                item for item in session["files"][role] if item["id"] != file_id
            ]
            self.save(session)

    # runs

    def runs_dir(self, session):
        """
        names the parent folder the CLI writes run-<id> directories into
        :param session: session record
        :returns: path
        """
        return self.session_dir(session["id"]) / "runs"

    def run_dir(self, session, run_name):
        """
        locates one published run directory
        :param session: session record
        :param run_name: run-<id> name from a URL
        :returns: path
        """
        path = self.runs_dir(session) / str(run_name)
        if not RUN_NAME.match(str(run_name)) or not (path / "manifest.json").is_file():
            raise WorkspaceError("That result was not found.", 404, "not_found")
        return path

    def run_names(self, session):
        """
        lists published runs, newest first
        :param session: session record
        :returns: list of run-<id> names
        """
        folder = self.runs_dir(session)
        return sorted(
            (
                path.name
                for path in folder.iterdir()
                if RUN_NAME.match(path.name) and (path / "manifest.json").is_file()
            ),
            reverse=True,
        )

    def incomplete_runs(self, session):
        """
        lists staging folders a stopped or interrupted run left behind

        the CLI publishes run-<id> only once every output is complete, so these
        folders are never results
        :param session: session record
        :returns: list of folder names
        """
        folder = self.runs_dir(session)
        return sorted(
            path.name for path in folder.glob(".run-*.partial") if path.is_dir()
        )

    def clear_incomplete(self, session):
        """
        removes staging folders left by stopped or interrupted runs
        :param session: session record
        :returns: number removed
        """
        names = self.incomplete_runs(session)
        for name in names:
            shutil.rmtree(self.runs_dir(session) / name, ignore_errors=True)
        return len(names)

    def save_profile(self, session, text):
        """
        keeps the exact profile text a run used, named by its SHA-256
        :param session: session record
        :param text: TOML profile text
        :returns: path of the stored profile
        """
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.session_dir(session["id"]) / "profiles" / f"{digest}.toml"
        if not path.exists():
            temporary = path.with_name(f".{digest}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
        return path

    def profile_for(self, session, sha256):
        """
        finds the stored profile a run's manifest names
        :param session: session record
        :param sha256: profile checksum from the manifest
        :returns: path, or none when it is not stored here
        """
        if not re.fullmatch(r"[0-9a-f]{64}", str(sha256 or "")):
            return None
        path = self.session_dir(session["id"]) / "profiles" / f"{sha256}.toml"
        return path if path.is_file() else None
