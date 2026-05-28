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
- Retries on 429/500/502/503/504 and network errors, honoring `Retry-After` and otherwise using exponential backoff capped at 60s.
- `fetch_paged` walks pages of size `PAGE_SIZE = 100` until a short or empty page; `max_pages` is a test-only cap.

### Per-patient grouping

`patient_reference` produces a stable identity used to bucket records under `by_patient/<id>/`. Resolution order: `ClientId` → `ClientNumber` → `ExternalClientId` → SHA-256(email) → SHA-256(name) → SHA-256(record). The hashed fallbacks exist so the directory layout never leaks PII; keep that property when extending.

### Web app job model

- Jobs are tracked in an in-memory `jobs: dict[str, ExportJob]` guarded by `jobs_lock` and executed in daemon `threading.Thread`s. **No persistence** — jobs and progress are lost on restart. If you add long-running features, account for this.
- Output is written under `EXPORT_ROOT` (defaults to `<repo>/exports`, set to `/data/exports` on Railway with a mounted volume). Each job writes to `<EXPORT_ROOT>/<job_id>/data/` and the ZIP is created next to it as `intakeq_export_<job_id>.zip`.
- Basic auth (`require_admin`) is only enforced when `EXPORTER_ADMIN_PASSWORD` is set — locally the form is unauthenticated by default. `EXPORTER_ADMIN_USERNAME` defaults to `admin`.

### Output layout (written by `perform_export`)

Top-level JSON+CSV pairs (`clients`, `appointments`, `intakes_summary`), plus `intakes_full.json`, `export_metadata.json`, a `by_patient/` tree, and optional `pdfs/intakes/` + `pdfs/consents/` when `--download-pdfs` is set.

## Conventions

- The CLI's `--client-*` flags map to the IntakeQ `/clients` query params (`search`, `dateCreatedStart/End`, `dateUpdatedStart/End`, `deletedOnly`) via `build_client_params`. The web form's `client_scope` radio mirrors these mutually exclusive modes.
- `fetch_clients` includes a fallback that retries with the capitalized `IncludeProfile` flag if the lowercased `includeProfile` returns records without `ClientId` — IntakeQ has historically been inconsistent here.
- Never commit API keys or anything under `exports/` or `intakeq_export_*`.
