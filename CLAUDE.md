# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Run web app locally (FastAPI + Jinja templates on http://127.0.0.1:8000)
uvicorn app.main:app --reload

# Run CLI exporter (stdlib only — venv not required)
python3 intakeq_exporter.py
INTAKEQ_API_KEY=... python3 intakeq_exporter.py --max-pages 1 --max-intakes 5

# Also emit a FHIR R4 bulk export (NDJSON) — fetches notes + per-client diagnoses
INTAKEQ_API_KEY=... python3 intakeq_exporter.py --fhir

# Tests
python3 -m unittest                                  # all tests
python3 -m unittest test_intakeq_exporter            # CLI/core tests only
python3 -m unittest test_web_app.WebAppTests.test_healthz   # single test

# Syntax check (used as a lightweight verification step)
python3 -m py_compile intakeq_exporter.py app/main.py
```

## Architecture

There are **two entry points sharing one export engine**:

1. **`intakeq_exporter.py`** — single-file CLI built on the Python standard library only (`urllib`, `csv`, `json`, `getpass`). Stdlib-only is intentional so the CLI is portable and can be copied/run without `pip install`. Do not introduce third-party dependencies into this file.
2. **`app/main.py`** — FastAPI web wrapper that imports `perform_export`, `validate_export_args`, `build_client_params`, `client_export_scope`, and the defaults (`BASE_URL`, `DEFAULT_START_DATE`, `DEFAULT_END_DATE`, `DEFAULT_DELAY_SECONDS`) from the CLI module. The web app constructs an `argparse.Namespace` from form fields and hands it to the same `perform_export` used by the CLI — keep that contract intact when changing CLI arg shape.

### IntakeQ API client (`IntakeQClient`)

- Auth header is `X-Auth-Key` (not `Authorization`).
- Default `delay_seconds = 6.2` because IntakeQ's standard API limit is 10 requests/minute. The throttle is per-instance and tracked via `_last_request_at`.
- Retries on 429/500/502/503/504 and network errors, honoring `Retry-After` when present. **429s without `Retry-After` use a dedicated longer schedule** (`RATE_LIMIT_BASE_WAIT_SECONDS = 60`, exponential up to `RATE_LIMIT_MAX_WAIT_SECONDS = 300`) because IntakeQ's per-minute bucket needs at least ~60s to reset. Other 5xx and network errors keep the shorter exponential-up-to-60s schedule.
- `fetch_paged` walks pages of size `PAGE_SIZE = 100` until a short or empty page; `max_pages` is a test-only cap.

### Per-patient grouping

`patient_reference` produces a stable identity used to bucket records under `by_patient/<id>/`. Resolution order: `ClientId` → `ClientNumber` → `ExternalClientId` → SHA-256(email) → SHA-256(name) → SHA-256(record). The hashed fallbacks exist so the directory layout never leaks PII; keep that property when extending.

### Web app job model

- Jobs are tracked in an in-memory `jobs: dict[str, ExportJob]` guarded by `jobs_lock` and executed in daemon `threading.Thread`s. The in-memory dict (status, messages, **the api_key**) is still lost on container restart — the api_key is deliberately never persisted.
- Each job's resume state **is** persisted to disk: `persist_job_record` writes `<EXPORT_ROOT>/<job_id>/job.json` (id, status, timestamps, serialized `args` — never the api_key) at creation and on every terminal status change. This is what lets an export survive a restart.
- Two resume paths: (1) the same-session **Resume** button (`POST /jobs/{id}/resume`) reuses the in-memory `api_key` + `args`; (2) the restart-safe path — `GET /recover` lists on-disk exports (`list_recoverable_jobs` scans `EXPORT_ROOT` for `<id>/data/`), each linking to `/?resume=<id>` which prefills the main export form. Submitting `/exports` with a `resume_job_id` reuses that job_id/`output_dir` instead of minting a new one, so the user re-enters only the api_key and the engine resumes from the on-disk checkpoint files. `valid_job_id` guards the id against path traversal.
- Output is written under `EXPORT_ROOT` (defaults to `<repo>/exports`, set to `/data/exports` on Railway with a mounted volume). Each job writes to `<EXPORT_ROOT>/<job_id>/data/` and the ZIP is created next to it as `intakeq_export_<job_id>.zip`.
- Basic auth (`require_admin`) is only enforced when `EXPORTER_ADMIN_PASSWORD` is set — locally the form is unauthenticated by default. `EXPORTER_ADMIN_USERNAME` defaults to `admin`.

### Output layout and incremental writes

`perform_export` writes each phase to disk as it completes — not at the end — so a crash mid-run leaves a resumable partial export:

- `clients.json` / `clients.csv` — written after the clients phase.
- `appointments.json` / `appointments.csv` — written after the appointments phase. Each individual page is also written to `_appointments_pages/page_NNNNN.json` as it's fetched, so a mid-phase failure (e.g. a 429 on page 159) preserves pages 1-158 for the next resume.
- `intakes_summary.json` / `intakes_summary.csv` — same per-page checkpointing under `_intakes_summary_pages/`.
- `intakes_full/<intake_id>.json` — one file per full intake, written as each is fetched. The aggregated `intakes_full.json` is collated at the end.
- `export_metadata.json` — final write; includes `skippedFullIntakes` listing intake IDs whose individual fetch failed but did not abort the run.
- Optional `pdfs/intakes/` + `pdfs/consents/` when `--download-pdfs` is set.

### FHIR R4 export (`--fhir`)

Opt-in via `--fhir` (CLI) or the web form's FHIR checkbox. When enabled, `perform_export` runs two extra resumable fetches after the standard phases — treatment notes (`notes.json` + `_notes_pages/`, via `load_or_fetch_paged_list`) and per-client diagnoses (`fetch_diagnoses_resumable`, cached one file per client under `_diagnoses/<patient-dir>.json`) — then transforms everything to FHIR and writes one NDJSON file per resource type under `fhir/`:

- `Patient.ndjson` ← clients, `Coverage.ndjson` ← client insurance fields
- `Appointment.ndjson` ← appointments, `QuestionnaireResponse.ndjson` ← full intakes
- `DocumentReference.ndjson` ← treatment notes, `Condition.ndjson` ← diagnoses (ICD-10-CM)

The transform is a pure function (`build_fhir_resources` + the `*_to_*` mappers like `client_to_patient`, `appointment_to_fhir`); it derives stable FHIR resource ids and `Patient/<id>` references from the same `patient_reference`/`patient_dir_name` identity used by `by_patient/`, so FHIR references line up with the per-patient layout. Diagnoses (`/client/{id}/diagnoses`) are one API call per client, so `--fhir` can be slow on large accounts — the per-client cache makes it resumable. Enum/timestamp normalization lives in the mappers (`fhir_gender`, `fhir_marital_status`, `fhir_appointment_status`, `ms_to_date`/`ms_to_datetime`); raw IntakeQ strings/Unix-ms values do not validate as FHIR and must go through these. `export_metadata.json` gains `fhirEnabled` + `fhirCounts`.

`load_or_fetch_paged_list` provides per-page checkpointing for paginated phases, and `fetch_full_intakes_resumable` provides per-intake checkpointing. If a phase's aggregate JSON exists and parses, it's loaded as-is and the underlying pages are not consulted; otherwise the helper walks pages 1..N, loading already-cached page files and only calling the API for missing ones. Per-intake API failures are logged + skipped (recorded in `skippedFullIntakes`) rather than aborting the whole run; whole-phase failures still propagate. To force a re-fetch, delete the relevant file (or the `_<phase>_pages/` directory) before resuming.

## Conventions

- The CLI's `--client-*` flags map to the IntakeQ `/clients` query params (`search`, `dateCreatedStart/End`, `dateUpdatedStart/End`, `deletedOnly`) via `build_client_params`. The web form's `client_scope` radio mirrors these mutually exclusive modes.
- `fetch_clients` includes a fallback that retries with the capitalized `IncludeProfile` flag if the lowercased `includeProfile` returns records without `ClientId` — IntakeQ has historically been inconsistent here.
- Never commit API keys or anything under `exports/` or `intakeq_export_*`.
