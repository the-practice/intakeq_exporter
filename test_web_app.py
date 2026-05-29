from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import main as web
from app.main import app


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("EXPORTER_ADMIN_PASSWORD", None)

    def test_healthz(self):
        client = TestClient(app)

        response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_index_renders_export_form(self):
        client = TestClient(app)

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("IntakeQ Exporter", response.text)
        self.assertIn("All clients", response.text)
        self.assertIn("Start export", response.text)

    def test_admin_password_enables_basic_auth(self):
        os.environ["EXPORTER_ADMIN_PASSWORD"] = "secret"
        client = TestClient(app)

        unauthenticated = client.get("/")
        authenticated = client.get("/", auth=("admin", "secret"))

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)


class ResumeEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("EXPORTER_ADMIN_PASSWORD", None)
        with web.jobs_lock:
            web.jobs.clear()

    def _seed_failed_job(self, job_id: str) -> web.ExportJob:
        args = argparse.Namespace(
            output_dir=Path("/tmp/does-not-matter") / job_id / "data",
            start_date="2025-01-01",
            end_date="2025-12-31",
            submitted_only=False,
            client_search=None,
            client_created_start=None,
            client_created_end=None,
            client_updated_start=None,
            client_updated_end=None,
            deleted_clients_only=False,
            download_pdfs=False,
            max_pages=None,
            max_intakes=None,
            delay_seconds=0.0,
            base_url=web.BASE_URL,
        )
        job = web.ExportJob(job_id, args.output_dir)
        job.status = "failed"
        job.error = "boom"
        job.api_key = "fake-key"
        job.args = args
        with web.jobs_lock:
            web.jobs[job_id] = job
        return job

    def test_resume_404_when_job_unknown(self):
        client = TestClient(app)
        response = client.post("/jobs/unknown/resume", follow_redirects=False)
        self.assertEqual(response.status_code, 404)

    def test_resume_409_when_job_still_running(self):
        job = self._seed_failed_job("job-running")
        job.status = "running"
        client = TestClient(app)
        response = client.post(f"/jobs/{job.id}/resume", follow_redirects=False)
        self.assertEqual(response.status_code, 409)

    def test_resume_restarts_failed_job_with_same_output_dir(self):
        job = self._seed_failed_job("job-failed")
        captured = {}

        def fake_thread_start(job_id, api_key, args):
            captured["job_id"] = job_id
            captured["api_key"] = api_key
            captured["output_dir"] = args.output_dir

        with mock.patch.object(web, "start_export_thread", side_effect=fake_thread_start):
            client = TestClient(app)
            response = client.post(f"/jobs/{job.id}/resume", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job.id}")
        self.assertEqual(captured["job_id"], job.id)
        self.assertEqual(captured["api_key"], "fake-key")
        self.assertEqual(captured["output_dir"], job.args.output_dir)

        with web.jobs_lock:
            self.assertEqual(web.jobs[job.id].status, "queued")
            self.assertIsNone(web.jobs[job.id].error)


class RecoveryHelperTests(unittest.TestCase):
    def test_serialize_args_stringifies_paths_and_is_json_safe(self):
        args = argparse.Namespace(
            output_dir=Path("/tmp/x") / "data",
            start_date="2022-01-01",
            fhir=True,
            max_pages=None,
        )

        data = web.serialize_args(args)

        self.assertEqual(data["output_dir"], str(Path("/tmp/x") / "data"))
        self.assertTrue(data["fhir"])
        self.assertIsNone(data["max_pages"])
        json.dumps(data)  # must not raise

    def test_valid_job_id_rejects_unsafe_values(self):
        self.assertTrue(web.valid_job_id("abc123DEF_-"))
        self.assertFalse(web.valid_job_id(""))
        self.assertFalse(web.valid_job_id("../etc"))
        self.assertFalse(web.valid_job_id("a/b"))
        self.assertFalse(web.valid_job_id("with space"))

    def test_persist_and_load_job_record_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPORT_ROOT"] = tmp
            try:
                args = argparse.Namespace(
                    output_dir=Path(tmp) / "jid" / "data",
                    start_date="2022-01-01",
                    end_date="2100-12-31",
                    fhir=True,
                )
                job = web.ExportJob("jid", args.output_dir)
                job.args = args
                job.status = "failed"

                web.persist_job_record(job)
                record = web.load_job_record("jid")

                self.assertEqual(record["id"], "jid")
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["args"]["start_date"], "2022-01-01")
                self.assertTrue(record["args"]["fhir"])
            finally:
                os.environ.pop("EXPORT_ROOT", None)

    def test_load_job_record_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPORT_ROOT"] = tmp
            try:
                self.assertIsNone(web.load_job_record("nope"))
            finally:
                os.environ.pop("EXPORT_ROOT", None)

    def test_list_recoverable_jobs_finds_export_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPORT_ROOT"] = tmp
            try:
                (Path(tmp) / "job-aaa" / "data").mkdir(parents=True)
                (Path(tmp) / "job-bbb" / "data").mkdir(parents=True)
                (Path(tmp) / "not-an-export").mkdir()  # no data/ subdir

                found = {job["id"] for job in web.list_recoverable_jobs()}

                self.assertEqual(found, {"job-aaa", "job-bbb"})
            finally:
                os.environ.pop("EXPORT_ROOT", None)


class RecoveryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("EXPORTER_ADMIN_PASSWORD", None)
        with web.jobs_lock:
            web.jobs.clear()

    def test_start_export_resumes_existing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPORT_ROOT"] = tmp
            try:
                existing_id = "abc123def456"
                (Path(tmp) / existing_id / "data").mkdir(parents=True)
                captured: dict = {}

                def fake_thread_start(job_id, api_key, args):
                    captured["job_id"] = job_id
                    captured["api_key"] = api_key
                    captured["output_dir"] = args.output_dir

                with mock.patch.object(web, "start_export_thread", side_effect=fake_thread_start):
                    client = TestClient(app)
                    response = client.post(
                        "/exports",
                        data={
                            "api_key": "fake-key",
                            "resume_job_id": existing_id,
                            "start_date": "2022-01-01",
                            "end_date": "2100-12-31",
                            "client_scope": "all",
                            "fhir": "on",
                        },
                        follow_redirects=False,
                    )

                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], f"/jobs/{existing_id}")
                self.assertEqual(captured["job_id"], existing_id)
                self.assertEqual(
                    captured["output_dir"], web.export_root() / existing_id / "data"
                )
                self.assertTrue((Path(tmp) / existing_id / "job.json").exists())
            finally:
                os.environ.pop("EXPORT_ROOT", None)

    def test_resume_unknown_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPORT_ROOT"] = tmp
            try:
                with mock.patch.object(web, "start_export_thread") as thread_mock:
                    client = TestClient(app)
                    response = client.post(
                        "/exports",
                        data={
                            "api_key": "fake-key",
                            "resume_job_id": "missing999",
                            "start_date": "2022-01-01",
                            "end_date": "2100-12-31",
                            "client_scope": "all",
                        },
                        follow_redirects=False,
                    )

                self.assertEqual(response.status_code, 422)
                thread_mock.assert_not_called()
            finally:
                os.environ.pop("EXPORT_ROOT", None)

    def test_resume_rejects_unsafe_job_id(self):
        with mock.patch.object(web, "start_export_thread") as thread_mock:
            client = TestClient(app)
            response = client.post(
                "/exports",
                data={
                    "api_key": "fake-key",
                    "resume_job_id": "../secrets",
                    "start_date": "2022-01-01",
                    "end_date": "2100-12-31",
                    "client_scope": "all",
                },
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 422)
        thread_mock.assert_not_called()

    def test_recover_page_lists_existing_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["EXPORT_ROOT"] = tmp
            try:
                (Path(tmp) / "job-xyz789" / "data").mkdir(parents=True)
                client = TestClient(app)

                response = client.get("/recover")

                self.assertEqual(response.status_code, 200)
                self.assertIn("job-xyz789", response.text)
            finally:
                os.environ.pop("EXPORT_ROOT", None)


if __name__ == "__main__":
    unittest.main()

