# IntakeQ Exporter

Local and Railway-ready exporter for IntakeQ patient data. It uses IntakeQ's
official REST API instead of browser scraping, prompts for the API key securely,
and writes exported data to local files or a downloadable ZIP from the web app.

## What It Exports

- Clients with profile/demographic fields
- Appointments across a configurable past/future date range
- Intake/questionnaire summaries
- Full intake/questionnaire JSON, including questions, answers, and consent form
  metadata
- Optional intake package PDFs and individual consent PDFs

## Web App

Run the admin form locally:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

The web form starts exports in the background, shows live progress, and provides
a ZIP download when the job completes.

## Railway Deployment

This repo includes `railway.json`, so Railway can start the FastAPI app with:

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

In Railway, set these variables:

```text
EXPORTER_ADMIN_PASSWORD=<strong password>
EXPORTER_ADMIN_USERNAME=admin
EXPORT_ROOT=/data/exports
```

`EXPORTER_ADMIN_PASSWORD` protects the public form with browser basic auth.
`EXPORT_ROOT=/data/exports` is recommended when you attach a Railway volume at
`/data`; without a volume, generated exports may disappear when the container is
replaced.

After deploying, generate a Railway public domain from the service's Networking
settings.

## Before You Run It

In IntakeQ, enable API access and get the API key from:

`More > Settings > Integrations > Developer API`

Only the main account owner has access to the API tab. Do not commit the API key
or patient exports to git.

## Usage

The command-line exporter uses only Python's standard library.

```bash
python3 intakeq_exporter.py
```

You can also provide the key through an environment variable:

```bash
export INTAKEQ_API_KEY="your-api-key"
python3 intakeq_exporter.py
```

Useful options:

```bash
python3 intakeq_exporter.py \
  --output-dir exports/intakeq_export \
  --start-date 1900-01-01 \
  --end-date 2100-12-31 \
  --download-pdfs
```

The default client mode exports all clients by walking every page from the
`/clients` API. To narrow the client export, use one of these options:

```bash
# All clients. This is the default.
python3 intakeq_exporter.py

# One patient or a partial name/email match.
python3 intakeq_exporter.py --client-search "Jane Doe"
python3 intakeq_exporter.py --client-search "jane@example.com"
python3 intakeq_exporter.py --client-search "12345"

# Clients created or updated in a date range.
python3 intakeq_exporter.py --client-created-start 2025-01-01 --client-created-end 2025-12-31
python3 intakeq_exporter.py --client-updated-start 2026-01-01

# Recently deleted clients only, where IntakeQ still exposes them.
python3 intakeq_exporter.py --deleted-clients-only
```

For a small test run:

```bash
python3 intakeq_exporter.py --max-pages 1 --max-intakes 5
```

## Output Layout

The exporter writes:

```text
intakeq_export_YYYYMMDD_HHMMSS/
  export_metadata.json
  clients.json
  clients.csv
  appointments.json
  appointments.csv
  intakes_summary.json
  intakes_summary.csv
  intakes_full.json
  by_patient/
    client-123/
      demographics.json
      appointments.json
      intakes.json
      index.json
  pdfs/
    intakes/
    consents/
```

The `pdfs/` folders are only created when `--download-pdfs` is used.

## Rate Limits

The default delay is `6.2` seconds between requests because IntakeQ's standard
API limit is commonly 10 requests per minute. Full intake and PDF exports can
use many requests, so large practices may need to run this in batches.

## Verify

```bash
python3 -m unittest
python3 -m py_compile intakeq_exporter.py app/main.py
```
