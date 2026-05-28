from __future__ import annotations

import argparse
import os
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


if __name__ == "__main__":
    unittest.main()

