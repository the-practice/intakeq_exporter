from __future__ import annotations

import email.message
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import intakeq_exporter as exporter


class IntakeQClientRetryTests(unittest.TestCase):
    def _make_response(self, body: bytes = b"[]", content_type: str = "application/json"):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = body
        response.headers.get.return_value = content_type
        return response

    def test_request_retries_on_connection_reset_error(self):
        api = exporter.IntakeQClient("fake-key", delay_seconds=0, max_retries=2)
        success_response = self._make_response()
        calls = {"n": 0}

        def fake_urlopen(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            return success_response

        with mock.patch.object(exporter.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(exporter.time, "sleep"):
            result = api.get_json("clients")

        self.assertEqual(result, [])
        self.assertEqual(calls["n"], 2)

    def _make_http_error(self, code: int, body: bytes = b'{"Message":"err"}'):
        hdrs = email.message.Message()
        return urllib.error.HTTPError(
            url="http://example.test/x",
            code=code,
            msg="error",
            hdrs=hdrs,
            fp=io.BytesIO(body),
        )

    def test_request_waits_at_least_sixty_seconds_on_429(self):
        api = exporter.IntakeQClient("fake-key", delay_seconds=0, max_retries=3)
        success_response = self._make_response()
        sleeps: list[float] = []
        calls = {"n": 0}

        def fake_urlopen(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._make_http_error(429, b'{"Message":"Too many requests."}')
            return success_response

        with mock.patch.object(exporter.urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch.object(exporter.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            result = api.get_json("clients")

        self.assertEqual(result, [])
        self.assertEqual(calls["n"], 2)
        self.assertTrue(
            any(s >= 60 for s in sleeps),
            f"Expected at least one sleep >= 60s on 429 retry, got {sleeps}",
        )


class FakeAPI:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        page = int((params or {}).get("page", 1))
        if page <= len(self.pages):
            return self.pages[page - 1]
        return []


class ExporterTests(unittest.TestCase):
    def test_fetch_paged_stops_on_short_page(self):
        api = FakeAPI(
            [
                [{"Id": index} for index in range(exporter.PAGE_SIZE)],
                [{"Id": "last"}],
            ]
        )

        records = exporter.fetch_paged(api, "clients", label="clients")

        self.assertEqual(len(records), exporter.PAGE_SIZE + 1)
        self.assertEqual([call[1]["page"] for call in api.calls], [1, 2])

    def test_patient_reference_prefers_client_id(self):
        record = {
            "ClientId": 123,
            "ExternalClientId": "external-1",
            "ClientEmail": "patient@example.com",
        }

        self.assertEqual(exporter.patient_reference(record), "client:123")

    def test_patient_reference_hashes_email(self):
        reference = exporter.patient_reference({"ClientEmail": "Patient@Example.com"})

        self.assertTrue(reference.startswith("email-sha256:"))
        self.assertNotIn("Patient", reference)
        self.assertNotIn("Example", reference)

    def test_group_by_patient_merges_related_records(self):
        clients = [{"ClientId": 123, "Name": "Test Patient"}]
        appointments = [{"ClientId": 123, "Id": "appt-1"}]
        intakes = [{"ClientId": 123, "Id": "intake-1"}]

        grouped = exporter.group_by_patient(clients, appointments, intakes)

        self.assertEqual(set(grouped.keys()), {"client:123"})
        self.assertEqual(grouped["client:123"]["demographics"], clients[0])
        self.assertEqual(grouped["client:123"]["appointments"], appointments)
        self.assertEqual(grouped["client:123"]["intakes"], intakes)

    def test_safe_filename_strips_unsafe_characters(self):
        self.assertEqual(exporter.safe_filename(" a/b:c* "), "a-b-c")

    def test_client_params_default_to_all_clients(self):
        args = exporter.parse_args([])

        self.assertEqual(exporter.build_client_params(args), {})
        self.assertEqual(exporter.client_export_scope({}), "all clients")

    def test_client_params_allow_search_and_dates(self):
        args = exporter.parse_args(
            [
                "--client-search",
                "Jane",
                "--client-created-start",
                "2025-01-01",
                "--client-updated-end",
                "2026-01-01",
            ]
        )

        self.assertEqual(
            exporter.build_client_params(args),
            {
                "search": "Jane",
                "dateCreatedStart": "2025-01-01",
                "dateUpdatedEnd": "2026-01-01",
            },
        )

    def test_deleted_clients_scope(self):
        args = exporter.parse_args(["--deleted-clients-only"])

        params = exporter.build_client_params(args)

        self.assertEqual(params, {"deletedOnly": "true"})
        self.assertEqual(exporter.client_export_scope(params), "recently deleted clients")


class StubAPI:
    """Path-keyed canned responses; raise IntakeQAPIError by mapping to one."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path not in self.responses:
            raise AssertionError(f"unexpected path: {path}")
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response


class LoadOrFetchListTests(unittest.TestCase):
    def test_writes_json_and_csv_after_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            records = [{"Id": 1, "Name": "A"}]

            result = exporter.load_or_fetch_list(
                output_dir, "clients", lambda: records, log=lambda _m: None
            )

            self.assertEqual(result, records)
            self.assertTrue((output_dir / "clients.json").exists())
            self.assertTrue((output_dir / "clients.csv").exists())
            self.assertEqual(
                json.loads((output_dir / "clients.json").read_text()), records
            )

    def test_loads_existing_file_without_calling_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            existing = [{"Id": 1}, {"Id": 2}]
            (output_dir / "clients.json").write_text(json.dumps(existing))

            def fetch_should_not_be_called():
                raise AssertionError("fetch_fn must not run when file exists")

            result = exporter.load_or_fetch_list(
                output_dir, "clients", fetch_should_not_be_called, log=lambda _m: None
            )

            self.assertEqual(result, existing)

    def test_refetches_when_file_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "clients.json").write_text("not json")
            fresh = [{"Id": 9}]

            result = exporter.load_or_fetch_list(
                output_dir, "clients", lambda: fresh, log=lambda _m: None
            )

            self.assertEqual(result, fresh)
            self.assertEqual(
                json.loads((output_dir / "clients.json").read_text()), fresh
            )


class PagedListAPI:
    """API stub that returns canned page responses; failures map to exceptions."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_json(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        page = int((params or {}).get("page", 1))
        if page > len(self.pages):
            return []
        response = self.pages[page - 1]
        if isinstance(response, Exception):
            raise response
        return response


class LoadOrFetchPagedListTests(unittest.TestCase):
    def test_writes_each_page_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            page1 = [{"Id": i} for i in range(exporter.PAGE_SIZE)]
            page2 = [{"Id": "last"}]
            api = PagedListAPI([page1, page2])

            result = exporter.load_or_fetch_paged_list(
                output_dir,
                "appointments",
                api,
                "appointments",
                params={"startDate": "2025-01-01"},
                label="appointments",
                log=lambda _m: None,
            )

            self.assertEqual(len(result), exporter.PAGE_SIZE + 1)
            self.assertTrue((output_dir / "_appointments_pages" / "page_00001.json").exists())
            self.assertTrue((output_dir / "_appointments_pages" / "page_00002.json").exists())
            self.assertTrue((output_dir / "appointments.json").exists())

    def test_resumes_from_existing_page_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            pages_dir = output_dir / "_appointments_pages"
            pages_dir.mkdir(parents=True)
            page1 = [{"Id": i} for i in range(exporter.PAGE_SIZE)]
            (pages_dir / "page_00001.json").write_text(json.dumps(page1))

            api = PagedListAPI([None, [{"Id": "page2"}]])

            result = exporter.load_or_fetch_paged_list(
                output_dir,
                "appointments",
                api,
                "appointments",
                params={},
                label="appointments",
                log=lambda _m: None,
            )

            self.assertEqual(len(result), exporter.PAGE_SIZE + 1)
            pages_called = [int(call[1]["page"]) for call in api.calls]
            self.assertEqual(pages_called, [2])

    def test_partial_pages_survive_mid_phase_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            page1 = [{"Id": i} for i in range(exporter.PAGE_SIZE)]
            failure = exporter.IntakeQAPIError("429 boom")
            api = PagedListAPI([page1, failure])

            with self.assertRaises(exporter.IntakeQAPIError):
                exporter.load_or_fetch_paged_list(
                    output_dir,
                    "appointments",
                    api,
                    "appointments",
                    params={},
                    label="appointments",
                    log=lambda _m: None,
                )

            page1_file = output_dir / "_appointments_pages" / "page_00001.json"
            self.assertTrue(page1_file.exists())
            self.assertEqual(
                json.loads(page1_file.read_text()),
                page1,
            )
            self.assertFalse((output_dir / "appointments.json").exists())


class FetchFullIntakesResumableTests(unittest.TestCase):
    def test_writes_each_intake_to_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            api = StubAPI(
                {
                    "intakes/i1": {"Id": "i1", "ConsentForms": []},
                    "intakes/i2": {"Id": "i2", "ConsentForms": []},
                }
            )

            results, skipped = exporter.fetch_full_intakes_resumable(
                api,
                [{"Id": "i1"}, {"Id": "i2"}],
                output_dir=output_dir,
                download_pdfs=False,
                max_intakes=None,
                log=lambda _m: None,
            )

            self.assertEqual(len(results), 2)
            self.assertEqual(skipped, [])
            self.assertTrue((output_dir / "intakes_full" / "i1.json").exists())
            self.assertTrue((output_dir / "intakes_full" / "i2.json").exists())

    def test_skips_intakes_already_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            intakes_dir = output_dir / "intakes_full"
            intakes_dir.mkdir()
            (intakes_dir / "i1.json").write_text(
                json.dumps({"Id": "i1", "cached": True})
            )
            api = StubAPI({"intakes/i2": {"Id": "i2", "ConsentForms": []}})

            results, skipped = exporter.fetch_full_intakes_resumable(
                api,
                [{"Id": "i1"}, {"Id": "i2"}],
                output_dir=output_dir,
                download_pdfs=False,
                max_intakes=None,
                log=lambda _m: None,
            )

            self.assertEqual(skipped, [])
            self.assertEqual(len(results), 2)
            paths_called = [call[0] for call in api.calls]
            self.assertEqual(paths_called, ["intakes/i2"])

    def test_continues_when_single_intake_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            api = StubAPI(
                {
                    "intakes/i1": exporter.IntakeQAPIError("boom"),
                    "intakes/i2": {"Id": "i2", "ConsentForms": []},
                }
            )

            results, skipped = exporter.fetch_full_intakes_resumable(
                api,
                [{"Id": "i1"}, {"Id": "i2"}],
                output_dir=output_dir,
                download_pdfs=False,
                max_intakes=None,
                log=lambda _m: None,
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["Id"], "i2")
            self.assertEqual(skipped, ["i1"])


class PerformExportResumeTests(unittest.TestCase):
    def _args(self, output_dir: Path):
        return exporter.parse_args(
            [
                "--output-dir",
                str(output_dir),
                "--start-date",
                "2025-01-01",
                "--end-date",
                "2025-12-31",
                "--delay-seconds",
                "0",
            ]
        )

    def test_resumes_appointments_after_failed_intakes_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "data"

            full_responses = {
                "clients": [{"ClientId": 1, "ClientName": "A"}],
                "appointments": [{"ClientId": 1, "Id": "a1"}],
                "intakes/summary": exporter.IntakeQAPIError("intakes-down"),
            }

            class PagedStub:
                def __init__(self, responses):
                    self.responses = responses
                    self.calls = []

                def get_json(self, path, params=None):
                    self.calls.append((path, dict(params or {})))
                    response = self.responses[path]
                    if isinstance(response, Exception):
                        raise response
                    page = int((params or {}).get("page", 1))
                    return response if page == 1 else []

            failing_api = PagedStub(full_responses)
            args = self._args(output_dir)

            with self.assertRaises(exporter.IntakeQAPIError):
                exporter.perform_export(
                    "fake", args, log=lambda _m: None, api=failing_api
                )

            self.assertTrue((output_dir / "clients.json").exists())
            self.assertTrue((output_dir / "appointments.json").exists())

            recovered_responses = {
                "clients": exporter.IntakeQAPIError("should not refetch"),
                "appointments": exporter.IntakeQAPIError("should not refetch"),
                "intakes/summary": [{"Id": "i1"}],
                "intakes/i1": {"Id": "i1", "ConsentForms": []},
            }
            recovered_api = PagedStub(recovered_responses)
            args2 = self._args(output_dir)

            result = exporter.perform_export(
                "fake", args2, log=lambda _m: None, api=recovered_api
            )

            paths = [call[0] for call in recovered_api.calls]
            self.assertNotIn("clients", paths)
            self.assertNotIn("appointments", paths)
            self.assertIn("intakes/summary", paths)
            self.assertIn("intakes/i1", paths)
            self.assertEqual(result.metadata["counts"]["clients"], 1)
            self.assertEqual(result.metadata["counts"]["appointments"], 1)
            self.assertEqual(result.metadata["counts"]["fullIntakes"], 1)


class FhirHelperTests(unittest.TestCase):
    def test_ms_to_date_converts_epoch(self):
        self.assertEqual(exporter.ms_to_date(0), "1970-01-01")

    def test_ms_to_date_handles_missing(self):
        self.assertIsNone(exporter.ms_to_date(None))
        self.assertIsNone(exporter.ms_to_date(""))
        self.assertIsNone(exporter.ms_to_date("not-a-number"))

    def test_ms_to_datetime_is_iso_utc(self):
        self.assertEqual(exporter.ms_to_datetime(0), "1970-01-01T00:00:00+00:00")

    def test_fhir_gender_maps_known_values(self):
        self.assertEqual(exporter.fhir_gender("Male"), "male")
        self.assertEqual(exporter.fhir_gender("female"), "female")
        self.assertEqual(exporter.fhir_gender("Nonbinary"), "other")
        self.assertEqual(exporter.fhir_gender(None), "unknown")
        self.assertEqual(exporter.fhir_gender(""), "unknown")

    def test_fhir_marital_status_known_and_unknown(self):
        married = exporter.fhir_marital_status("Married")
        self.assertEqual(married["coding"][0]["code"], "M")
        self.assertIsNone(exporter.fhir_marital_status("Confidential"))
        self.assertIsNone(exporter.fhir_marital_status(None))

    def test_fhir_appointment_status_mapping(self):
        self.assertEqual(exporter.fhir_appointment_status("Confirmed"), "booked")
        self.assertEqual(exporter.fhir_appointment_status("WaitingConfirmation"), "pending")
        self.assertEqual(exporter.fhir_appointment_status("Canceled"), "cancelled")
        self.assertEqual(exporter.fhir_appointment_status("Declined"), "cancelled")
        self.assertEqual(exporter.fhir_appointment_status("Missed"), "noshow")
        self.assertEqual(exporter.fhir_appointment_status("Whatever"), "proposed")


class FhirPatientTests(unittest.TestCase):
    def _client(self):
        return {
            "ClientId": 123,
            "ExternalClientId": "ext-1",
            "FirstName": "John",
            "MiddleName": "Robert",
            "LastName": "Doe",
            "Email": "john@example.com",
            "Phone": "(904) 555-1234",
            "DateOfBirth": 0,
            "Gender": "Male",
            "MaritalStatus": "Married",
            "StreetAddress": "123 Main St",
            "UnitNumber": "Apt 5",
            "City": "Jacksonville",
            "StateShort": "FL",
            "PostalCode": "32256",
            "Country": "USA",
            "Archived": False,
        }

    def test_patient_core_fields(self):
        patient = exporter.client_to_patient(self._client())

        self.assertEqual(patient["resourceType"], "Patient")
        self.assertEqual(patient["id"], "client-123")
        self.assertEqual(patient["gender"], "male")
        self.assertEqual(patient["birthDate"], "1970-01-01")
        self.assertEqual(patient["name"][0]["family"], "Doe")
        self.assertEqual(patient["name"][0]["given"], ["John", "Robert"])
        self.assertTrue(patient["active"])

    def test_patient_identifiers_include_external_id(self):
        patient = exporter.client_to_patient(self._client())
        values = {ident["value"] for ident in patient["identifier"]}
        self.assertIn("123", values)
        self.assertIn("ext-1", values)

    def test_patient_telecom_and_address(self):
        patient = exporter.client_to_patient(self._client())
        systems = {t["system"]: t["value"] for t in patient["telecom"]}
        self.assertEqual(systems["email"], "john@example.com")
        self.assertEqual(systems["phone"], "(904) 555-1234")
        address = patient["address"][0]
        self.assertEqual(address["city"], "Jacksonville")
        self.assertEqual(address["state"], "FL")
        self.assertIn("123 Main St", address["line"])

    def test_patient_active_reflects_archived(self):
        client = self._client()
        client["Archived"] = True
        patient = exporter.client_to_patient(client)
        self.assertFalse(patient["active"])

    def test_coverages_primary_and_secondary(self):
        client = {
            "ClientId": 5,
            "PrimaryInsuranceCompany": "Blue Cross",
            "PrimaryInsurancePolicyNumber": "ABC123",
            "PrimaryInsuranceGroupNumber": "GRP1",
            "PrimaryInsuranceRelationship": "Self",
            "SecondaryInsuranceCompany": "Aetna",
            "SecondaryInsurancePolicyNumber": "XYZ789",
        }

        coverages = exporter.client_to_coverages(client)

        self.assertEqual(len(coverages), 2)
        self.assertEqual(coverages[0]["order"], 1)
        self.assertEqual(coverages[0]["payor"][0]["display"], "Blue Cross")
        self.assertEqual(coverages[0]["subscriberId"], "ABC123")
        self.assertEqual(coverages[0]["beneficiary"]["reference"], "Patient/client-5")
        self.assertEqual(coverages[1]["order"], 2)
        self.assertEqual(coverages[1]["payor"][0]["display"], "Aetna")

    def test_coverages_empty_without_insurance(self):
        self.assertEqual(exporter.client_to_coverages({"ClientId": 9}), [])


class FhirAppointmentTests(unittest.TestCase):
    def test_appointment_maps_status_times_and_participants(self):
        appt = {
            "Id": "appt-1",
            "ClientId": 123,
            "Status": "Confirmed",
            "StartDateIso": "2024-11-13T10:00:00Z",
            "EndDateIso": "2024-11-13T11:00:00Z",
            "Duration": 60,
            "ServiceName": "Initial Consultation",
            "PractitionerName": "Charles Maddix",
        }

        resource = exporter.appointment_to_fhir(appt)

        self.assertEqual(resource["resourceType"], "Appointment")
        self.assertEqual(resource["id"], "appt-1")
        self.assertEqual(resource["status"], "booked")
        self.assertEqual(resource["start"], "2024-11-13T10:00:00Z")
        self.assertEqual(resource["end"], "2024-11-13T11:00:00Z")
        self.assertEqual(resource["minutesDuration"], 60)
        refs = [p["actor"].get("reference") for p in resource["participant"]]
        self.assertIn("Patient/client-123", refs)

    def test_appointment_falls_back_to_unix_times(self):
        appt = {"Id": "a2", "ClientId": 1, "Status": "Missed", "StartDate": 0}
        resource = exporter.appointment_to_fhir(appt)
        self.assertEqual(resource["status"], "noshow")
        self.assertEqual(resource["start"], "1970-01-01T00:00:00+00:00")


class FhirQuestionnaireResponseTests(unittest.TestCase):
    def test_maps_open_and_date_questions(self):
        intake = {
            "Id": "intake-1",
            "ClientId": 123,
            "Status": "Completed",
            "DateSubmitted": 0,
            "QuestionnaireId": "tmpl-1",
            "Questions": [
                {"Id": "q1", "Text": "Full name", "Answer": "John Doe", "QuestionType": "OpenQuestion"},
                {"Id": "q2", "Text": "DOB", "Answer": "1985-05-15", "QuestionType": "DateQuestion"},
            ],
        }

        qr = exporter.intake_to_questionnaire_response(intake)

        self.assertEqual(qr["resourceType"], "QuestionnaireResponse")
        self.assertEqual(qr["status"], "completed")
        self.assertEqual(qr["subject"]["reference"], "Patient/client-123")
        self.assertEqual(qr["authored"], "1970-01-01T00:00:00+00:00")
        items = {item["linkId"]: item for item in qr["item"]}
        self.assertEqual(items["q1"]["answer"][0]["valueString"], "John Doe")
        self.assertEqual(items["q2"]["answer"][0]["valueDate"], "1985-05-15")

    def test_partial_status_maps_to_in_progress(self):
        qr = exporter.intake_to_questionnaire_response(
            {"Id": "i", "ClientId": 1, "Status": "Partial", "Questions": []}
        )
        self.assertEqual(qr["status"], "in-progress")

    def test_matrix_question_produces_nested_items(self):
        intake = {
            "Id": "i3",
            "ClientId": 1,
            "Status": "Completed",
            "Questions": [
                {
                    "Id": "q5",
                    "Text": "History",
                    "QuestionType": "Matrix",
                    "ColumnNames": ["Concern", "Date"],
                    "Rows": [
                        {"Text": "1", "Answers": ["High blood pressure", "2020"]},
                        {"Text": "2", "Answers": ["Diabetes", "2021"]},
                    ],
                }
            ],
        }

        qr = exporter.intake_to_questionnaire_response(intake)
        matrix_item = qr["item"][0]
        self.assertEqual(len(matrix_item["item"]), 2)


class FhirDocumentAndConditionTests(unittest.TestCase):
    def test_note_to_document_reference(self):
        note = {
            "Id": "note-1",
            "ClientId": 123,
            "NoteName": "Follow-up Visit",
            "Status": "locked",
            "Date": 0,
            "PractitionerName": "Charles Maddix",
        }

        doc = exporter.note_to_document_reference(note)

        self.assertEqual(doc["resourceType"], "DocumentReference")
        self.assertEqual(doc["id"], "note-1")
        self.assertEqual(doc["status"], "current")
        self.assertEqual(doc["docStatus"], "final")
        self.assertEqual(doc["subject"]["reference"], "Patient/client-123")
        self.assertEqual(doc["type"]["text"], "Follow-up Visit")
        self.assertEqual(doc["author"][0]["display"], "Charles Maddix")
        self.assertEqual(doc["content"][0]["attachment"]["contentType"], "application/pdf")

    def test_diagnosis_to_condition(self):
        diagnosis = {
            "Code": "F41.1",
            "Description": "Generalized anxiety disorder",
            "Date": "2024-03-15T00:00:00Z",
        }

        condition = exporter.diagnosis_to_condition(diagnosis, "Patient/client-123", 0)

        self.assertEqual(condition["resourceType"], "Condition")
        self.assertEqual(condition["subject"]["reference"], "Patient/client-123")
        coding = condition["code"]["coding"][0]
        self.assertEqual(coding["code"], "F41.1")
        self.assertEqual(coding["system"], "http://hl7.org/fhir/sid/icd-10-cm")
        self.assertEqual(condition["onsetDateTime"], "2024-03-15T00:00:00Z")


class BuildFhirResourcesTests(unittest.TestCase):
    def test_groups_resources_by_type(self):
        clients = [
            {
                "ClientId": 123,
                "FirstName": "John",
                "LastName": "Doe",
                "PrimaryInsuranceCompany": "Blue Cross",
            }
        ]
        appointments = [{"Id": "a1", "ClientId": 123, "Status": "Confirmed", "StartDate": 0}]
        intakes = [{"Id": "i1", "ClientId": 123, "Status": "Completed", "Questions": []}]
        notes = [{"Id": "n1", "ClientId": 123, "NoteName": "Visit", "Date": 0}]
        diagnoses = {"client:123": [{"Code": "F41.1", "Description": "GAD", "Date": "2024-01-01T00:00:00Z"}]}

        resources = exporter.build_fhir_resources(
            clients, appointments, intakes, notes, diagnoses
        )

        self.assertEqual(len(resources["Patient"]), 1)
        self.assertEqual(len(resources["Coverage"]), 1)
        self.assertEqual(len(resources["Appointment"]), 1)
        self.assertEqual(len(resources["QuestionnaireResponse"]), 1)
        self.assertEqual(len(resources["DocumentReference"]), 1)
        self.assertEqual(len(resources["Condition"]), 1)
        self.assertEqual(resources["Condition"][0]["subject"]["reference"], "Patient/client-123")

    def test_write_fhir_ndjson_one_resource_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            resources = {
                "Patient": [{"resourceType": "Patient", "id": "p1"}, {"resourceType": "Patient", "id": "p2"}],
                "Condition": [],
            }

            exporter.write_fhir_ndjson(output_dir, resources)

            patient_file = output_dir / "fhir" / "Patient.ndjson"
            self.assertTrue(patient_file.exists())
            lines = patient_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["id"], "p1")
            self.assertFalse((output_dir / "fhir" / "Condition.ndjson").exists())


class FetchDiagnosesResumableTests(unittest.TestCase):
    def test_caches_diagnoses_per_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            clients = [{"ClientId": 1}, {"ClientId": 2}]
            api = StubAPI(
                {
                    "client/1/diagnoses": [{"Code": "F41.1"}],
                    "client/2/diagnoses": [],
                }
            )

            result = exporter.fetch_diagnoses_resumable(
                api, clients, output_dir=output_dir, log=lambda _m: None
            )

            self.assertEqual(result["client:1"], [{"Code": "F41.1"}])
            self.assertEqual(result["client:2"], [])
            self.assertTrue((output_dir / "_diagnoses" / "client-1.json").exists())

    def test_skips_clients_already_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            diag_dir = output_dir / "_diagnoses"
            diag_dir.mkdir(parents=True)
            (diag_dir / "client-1.json").write_text(json.dumps([{"Code": "cached"}]))
            clients = [{"ClientId": 1}, {"ClientId": 2}]
            api = StubAPI({"client/2/diagnoses": [{"Code": "F32.9"}]})

            result = exporter.fetch_diagnoses_resumable(
                api, clients, output_dir=output_dir, log=lambda _m: None
            )

            self.assertEqual(result["client:1"], [{"Code": "cached"}])
            self.assertEqual([call[0] for call in api.calls], ["client/2/diagnoses"])


class PerformExportFhirTests(unittest.TestCase):
    def test_fhir_mode_writes_ndjson_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "data"

            class FhirStub:
                def __init__(self):
                    self.calls = []

                def get_json(self, path, params=None):
                    self.calls.append((path, dict(params or {})))
                    page = int((params or {}).get("page", 1))
                    if path == "clients":
                        return (
                            [
                                {
                                    "ClientId": 1,
                                    "FirstName": "A",
                                    "LastName": "B",
                                    "PrimaryInsuranceCompany": "BCBS",
                                }
                            ]
                            if page == 1
                            else []
                        )
                    if path == "appointments":
                        return (
                            [{"Id": "a1", "ClientId": 1, "Status": "Confirmed", "StartDate": 0}]
                            if page == 1
                            else []
                        )
                    if path == "intakes/summary":
                        return [{"Id": "i1", "ClientId": 1}] if page == 1 else []
                    if path == "intakes/i1":
                        return {
                            "Id": "i1",
                            "ClientId": 1,
                            "Status": "Completed",
                            "Questions": [],
                            "ConsentForms": [],
                        }
                    if path == "notes":
                        return (
                            [{"Id": "n1", "ClientId": 1, "NoteName": "Visit", "Date": 0}]
                            if page == 1
                            else []
                        )
                    if path == "client/1/diagnoses":
                        return [
                            {"Code": "F41.1", "Description": "GAD", "Date": "2024-01-01T00:00:00Z"}
                        ]
                    raise AssertionError(f"unexpected path {path}")

            api = FhirStub()
            args = exporter.parse_args(
                [
                    "--output-dir",
                    str(output_dir),
                    "--start-date",
                    "2025-01-01",
                    "--end-date",
                    "2025-12-31",
                    "--delay-seconds",
                    "0",
                    "--fhir",
                ]
            )

            result = exporter.perform_export("fake", args, log=lambda _m: None, api=api)

            fhir_dir = output_dir / "fhir"
            for resource_type in (
                "Patient",
                "Coverage",
                "Appointment",
                "QuestionnaireResponse",
                "DocumentReference",
                "Condition",
            ):
                self.assertTrue(
                    (fhir_dir / f"{resource_type}.ndjson").exists(),
                    f"missing {resource_type}.ndjson",
                )

            self.assertTrue(result.metadata["fhirEnabled"])
            self.assertEqual(result.metadata["fhirCounts"]["Patient"], 1)
            self.assertEqual(result.metadata["fhirCounts"]["Condition"], 1)

            patient_line = (fhir_dir / "Patient.ndjson").read_text(encoding="utf-8").strip()
            self.assertEqual(json.loads(patient_line)["resourceType"], "Patient")

    def test_fhir_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "data"

            class PlainStub:
                def get_json(self, path, params=None):
                    page = int((params or {}).get("page", 1))
                    if path == "clients":
                        return [{"ClientId": 1}] if page == 1 else []
                    if path == "appointments":
                        return []
                    if path == "intakes/summary":
                        return []
                    raise AssertionError(f"unexpected path {path}")

            args = exporter.parse_args(
                [
                    "--output-dir",
                    str(output_dir),
                    "--start-date",
                    "2025-01-01",
                    "--end-date",
                    "2025-12-31",
                    "--delay-seconds",
                    "0",
                ]
            )

            result = exporter.perform_export(
                "fake", args, log=lambda _m: None, api=PlainStub()
            )

            self.assertFalse(result.metadata["fhirEnabled"])
            self.assertIsNone(result.metadata["fhirCounts"])
            self.assertFalse((output_dir / "fhir").exists())


if __name__ == "__main__":
    unittest.main()
