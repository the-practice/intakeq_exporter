from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
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


def complete_job(job_id: str, archive_path: Path, metadata: dict[str, Any]) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "completed"
        job.archive_path = archive_path
        job.metadata = metadata
        job.updated_at = dt.datetime.now(dt.timezone.utc)


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
        **extra,
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: None = Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", template_context(request))


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

    job_id = uuid.uuid4().hex[:12]
    job_dir = export_root() / job_id
    output_dir = job_dir / "data"

    try:
        args = export_args_from_form(form, output_dir)
        client_scope = client_export_scope(build_client_params(args))
    except (ValueError, SystemExit) as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, error=str(exc)),
            status_code=422,
        )

    job_dir.mkdir(parents=True, exist_ok=True)
    job = ExportJob(job_id, output_dir)
    job.messages.append(f"Queued export for {client_scope}.")
    job.api_key = api_key
    job.args = args
    with jobs_lock:
        jobs[job_id] = job

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
