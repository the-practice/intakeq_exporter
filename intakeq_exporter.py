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
RATE_LIMIT_BASE_WAIT_SECONDS = 60.0
RATE_LIMIT_MAX_WAIT_SECONDS = 300.0
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
                        if exc.code == 429:
                            wait_seconds = min(
                                RATE_LIMIT_MAX_WAIT_SECONDS,
                                RATE_LIMIT_BASE_WAIT_SECONDS * (2 ** attempt),
                            )
                        else:
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


def load_or_fetch_paged_list(
    output_dir: Path,
    phase: str,
    api: IntakeQClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    label: str,
    max_pages: int | None = None,
    log: ProgressLog = default_log,
) -> list[dict[str, Any]]:
    json_path = output_dir / f"{phase}.json"
    if json_path.exists() and json_path.stat().st_size > 0:
        try:
            cached = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"Resume: {json_path.name} is unreadable, refetching.")
        else:
            if isinstance(cached, list):
                log(f"Resume: loaded {len(cached)} {phase} records from {json_path.name}.")
                return cached
            log(f"Resume: {json_path.name} is not a list, refetching.")

    pages_dir = output_dir / f"_{phase}_pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    all_items: list[dict[str, Any]] = []
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break

        page_file = pages_dir / f"page_{page:05d}.json"
        cached_page: list[dict[str, Any]] | None = None
        if page_file.exists() and page_file.stat().st_size > 0:
            try:
                value = json.loads(page_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                value = None
            if isinstance(value, list):
                cached_page = value

        if cached_page is not None:
            log(f"Resume: loaded cached {label} page {page} ({len(cached_page)} records).")
            page_items = cached_page
        else:
            request_params = dict(params or {})
            request_params["page"] = page
            log(f"Fetching {label} page {page}...")
            page_items = api.get_json(path, request_params)
            if page_items is None:
                page_items = []
            if not isinstance(page_items, list):
                raise IntakeQAPIError(
                    f"Expected list from {path}, got {type(page_items).__name__}"
                )
            write_json(page_file, page_items)

        if not page_items:
            break
        all_items.extend(page_items)
        if len(page_items) < PAGE_SIZE:
            break
        page += 1

    write_json(json_path, all_items)
    write_csv(output_dir / f"{phase}.csv", all_items)
    return all_items


def load_or_fetch_list(
    output_dir: Path,
    phase: str,
    fetch_fn: Callable[[], list[dict[str, Any]]],
    *,
    log: ProgressLog = default_log,
) -> list[dict[str, Any]]:
    json_path = output_dir / f"{phase}.json"
    if json_path.exists() and json_path.stat().st_size > 0:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"Resume: {json_path.name} is unreadable, refetching.")
        else:
            if isinstance(data, list):
                log(f"Resume: loaded {len(data)} {phase} records from {json_path.name}.")
                return data
            log(f"Resume: {json_path.name} is not a list, refetching.")

    data = fetch_fn()
    write_json(json_path, data)
    write_csv(output_dir / f"{phase}.csv", data)
    return data


def fetch_full_intakes_resumable(
    api: IntakeQClient,
    summaries: list[dict[str, Any]],
    *,
    output_dir: Path,
    download_pdfs: bool,
    max_intakes: int | None,
    log: ProgressLog = default_log,
) -> tuple[list[dict[str, Any]], list[str]]:
    intakes_dir = output_dir / "intakes_full"
    intakes_dir.mkdir(parents=True, exist_ok=True)

    selected = summaries[:max_intakes] if max_intakes is not None else summaries
    full_intakes: list[dict[str, Any]] = []
    skipped: list[str] = []

    for index, summary in enumerate(selected, start=1):
        intake_id = summary.get("Id")
        if not intake_id:
            log(f"Skipping intake summary without Id at index {index}")
            continue

        intake_file = intakes_dir / f"{safe_filename(str(intake_id))}.json"
        if intake_file.exists() and intake_file.stat().st_size > 0:
            try:
                cached = json.loads(intake_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log(f"Resume: {intake_file.name} is unreadable, refetching.")
            else:
                if isinstance(cached, dict):
                    full_intakes.append(cached)
                    continue

        log(f"Fetching full intake {index}/{len(selected)}: {intake_id}")
        try:
            full_intake = api.get_json(f"intakes/{quote_segment(intake_id)}")
        except IntakeQAPIError as exc:
            log(f"Failed to fetch intake {intake_id}: {exc}; skipping.")
            skipped.append(str(intake_id))
            continue

        if not isinstance(full_intake, dict):
            log(f"Skipping non-object full intake response for {intake_id}")
            skipped.append(str(intake_id))
            continue

        write_json(intake_file, full_intake)
        full_intakes.append(full_intake)

        if download_pdfs:
            try:
                download_intake_pdfs(api, output_dir, full_intake, log=log)
            except IntakeQAPIError as exc:
                log(f"Failed to download PDFs for {intake_id}: {exc}; continuing.")

    return full_intakes, skipped


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


ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10-cm"
MARITAL_STATUS_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus"
SUBSCRIBER_RELATIONSHIP_SYSTEM = (
    "http://terminology.hl7.org/CodeSystem/subscriber-relationship"
)
COVERAGE_CLASS_SYSTEM = "http://terminology.hl7.org/CodeSystem/coverage-class"
INTAKEQ_CLIENT_ID_SYSTEM = "https://intakeq.com/client-id"
INTAKEQ_EXTERNAL_CLIENT_ID_SYSTEM = "https://intakeq.com/external-client-id"

_GENDER_MAP = {"male": "male", "female": "female"}
_MARITAL_MAP = {
    "married": ("M", "Married"),
    "single": ("S", "Never Married"),
    "divorced": ("D", "Divorced"),
    "widowed": ("W", "Widowed"),
    "separated": ("L", "Legally Separated"),
}
_APPOINTMENT_STATUS_MAP = {
    "confirmed": "booked",
    "waitingconfirmation": "pending",
    "declined": "cancelled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "missed": "noshow",
}
_QR_STATUS_MAP = {
    "completed": "completed",
    "partial": "in-progress",
    "sent": "in-progress",
    "offline": "completed",
}
_SUBSCRIBER_RELATIONSHIP_MAP = {
    "self": "self",
    "spouse": "spouse",
    "child": "child",
    "parent": "parent",
    "common": "common",
    "other": "other",
}


def _ms_to_datetime_obj(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def ms_to_date(value: Any) -> str | None:
    moment = _ms_to_datetime_obj(value)
    return moment.date().isoformat() if moment else None


def ms_to_datetime(value: Any) -> str | None:
    moment = _ms_to_datetime_obj(value)
    return moment.isoformat() if moment else None


def _iso_or_ms_datetime(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    return ms_to_datetime(value)


def fhir_gender(value: Any) -> str:
    if not value:
        return "unknown"
    return _GENDER_MAP.get(str(value).strip().lower(), "other")


def fhir_marital_status(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    entry = _MARITAL_MAP.get(str(value).strip().lower())
    if not entry:
        return None
    code, display = entry
    return {
        "coding": [{"system": MARITAL_STATUS_SYSTEM, "code": code, "display": display}]
    }


def fhir_appointment_status(value: Any) -> str:
    return _APPOINTMENT_STATUS_MAP.get(str(value or "").strip().lower(), "proposed")


def fhir_resource_id(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9.\-]+", "-", str(value).strip()).strip("-.")
    return cleaned[:64] or "unknown"


def fhir_patient_id(record: dict[str, Any]) -> str:
    return patient_dir_name(patient_reference(record))


def fhir_patient_reference(record: dict[str, Any]) -> dict[str, str]:
    return {"reference": f"Patient/{fhir_patient_id(record)}"}


def _patient_name(client: dict[str, Any]) -> dict[str, Any] | None:
    family = str(client.get("LastName") or "").strip()
    given = [str(part).strip() for part in (client.get("FirstName"), client.get("MiddleName")) if part]
    if not family and not given:
        text = str(client.get("Name") or "").strip()
        return {"use": "official", "text": text} if text else None
    name: dict[str, Any] = {"use": "official"}
    if family:
        name["family"] = family
    if given:
        name["given"] = given
    return name


def _patient_telecom(client: dict[str, Any]) -> list[dict[str, Any]]:
    telecom: list[dict[str, Any]] = []
    email = client.get("Email") or client.get("ClientEmail")
    if email:
        telecom.append({"system": "email", "value": str(email)})
    seen: set[str] = set()
    for field, use in (
        ("MobilePhone", "mobile"),
        ("HomePhone", "home"),
        ("WorkPhone", "work"),
        ("Phone", "home"),
    ):
        value = client.get(field)
        if value and str(value) not in seen:
            seen.add(str(value))
            telecom.append({"system": "phone", "value": str(value), "use": use})
    return telecom


def _patient_address(client: dict[str, Any]) -> dict[str, Any] | None:
    line = [str(part) for part in (client.get("StreetAddress"), client.get("UnitNumber")) if part]
    city = client.get("City")
    state = client.get("StateShort") or client.get("State")
    postal = client.get("PostalCode")
    country = client.get("Country")
    if not any([line, city, state, postal, country]):
        text = client.get("Address")
        return {"text": str(text)} if text else None
    address: dict[str, Any] = {}
    if line:
        address["line"] = line
    if city:
        address["city"] = str(city)
    if state:
        address["state"] = str(state)
    if postal:
        address["postalCode"] = str(postal)
    if country:
        address["country"] = str(country)
    return address


def client_to_patient(client: dict[str, Any]) -> dict[str, Any]:
    patient: dict[str, Any] = {
        "resourceType": "Patient",
        "id": fhir_patient_id(client),
    }

    identifiers: list[dict[str, str]] = []
    client_id = client.get("ClientId") or client.get("ClientNumber")
    if client_id not in (None, ""):
        identifiers.append({"system": INTAKEQ_CLIENT_ID_SYSTEM, "value": str(client_id)})
    external_id = client.get("ExternalClientId")
    if external_id not in (None, ""):
        identifiers.append(
            {"system": INTAKEQ_EXTERNAL_CLIENT_ID_SYSTEM, "value": str(external_id)}
        )
    if identifiers:
        patient["identifier"] = identifiers

    patient["active"] = not bool(client.get("Archived"))

    name = _patient_name(client)
    if name:
        patient["name"] = [name]

    telecom = _patient_telecom(client)
    if telecom:
        patient["telecom"] = telecom

    patient["gender"] = fhir_gender(client.get("Gender"))

    birth_date = ms_to_date(client.get("DateOfBirth"))
    if birth_date:
        patient["birthDate"] = birth_date

    marital = fhir_marital_status(client.get("MaritalStatus"))
    if marital:
        patient["maritalStatus"] = marital

    address = _patient_address(client)
    if address:
        patient["address"] = [address]

    return patient


def client_to_coverages(client: dict[str, Any]) -> list[dict[str, Any]]:
    coverages: list[dict[str, Any]] = []
    reference = fhir_patient_reference(client)
    patient_id = fhir_patient_id(client)

    for order, prefix in ((1, "Primary"), (2, "Secondary")):
        company = client.get(f"{prefix}InsuranceCompany")
        policy = client.get(f"{prefix}InsurancePolicyNumber")
        if not company and not policy:
            continue

        coverage: dict[str, Any] = {
            "resourceType": "Coverage",
            "id": f"{patient_id}-coverage-{order}",
            "status": "active",
            "beneficiary": reference,
            "order": order,
        }
        if policy:
            coverage["subscriberId"] = str(policy)
        if company:
            coverage["payor"] = [{"display": str(company)}]
        group = client.get(f"{prefix}InsuranceGroupNumber")
        if group:
            coverage["class"] = [
                {
                    "type": {
                        "coding": [{"system": COVERAGE_CLASS_SYSTEM, "code": "group"}]
                    },
                    "value": str(group),
                }
            ]
        relationship = _SUBSCRIBER_RELATIONSHIP_MAP.get(
            str(client.get(f"{prefix}InsuranceRelationship") or "").strip().lower()
        )
        if relationship:
            coverage["relationship"] = {
                "coding": [
                    {"system": SUBSCRIBER_RELATIONSHIP_SYSTEM, "code": relationship}
                ]
            }
        coverages.append(coverage)

    return coverages


def appointment_to_fhir(appointment: dict[str, Any]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "Appointment",
        "status": fhir_appointment_status(appointment.get("Status")),
    }
    appt_id = appointment.get("Id")
    if appt_id:
        resource["id"] = fhir_resource_id(appt_id)

    start = appointment.get("StartDateIso") or ms_to_datetime(appointment.get("StartDate"))
    if start:
        resource["start"] = start
    end = appointment.get("EndDateIso") or ms_to_datetime(appointment.get("EndDate"))
    if end:
        resource["end"] = end

    duration = appointment.get("Duration")
    if isinstance(duration, (int, float)) and duration:
        resource["minutesDuration"] = int(duration)
    service = appointment.get("ServiceName")
    if service:
        resource["description"] = str(service)

    participants: list[dict[str, Any]] = [
        {"actor": fhir_patient_reference(appointment), "status": "accepted"}
    ]
    practitioner = appointment.get("PractitionerName")
    if practitioner:
        participants.append({"actor": {"display": str(practitioner)}, "status": "accepted"})
    resource["participant"] = participants

    return resource


def _answer_values(question: dict[str, Any], qtype: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for attachment_data in question.get("Attachments") or []:
        if not isinstance(attachment_data, dict):
            continue
        attachment: dict[str, Any] = {}
        if attachment_data.get("ContentType"):
            attachment["contentType"] = str(attachment_data["ContentType"])
        if attachment_data.get("Url"):
            attachment["url"] = str(attachment_data["Url"])
        if attachment_data.get("FileName"):
            attachment["title"] = str(attachment_data["FileName"])
        if attachment:
            values.append({"valueAttachment": attachment})
    if values:
        return values

    answer = question.get("Answer")
    if answer in (None, ""):
        return []
    if qtype == "datequestion":
        return [{"valueDate": str(answer)}]
    return [{"valueString": str(answer)}]


def _question_to_item(question: dict[str, Any]) -> dict[str, Any]:
    link_id = question.get("Id")
    item: dict[str, Any] = {
        "linkId": str(link_id) if link_id not in (None, "") else "",
        "text": str(question.get("Text") or ""),
    }
    qtype = str(question.get("QuestionType") or "").strip().lower()

    if qtype == "matrix":
        sub_items: list[dict[str, Any]] = []
        for index, row in enumerate(question.get("Rows") or []):
            if not isinstance(row, dict):
                continue
            sub_item: dict[str, Any] = {
                "linkId": f"{item['linkId']}.{index}",
                "text": str(row.get("Text") or ""),
            }
            answers = [
                {"valueString": str(value)}
                for value in (row.get("Answers") or [])
                if value not in (None, "")
            ]
            if answers:
                sub_item["answer"] = answers
            sub_items.append(sub_item)
        if sub_items:
            item["item"] = sub_items
        return item

    answers = _answer_values(question, qtype)
    if answers:
        item["answer"] = answers
    return item


def intake_to_questionnaire_response(intake: dict[str, Any]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "resourceType": "QuestionnaireResponse",
        "status": _QR_STATUS_MAP.get(
            str(intake.get("Status") or "").strip().lower(), "completed"
        ),
        "subject": fhir_patient_reference(intake),
    }
    intake_id = intake.get("Id")
    if intake_id:
        resource["id"] = fhir_resource_id(intake_id)
    template = intake.get("QuestionnaireId")
    if template:
        resource["questionnaire"] = f"https://intakeq.com/questionnaire/{template}"
    authored = ms_to_datetime(intake.get("DateSubmitted"))
    if authored:
        resource["authored"] = authored

    items: list[dict[str, Any]] = []
    for question in intake.get("Questions") or []:
        if isinstance(question, dict):
            items.append(_question_to_item(question))
    resource["item"] = items

    return resource


def note_to_document_reference(note: dict[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "resourceType": "DocumentReference",
        "status": "current",
        "subject": fhir_patient_reference(note),
    }
    note_id = note.get("Id")
    if note_id:
        doc["id"] = fhir_resource_id(note_id)
    doc["docStatus"] = (
        "final" if str(note.get("Status") or "").strip().lower() == "locked" else "preliminary"
    )
    note_name = note.get("NoteName")
    if note_name:
        doc["type"] = {"text": str(note_name)}
    date = ms_to_datetime(note.get("Date"))
    if date:
        doc["date"] = date
    author = note.get("PractitionerName")
    if author:
        doc["author"] = [{"display": str(author)}]

    attachment: dict[str, Any] = {"contentType": "application/pdf"}
    if note_id:
        attachment["url"] = f"{BASE_URL}/notes/{quote_segment(note_id)}/pdf"
    if note_name:
        attachment["title"] = str(note_name)
    doc["content"] = [{"attachment": attachment}]

    return doc


def diagnosis_to_condition(
    diagnosis: dict[str, Any], patient_reference_str: str, index: int
) -> dict[str, Any]:
    patient_id = patient_reference_str.split("/", 1)[-1]
    condition: dict[str, Any] = {
        "resourceType": "Condition",
        "id": f"{patient_id}-condition-{index}",
        "subject": {"reference": patient_reference_str},
    }

    code = diagnosis.get("Code")
    description = diagnosis.get("Description")
    if code or description:
        coding: dict[str, Any] = {"system": ICD10_SYSTEM}
        if code:
            coding["code"] = str(code)
        if description:
            coding["display"] = str(description)
        code_concept: dict[str, Any] = {"coding": [coding]}
        if description:
            code_concept["text"] = str(description)
        condition["code"] = code_concept

    onset = _iso_or_ms_datetime(diagnosis.get("Date"))
    if onset:
        condition["onsetDateTime"] = onset
    abatement = _iso_or_ms_datetime(diagnosis.get("EndDate"))
    if abatement:
        condition["abatementDateTime"] = abatement

    return condition


def build_fhir_resources(
    clients: list[dict[str, Any]],
    appointments: list[dict[str, Any]],
    intakes: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    diagnoses_by_reference: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    resources: dict[str, list[dict[str, Any]]] = {
        "Patient": [],
        "Coverage": [],
        "Appointment": [],
        "QuestionnaireResponse": [],
        "DocumentReference": [],
        "Condition": [],
    }

    for client in clients:
        if not isinstance(client, dict):
            continue
        resources["Patient"].append(client_to_patient(client))
        resources["Coverage"].extend(client_to_coverages(client))

    for appointment in appointments:
        if isinstance(appointment, dict):
            resources["Appointment"].append(appointment_to_fhir(appointment))

    for intake in intakes:
        if isinstance(intake, dict):
            resources["QuestionnaireResponse"].append(
                intake_to_questionnaire_response(intake)
            )

    for note in notes:
        if isinstance(note, dict):
            resources["DocumentReference"].append(note_to_document_reference(note))

    for reference, diagnoses in (diagnoses_by_reference or {}).items():
        patient_reference_str = f"Patient/{patient_dir_name(reference)}"
        for index, diagnosis in enumerate(diagnoses or []):
            if isinstance(diagnosis, dict):
                resources["Condition"].append(
                    diagnosis_to_condition(diagnosis, patient_reference_str, index)
                )

    return resources


def write_fhir_ndjson(
    output_dir: Path, resources: dict[str, list[dict[str, Any]]]
) -> None:
    fhir_dir = output_dir / "fhir"
    fhir_dir.mkdir(parents=True, exist_ok=True)
    for resource_type, items in resources.items():
        if not items:
            continue
        path = fhir_dir / f"{resource_type}.ndjson"
        with path.open("w", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=False, default=str))
                handle.write("\n")


def fetch_diagnoses_resumable(
    api: IntakeQClient,
    clients: list[dict[str, Any]],
    *,
    output_dir: Path,
    log: ProgressLog = default_log,
) -> dict[str, list[dict[str, Any]]]:
    diagnoses_dir = output_dir / "_diagnoses"
    diagnoses_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, list[dict[str, Any]]] = {}
    for client in clients:
        if not isinstance(client, dict):
            continue
        client_id = client.get("ClientId") or client.get("ClientNumber")
        if client_id in (None, ""):
            continue

        reference = patient_reference(client)
        cache_file = diagnoses_dir / f"{patient_dir_name(reference)}.json"
        if cache_file.exists() and cache_file.stat().st_size > 0:
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log(f"Resume: {cache_file.name} is unreadable, refetching diagnoses.")
            else:
                if isinstance(cached, list):
                    result[reference] = cached
                    continue

        log(f"Fetching diagnoses for client {client_id}...")
        try:
            diagnoses = api.get_json(f"client/{quote_segment(client_id)}/diagnoses")
        except IntakeQAPIError as exc:
            log(f"Failed to fetch diagnoses for client {client_id}: {exc}; skipping.")
            diagnoses = []
        if not isinstance(diagnoses, list):
            diagnoses = []

        write_json(cache_file, diagnoses)
        result[reference] = diagnoses

    return result


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
        "--fhir",
        action="store_true",
        help=(
            "Also emit a FHIR R4 bulk export (NDJSON by resource type) under fhir/. "
            "Fetches treatment notes and per-client diagnoses, which uses extra API requests."
        ),
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
    api: Any = None,
) -> ExportResult:
    validate_export_args(args)
    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    if api is None:
        api = IntakeQClient(
            api_key,
            base_url=args.base_url,
            delay_seconds=args.delay_seconds,
        )

    log(f"Writing export to {output_dir.resolve()}")

    client_params = build_client_params(args)
    log(f"Client export scope: {client_export_scope(client_params)}")

    clients = load_or_fetch_list(
        output_dir,
        "clients",
        lambda: fetch_clients(api, args.max_pages, params=client_params, log=log),
        log=log,
    )

    appointments = load_or_fetch_paged_list(
        output_dir,
        "appointments",
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

    intake_summaries = load_or_fetch_paged_list(
        output_dir,
        "intakes_summary",
        api,
        "intakes/summary",
        params=intake_params,
        label="intake summaries",
        max_pages=args.max_pages,
        log=log,
    )

    full_intakes, skipped_intakes = fetch_full_intakes_resumable(
        api,
        intake_summaries,
        output_dir=output_dir,
        download_pdfs=args.download_pdfs,
        max_intakes=args.max_intakes,
        log=log,
    )
    write_json(output_dir / "intakes_full.json", full_intakes)

    grouped = group_by_patient(clients, appointments, full_intakes)
    write_grouped_patients(output_dir, grouped)

    fhir_summary: dict[str, int] | None = None
    if getattr(args, "fhir", False):
        log("Building FHIR R4 export (treatment notes, diagnoses, NDJSON)...")
        notes = load_or_fetch_paged_list(
            output_dir,
            "notes",
            api,
            "notes",
            params={"startDate": args.start_date, "endDate": args.end_date},
            label="treatment notes",
            max_pages=args.max_pages,
            log=log,
        )
        diagnoses = fetch_diagnoses_resumable(api, clients, output_dir=output_dir, log=log)
        fhir_resources = build_fhir_resources(
            clients, appointments, full_intakes, notes, diagnoses
        )
        write_fhir_ndjson(output_dir, fhir_resources)
        fhir_summary = {
            resource_type: len(items) for resource_type, items in fhir_resources.items()
        }
        fhir_summary["notes"] = len(notes)
        log(f"Wrote FHIR NDJSON resources: {fhir_summary}")

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
        "fhirEnabled": bool(getattr(args, "fhir", False)),
        "fhirCounts": fhir_summary,
        "counts": {
            "clients": len(clients),
            "appointments": len(appointments),
            "intakeSummaries": len(intake_summaries),
            "fullIntakes": len(full_intakes),
            "patients": len(grouped),
        },
        "skippedFullIntakes": skipped_intakes,
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
