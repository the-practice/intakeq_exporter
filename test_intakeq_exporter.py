from __future__ import annotations

import unittest

import intakeq_exporter as exporter


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


if __name__ == "__main__":
    unittest.main()
