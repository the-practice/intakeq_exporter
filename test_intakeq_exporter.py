from __future__ import annotations

import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
