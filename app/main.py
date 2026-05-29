from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
from typing import Any
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from intakeq_exporter import (
    BASE_URL,
    DEFAULT_DELAY_SECONDS,
    DEFAULT_END_DATE,
    DEFAULT_START_DATE,
    IntakeQAPIError,
    build_client_params,
    client_export_scope,
    perform_export,
    validate_export_args,
)


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
security = HTTPBasic(auto_error=False)
app = FastAPI(title="IntakeQ Exporter", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


class ExportJob:
    def __init__(self, job_id: str, output_dir: Path) -> None:
        self.id = job_id
        self.status = "queued"
        self.created_at = dt.datetime.now(dt.timezone.utc)
        self.updated_at = self.created_at
        self.output_dir = output_dir
        self.archive_path: Path | None = None
        self.metadata: dict[str, Any] | None = None
        self.error: str | None = None
        self.messages: list[str] = []
        self.api_key: str | None = None
        self.args: argparse.Namespace | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "messages": self.messages[-80:],
            "error": self.error,
            "metadata": self.metadata,
            "downloadReady": self.archive_path is not None and self.archive_path.exists(),
            "canResume": self.status == "failed" and self.api_key is not None,
        }


jobs: dict[str, ExportJob] = {}
jobs_lock = threading.Lock()


def export_root() -> Path:
    return Path(os.environ.get("EXPORT_ROOT", PROJECT_DIR / "exports")).resolve()


_JOB_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.match(job_id or ""))


def serialize_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        key: (str(value) if isinstance(value, Path) else value)
        for key, value in vars(args).items()
    }


def job_record_path(job_id: str) -> Path:
    return export_root() / job_id / "job.json"


def persist_job_record(job: "ExportJob") -> None:
    """Best-effort write of resume state to disk (never the API key)."""
    record = {
        "id": job.id,
        "status": job.status,
        "createdAt": job.created_at.isoformat(),
        "updatedAt": job.updated_at.isoformat(),
        "args": serialize_args(job.args) if job.args is not None else None,
    }
    try:
        path = job_record_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_job_record(job_id: str) -> dict[str, Any] | None:
    if not valid_job_id(job_id):
        return None
    path = job_record_path(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_recoverable_jobs() -> list[dict[str, Any]]:
    root = export_root()
    results: list[dict[str, Any]] = []
    if not root.exists():
        return results
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "data").is_dir():
            continue
        record = load_job_record(child.name) or {}
        metadata: dict[str, Any] | None = None
        meta_path = child / "data" / "export_metadata.json"
        if meta_path.exists():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = None
        results.append(
            {
                "id": child.name,
                "status": record.get("status", "unknown"),
                "createdAt": record.get("createdAt"),
                "complete": metadata is not None,
                "counts": (metadata or {}).get("counts"),
            }
        )
    return results


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> None:
    expected_password = os.environ.get("EXPORTER_ADMIN_PASSWORD")
    if not expected_password:
        return

    expected_username = os.environ.get("EXPORTER_ADMIN_USERNAME", "admin")
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, expected_username)
        and secrets.compare_digest(credentials.password, expected_password)
    )
    if valid:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Basic"},
    )


def get_job_or_404(job_id: str) -> ExportJob:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Export job not found")
    return job


def record_job_message(job_id: str, message: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.messages.append(message)
        job.updated_at = dt.datetime.now(dt.timezone.utc)


def set_job_status(job_id: str, status_value: str, *, error: str | None = None) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = status_value
        job.error = error
        job.updated_at = dt.datetime.now(dt.timezone.utc)
    persist_job_record(job)


def complete_job(job_id: str, archive_path: Path, metadata: dict[str, Any]) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "completed"
        job.archive_path = archive_path
        job.metadata = metadata
        job.updated_at = dt.datetime.now(dt.timezone.utc)
    persist_job_record(job)


def optional_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def optional_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


def export_args_from_form(form: Any, output_dir: Path) -> argparse.Namespace:
    client_scope = str(form.get("client_scope") or "all")
    args = argparse.Namespace(
        output_dir=output_dir,
        start_date=str(form.get("start_date") or DEFAULT_START_DATE),
        end_date=str(form.get("end_date") or DEFAULT_END_DATE),
        submitted_only=form.get("submitted_only") == "on",
        client_search=None,
        client_created_start=None,
        client_created_end=None,
        client_updated_start=None,
        client_updated_end=None,
        deleted_clients_only=False,
        download_pdfs=form.get("download_pdfs") == "on",
        fhir=form.get("fhir") == "on",
        max_pages=optional_int(form.get("max_pages")),
        max_intakes=optional_int(form.get("max_intakes")),
        delay_seconds=optional_float(form.get("delay_seconds"), DEFAULT_DELAY_SECONDS),
        base_url=str(form.get("base_url") or BASE_URL),
    )

    if client_scope == "search":
        args.client_search = str(form.get("client_search") or "").strip() or None
    elif client_scope == "created":
        args.client_created_start = str(form.get("client_created_start") or "").strip() or None
        args.client_created_end = str(form.get("client_created_end") or "").strip() or None
    elif client_scope == "updated":
        args.client_updated_start = str(form.get("client_updated_start") or "").strip() or None
        args.client_updated_end = str(form.get("client_updated_end") or "").strip() or None
    elif client_scope == "deleted":
        args.deleted_clients_only = True

    validate_export_args(args)
    return args


def create_archive(job_id: str, output_dir: Path) -> Path:
    archive_path = output_dir.parent / f"intakeq_export_{job_id}.zip"
    if archive_path.exists():
        archive_path.unlink()
    shutil.make_archive(str(archive_path.with_suffix("")), "zip", root_dir=output_dir)
    return archive_path


def run_export_job(job_id: str, api_key: str, args: argparse.Namespace) -> None:
    set_job_status(job_id, "running")
    try:
        result = perform_export(
            api_key,
            args,
            log=lambda message: record_job_message(job_id, message),
        )
        record_job_message(job_id, "Creating ZIP archive...")
        archive_path = create_archive(job_id, result.output_dir)
        complete_job(job_id, archive_path, result.metadata)
        record_job_message(job_id, "Export complete.")
    except (IntakeQAPIError, ValueError, OSError, RuntimeError) as exc:
        record_job_message(job_id, f"Export failed: {exc}")
        set_job_status(job_id, "failed", error=str(exc))
    except Exception as exc:
        record_job_message(job_id, "Export failed unexpectedly.")
        set_job_status(job_id, "failed", error=f"{type(exc).__name__}: {exc}")


def start_export_thread(job_id: str, api_key: str, args: argparse.Namespace) -> None:
    thread = threading.Thread(
        target=run_export_job,
        args=(job_id, api_key, args),
        daemon=True,
    )
    thread.start()


def template_context(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "defaults": {
            "start_date": DEFAULT_START_DATE,
            "end_date": DEFAULT_END_DATE,
            "delay_seconds": DEFAULT_DELAY_SECONDS,
            "base_url": BASE_URL,
        },
        "admin_password_set": bool(os.environ.get("EXPORTER_ADMIN_PASSWORD")),
        "resume_job_id": "",
        **extra,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    resume: str = "",
    _: None = Depends(require_admin),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", template_context(request, resume_job_id=resume)
    )


@app.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request, _: None = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "recover.html",
        template_context(request, recoverable_jobs=list_recoverable_jobs()),
    )


@app.post("/exports")
async def start_export(
    request: Request,
    _: None = Depends(require_admin),
):
    form = await request.form()
    api_key = str(form.get("api_key") or "").strip()
    if not api_key:
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, error="Enter an IntakeQ API key to start the export."),
            status_code=422,
        )

    resume_id = str(form.get("resume_job_id") or "").strip()
    if resume_id:
        if not valid_job_id(resume_id):
            return templates.TemplateResponse(
                request,
                "index.html",
                template_context(
                    request,
                    error="That export ID isn't valid.",
                    resume_job_id=resume_id,
                ),
                status_code=422,
            )
        job_id = resume_id
    else:
        job_id = uuid.uuid4().hex[:12]

    job_dir = export_root() / job_id
    output_dir = job_dir / "data"

    if resume_id and not output_dir.is_dir():
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(
                request,
                error=f"No existing export found with ID {resume_id!r} to resume.",
                resume_job_id=resume_id,
            ),
            status_code=422,
        )

    try:
        args = export_args_from_form(form, output_dir)
        client_scope = client_export_scope(build_client_params(args))
    except (ValueError, SystemExit) as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, error=str(exc), resume_job_id=resume_id),
            status_code=422,
        )

    job_dir.mkdir(parents=True, exist_ok=True)
    job = ExportJob(job_id, output_dir)
    verb = "Resuming" if resume_id else "Queued"
    job.messages.append(f"{verb} export for {client_scope}.")
    job.api_key = api_key
    job.args = args
    with jobs_lock:
        jobs[job_id] = job
    persist_job_record(job)

    start_export_thread(job_id, api_key, args)

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/jobs/{job_id}/resume")
def resume_export(job_id: str, _: None = Depends(require_admin)) -> RedirectResponse:
    job = get_job_or_404(job_id)
    if job.status not in {"failed"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resume job in status {job.status!r}",
        )
    if job.api_key is None or job.args is None:
        raise HTTPException(
            status_code=409,
            detail="Resume context is unavailable for this job",
        )

    with jobs_lock:
        job.status = "queued"
        job.error = None
        job.updated_at = dt.datetime.now(dt.timezone.utc)
        job.messages.append("Resuming export from last completed phase.")

    start_export_thread(job.id, job.api_key, job.args)
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status_page(
    job_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> HTMLResponse:
    job = get_job_or_404(job_id)
    return templates.TemplateResponse(request, "job.html", template_context(request, job=job))


@app.get("/api/jobs/{job_id}")
def job_status_api(job_id: str, _: None = Depends(require_admin)) -> JSONResponse:
    job = get_job_or_404(job_id)
    return JSONResponse(job.to_dict())


@app.get("/exports/{job_id}/download")
def download_export(job_id: str, _: None = Depends(require_admin)) -> FileResponse:
    job = get_job_or_404(job_id)
    if job.archive_path is None or not job.archive_path.exists():
        raise HTTPException(status_code=404, detail="Export archive is not ready")
    return FileResponse(
        job.archive_path,
        media_type="application/zip",
        filename=f"intakeq_export_{job_id}.zip",
    )
