#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = "https://intakeq.com/api/v1"
DEFAULT_START_DATE = "1900-01-01"
DEFAULT_END_DATE = "2100-12-31"
DEFAULT_DELAY_SECONDS = 6.2
PAGE_SIZE = 100
ProgressLog = Callable[[str], None]


class IntakeQAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    metadata: dict[str, Any]


def default_log(message: str) -> None:
    print(message, file=sys.stderr)


class IntakeQClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout_seconds: float = 60.0,
        max_retries: int = 4,
    ) -> None:
        if not api_key.strip():
            raise ValueError("API key is required")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        data, content_type = self._request("GET", path, params=params)
        if not data:
            return None
        if "json" not in content_type.lower():
            raise IntakeQAPIError(f"Expected JSON from {path}, got {content_type!r}")
        return json.loads(data.decode("utf-8"))

    def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        data, _content_type = self._request("GET", path, params=params)
        return data

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[bytes, str]:
        url = self._build_url(path, params)
        headers = {
            "Accept": "application/json, application/pdf, */*",
            "User-Agent": "local-intakeq-exporter/1.0",
            "X-Auth-Key": self.api_key,
        }

        for attempt in range(self.max_retries + 1):
            self._throttle()
            request = urllib.request.Request(url, headers=headers, method=method)

            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    self._last_request_at = time.monotonic()
                    return response.read(), response.headers.get("Content-Type", "")
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    wait_seconds = retry_after_seconds(exc.headers.get("Retry-After"))
                    if wait_seconds is None:
                        wait_seconds = min(60.0, max(self.delay_seconds, 2.0**attempt))
                    print(
                        f"Request hit HTTP {exc.code}; retrying in {wait_seconds:.1f}s "
                        f"({method} {path})",
                        file=sys.stderr,
                    )
                    time.sleep(wait_seconds)
                    continue
                raise IntakeQAPIError(
                    f"{method} {path} failed with HTTP {exc.code}: {body[:800]}"
                ) from exc
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                self._last_request_at = time.monotonic()
                reason = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
                if attempt < self.max_retries:
                    wait_seconds = min(60.0, max(self.delay_seconds, 2.0**attempt))
                    print(
                        f"Network error; retrying in {wait_seconds:.1f}s "
                        f"({method} {path}): {reason}",
                        file=sys.stderr,
                    )
                    time.sleep(wait_seconds)
                    continue
                raise IntakeQAPIError(f"{method} {path} failed: {reason}") from exc

        raise IntakeQAPIError(f"{method} {path} failed after retries")

    def _build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        clean_params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None and value != ""
        }
        if clean_params:
            url = f"{url}?{urllib.parse.urlencode(clean_params, doseq=True)}"
        return url

    def _throttle(self) -> None:
        if self.delay_seconds <= 0 or self._last_request_at <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def quote_segment(value: Any) -> str:
    return urllib.parse.quote(str(value), safe="")


def fetch_paged(
    api: IntakeQClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    label: str,
    max_pages: int | None = None,
    log: ProgressLog = default_log,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1

    while True:
        if max_pages is not None and page > max_pages:
            break

        request_params = dict(params or {})
        request_params["page"] = page
        log(f"Fetching {label} page {page}...")
        page_items = api.get_json(path, request_params)

        if page_items is None:
            break
        if not isinstance(page_items, list):
            raise IntakeQAPIError(f"Expected list from {path}, got {type(page_items).__name__}")
        if not page_items:
            break

        items.extend(page_items)
        if len(page_items) < PAGE_SIZE:
            break
        page += 1

    return items


def fetch_clients(
    api: IntakeQClient,
    max_pages: int | None,
    *,
    params: dict[str, Any] | None = None,
    log: ProgressLog = default_log,
) -> list[dict[str, Any]]:
    request_params = {"includeProfile": "true", **(params or {})}
    clients = fetch_paged(
        api,
        "clients",
        params=request_params,
        label="clients",
        max_pages=max_pages,
        log=log,
    )

    if clients and not any("ClientId" in client for client in clients[:10]):
        fallback_params = dict(request_params)
        fallback_params.pop("includeProfile", None)
        fallback_params["IncludeProfile"] = "true"
        log(
            "Client profile fields were not returned with includeProfile; retrying "
            "with IncludeProfile."
        )
        clients = fetch_paged(
            api,
            "clients",
            params=fallback_params,
            label="clients",
            max_pages=max_pages,
            log=log,
        )

    return clients


def fetch_full_intakes(
    api: IntakeQClient,
    summaries: list[dict[str, Any]],
    *,
    output_dir: Path,
    download_pdfs: bool,
    max_intakes: int | None,
    log: ProgressLog = default_log,
) -> list[dict[str, Any]]:
    full_intakes: list[dict[str, Any]] = []
    selected_summaries = summaries[:max_intakes] if max_intakes is not None else summaries

    for index, summary in enumerate(selected_summaries, start=1):
        intake_id = summary.get("Id")
        if not intake_id:
            log(f"Skipping intake summary without Id at index {index}")
            continue

        log(f"Fetching full intake {index}/{len(selected_summaries)}: {intake_id}")
        full_intake = api.get_json(f"intakes/{quote_segment(intake_id)}")
        if isinstance(full_intake, dict):
            full_intakes.append(full_intake)
            if download_pdfs:
                download_intake_pdfs(api, output_dir, full_intake, log=log)
        else:
            log(f"Skipping non-object full intake response for {intake_id}")

    return full_intakes


def download_intake_pdfs(
    api: IntakeQClient,
    output_dir: Path,
    intake: dict[str, Any],
    *,
    log: ProgressLog = default_log,
) -> None:
    intake_id = intake.get("Id")
    if not intake_id:
        return

    intake_pdf_dir = output_dir / "pdfs" / "intakes"
    consent_pdf_dir = output_dir / "pdfs" / "consents"
    intake_pdf_dir.mkdir(parents=True, exist_ok=True)
    consent_pdf_dir.mkdir(parents=True, exist_ok=True)

    intake_pdf_path = intake_pdf_dir / f"{safe_filename(str(intake_id))}.pdf"
    if not intake_pdf_path.exists():
        log(f"Downloading intake PDF: {intake_id}")
        intake_pdf_path.write_bytes(api.get_bytes(f"intakes/{quote_segment(intake_id)}/pdf"))

    for consent in intake.get("ConsentForms") or []:
        if not isinstance(consent, dict):
            continue
        consent_id = consent.get("Id")
        if not consent_id:
            continue
        consent_name = safe_filename(str(consent.get("Name") or "consent"))
        consent_pdf_path = consent_pdf_dir / (
            f"{safe_filename(str(intake_id))}__{safe_filename(str(consent_id))}__{consent_name}.pdf"
        )
        if consent_pdf_path.exists():
            continue
        log(f"Downloading consent PDF: {intake_id} / {consent_id}")
        consent_pdf_path.write_bytes(
            api.get_bytes(
                f"intakes/{quote_segment(intake_id)}/consent/{quote_segment(consent_id)}/pdf"
            )
        )


def patient_reference(record: dict[str, Any]) -> str:
    for key in ("ClientId", "ClientNumber"):
        value = record.get(key)
        if value not in (None, ""):
            return f"client:{value}"

    external_id = record.get("ExternalClientId")
    if external_id not in (None, ""):
        return f"external:{external_id}"

    email = record.get("ClientEmail") or record.get("Email")
    if email not in (None, ""):
        digest = hashlib.sha256(str(email).strip().lower().encode("utf-8")).hexdigest()[:12]
        return f"email-sha256:{digest}"

    name = record.get("ClientName") or record.get("Name")
    if name not in (None, ""):
        digest = hashlib.sha256(str(name).strip().lower().encode("utf-8")).hexdigest()[:12]
        return f"name-sha256:{digest}"

    digest = hashlib.sha256(
        json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"unknown-sha256:{digest}"


def patient_dir_name(reference: str) -> str:
    namespace, _, value = reference.partition(":")
    if namespace == "client":
        return f"client-{safe_filename(value)}"
    if namespace == "external":
        return f"external-{safe_filename(value)}"
    return f"{safe_filename(namespace)}-{safe_filename(value)}"


def group_by_patient(
    clients: list[dict[str, Any]],
    appointments: list[dict[str, Any]],
    intakes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    def entry_for(record: dict[str, Any]) -> dict[str, Any]:
        reference = patient_reference(record)
        return grouped.setdefault(
            reference,
            {
                "patientReference": reference,
                "demographics": None,
                "appointments": [],
                "intakes": [],
            },
        )

    for client in clients:
        entry_for(client)["demographics"] = client

    for appointment in appointments:
        entry_for(appointment)["appointments"].append(appointment)

    for intake in intakes:
        entry_for(intake)["intakes"].append(intake)

    return grouped


def write_grouped_patients(output_dir: Path, grouped: dict[str, dict[str, Any]]) -> None:
    by_patient_dir = output_dir / "by_patient"
    by_patient_dir.mkdir(parents=True, exist_ok=True)

    for reference, payload in sorted(grouped.items()):
        patient_dir = by_patient_dir / patient_dir_name(reference)
        patient_dir.mkdir(parents=True, exist_ok=True)
        write_json(patient_dir / "index.json", {"patientReference": reference})
        write_json(patient_dir / "demographics.json", payload.get("demographics"))
        write_json(patient_dir / "appointments.json", payload.get("appointments") or [])
        write_json(patient_dir / "intakes.json", payload.get("intakes") or [])


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for record in records for key in record.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({key: csv_value(record.get(key)) for key in fieldnames})


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip(".-_")
    return cleaned[:120] or "item"


def today_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_output_dir() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(f"intakeq_export_{stamp}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export IntakeQ clients, appointments, forms, consents, and questionnaires."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write exports into. Defaults to intakeq_export_YYYYMMDD_HHMMSS.",
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help="Start date for appointment and intake queries in yyyy-MM-dd format.",
    )
    parser.add_argument(
        "--end-date",
        default=DEFAULT_END_DATE,
        help="End date for appointment and intake queries in yyyy-MM-dd format.",
    )
    parser.add_argument(
        "--submitted-only",
        action="store_true",
        help="Only export submitted intakes. By default all intake statuses are requested.",
    )
    parser.add_argument(
        "--client-search",
        default=None,
        help=(
            "Export matching clients instead of all clients. Supports partial name/email "
            "or a numeric IntakeQ client ID."
        ),
    )
    parser.add_argument(
        "--client-created-start",
        default=None,
        help="Only export clients created on or after this yyyy-MM-dd date.",
    )
    parser.add_argument(
        "--client-created-end",
        default=None,
        help="Only export clients created on or before this yyyy-MM-dd date.",
    )
    parser.add_argument(
        "--client-updated-start",
        default=None,
        help="Only export clients updated on or after this yyyy-MM-dd date.",
    )
    parser.add_argument(
        "--client-updated-end",
        default=None,
        help="Only export clients updated on or before this yyyy-MM-dd date.",
    )
    parser.add_argument(
        "--deleted-clients-only",
        action="store_true",
        help="Export only recently deleted clients, as supported by IntakeQ.",
    )
    parser.add_argument(
        "--download-pdfs",
        action="store_true",
        help="Download intake package PDFs and consent PDFs. This can use many API requests.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit paginated list calls for test runs.",
    )
    parser.add_argument(
        "--max-intakes",
        type=int,
        default=None,
        help="Limit full-intake fetches for test runs.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Delay between API requests. Default respects 10 requests/minute.",
    )
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help="Override the IntakeQ API base URL.",
    )
    return parser.parse_args(argv)


def validate_date(value: str, arg_name: str) -> None:
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"{arg_name} must use yyyy-MM-dd format, got {value!r}") from exc


def build_client_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if args.client_search:
        params["search"] = args.client_search
    if args.client_created_start:
        params["dateCreatedStart"] = args.client_created_start
    if args.client_created_end:
        params["dateCreatedEnd"] = args.client_created_end
    if args.client_updated_start:
        params["dateUpdatedStart"] = args.client_updated_start
    if args.client_updated_end:
        params["dateUpdatedEnd"] = args.client_updated_end
    if args.deleted_clients_only:
        params["deletedOnly"] = "true"
    return params


def client_export_scope(params: dict[str, Any]) -> str:
    if not params:
        return "all clients"
    if params.get("deletedOnly") == "true":
        return "recently deleted clients"
    if "search" in params:
        return f"clients matching {params['search']!r}"
    return "filtered clients"


def validate_export_args(args: argparse.Namespace) -> None:
    validate_date(args.start_date, "--start-date")
    validate_date(args.end_date, "--end-date")
    for attr, flag in [
        ("client_created_start", "--client-created-start"),
        ("client_created_end", "--client-created-end"),
        ("client_updated_start", "--client-updated-start"),
        ("client_updated_end", "--client-updated-end"),
    ]:
        value = getattr(args, attr)
        if value:
            validate_date(value, flag)


def get_api_key() -> str:
    api_key = os.environ.get("INTAKEQ_API_KEY")
    if api_key:
        return api_key
    return getpass.getpass("IntakeQ API key: ")


def perform_export(
    api_key: str,
    args: argparse.Namespace,
    *,
    log: ProgressLog = default_log,
) -> ExportResult:
    validate_export_args(args)
    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    api = IntakeQClient(
        api_key,
        base_url=args.base_url,
        delay_seconds=args.delay_seconds,
    )

    log(f"Writing export to {output_dir.resolve()}")

    client_params = build_client_params(args)
    log(f"Client export scope: {client_export_scope(client_params)}")
    clients = fetch_clients(api, args.max_pages, params=client_params, log=log)
    appointments = fetch_paged(
        api,
        "appointments",
        params={"startDate": args.start_date, "endDate": args.end_date},
        label="appointments",
        max_pages=args.max_pages,
        log=log,
    )
    intake_params: dict[str, Any] = {
        "startDate": args.start_date,
        "endDate": args.end_date,
    }
    if not args.submitted_only:
        intake_params["all"] = "true"
    intake_summaries = fetch_paged(
        api,
        "intakes/summary",
        params=intake_params,
        label="intake summaries",
        max_pages=args.max_pages,
        log=log,
    )
    full_intakes = fetch_full_intakes(
        api,
        intake_summaries,
        output_dir=output_dir,
        download_pdfs=args.download_pdfs,
        max_intakes=args.max_intakes,
        log=log,
    )

    write_json(output_dir / "clients.json", clients)
    write_json(output_dir / "appointments.json", appointments)
    write_json(output_dir / "intakes_summary.json", intake_summaries)
    write_json(output_dir / "intakes_full.json", full_intakes)
    write_csv(output_dir / "clients.csv", clients)
    write_csv(output_dir / "appointments.csv", appointments)
    write_csv(output_dir / "intakes_summary.csv", intake_summaries)

    grouped = group_by_patient(clients, appointments, full_intakes)
    write_grouped_patients(output_dir, grouped)

    metadata = {
        "generatedAt": today_utc_iso(),
        "baseUrl": args.base_url,
        "dateRange": {
            "startDate": args.start_date,
            "endDate": args.end_date,
        },
        "submittedOnly": bool(args.submitted_only),
        "clientExportScope": client_export_scope(client_params),
        "clientFilters": client_params,
        "downloadedPdfs": bool(args.download_pdfs),
        "counts": {
            "clients": len(clients),
            "appointments": len(appointments),
            "intakeSummaries": len(intake_summaries),
            "fullIntakes": len(full_intakes),
            "patients": len(grouped),
        },
    }
    write_json(output_dir / "export_metadata.json", metadata)

    return ExportResult(output_dir=output_dir, metadata=metadata)


def run(argv: list[str]) -> int:
    args = parse_args(argv)
    result = perform_export(get_api_key(), args)
    print(json.dumps(result.metadata, indent=2), file=sys.stderr)
    print(result.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
