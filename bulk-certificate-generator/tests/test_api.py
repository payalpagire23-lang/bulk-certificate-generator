"""Tests. Plain unittest, so both `python -m unittest` and `pytest` work.

Most tests use ASYNC_PROCESSING=False so results are deterministic (job is finished when POST returns).
One test uses the real background thread pool.
"""
import io
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from app import create_app, db, generator, service


def payload(**overrides):
    data = {
        "event_name": "Python Bootcamp",
        "issuer": "Tech Academy",
        "recipients": [
            {"name": "Rahul Patil", "email": "rahul@example.com", "course": "Backend"},
            {"name": "Sneha Joshi", "email": "sneha@example.com"},
            {"name": "Amit Deshmukh", "email": "amit@example.com"},
        ],
    }
    data.update(overrides)
    return data


class BaseTest(unittest.TestCase):
    ASYNC = False
    MAX_RECIPIENTS = 1000

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = {
            "DATABASE": os.path.join(self.tmp, "test.db"),
            "STORAGE_DIR": os.path.join(self.tmp, "storage"),
            "ASYNC_PROCESSING": self.ASYNC,
            "MAX_RECIPIENTS": self.MAX_RECIPIENTS,
            "TESTING": True,
        }
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        executor = self.app.extensions.get("executor")
        if executor:
            executor.shutdown(wait=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post_job(self, body=None):
        return self.client.post("/api/jobs", json=body if body is not None else payload())


# --------------------------------------------------------------------------- 1. creating a job
class CreateJobTests(BaseTest):
    def test_create_job_returns_job_with_counts(self):
        res = self.post_job()
        self.assertEqual(res.status_code, 201)  # sync mode -> already finished
        body = res.get_json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["successful"], 3)
        self.assertEqual(body["status"], "completed")
        self.assertIn("status_url", body)

    def test_job_is_persisted_and_listed(self):
        job_id = self.post_job().get_json()["id"]
        jobs = self.client.get("/api/jobs").get_json()["jobs"]
        self.assertEqual([j["id"] for j in jobs], [job_id])

    def test_async_mode_returns_202_and_finishes_in_background(self):
        # Separate app with the real thread pool.
        cfg = dict(self.config, DATABASE=os.path.join(self.tmp, "async.db"), ASYNC_PROCESSING=True)
        app = create_app(cfg)
        self.addCleanup(app.extensions["executor"].shutdown, wait=True)
        client = app.test_client()
        res = client.post("/api/jobs", json=payload())
        self.assertEqual(res.status_code, 202)
        job_id = res.get_json()["id"]
        for _ in range(100):
            body = client.get(f"/api/jobs/{job_id}").get_json()
            if body["status"] == "completed":
                break
            time.sleep(0.1)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["successful"], 3)

    def test_unfinished_job_is_resumed_after_restart(self):
        conn = db.connect(self.config["DATABASE"])
        job_id = service.create_job(conn, {
            "event_name": "E", "title": "T", "issuer": "", "issue_date": "2026-01-01",
            "recipients": [{"name": "A B", "email": "a@b.com"}]})
        conn.close()  # job is 'pending' (server "crashed" before processing)
        cfg = dict(self.config, ASYNC_PROCESSING=True)
        app = create_app(cfg)  # startup recovery
        self.addCleanup(app.extensions["executor"].shutdown, wait=True)
        client = app.test_client()
        for _ in range(100):
            body = client.get(f"/api/jobs/{job_id}").get_json()
            if body["status"] == "completed":
                break
            time.sleep(0.1)
        self.assertEqual(body["status"], "completed")


# --------------------------------------------------------------------------- 2. input validation
class ValidationTests(BaseTest):
    MAX_RECIPIENTS = 10

    def test_non_json_body_rejected(self):
        res = self.client.post("/api/jobs", data="hello", content_type="text/plain")
        self.assertEqual(res.status_code, 400)

    def test_missing_event_name_rejected(self):
        body = payload()
        del body["event_name"]
        res = self.post_job(body)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(any("event_name" in d for d in res.get_json()["details"]))

    def test_empty_or_missing_recipients_rejected(self):
        self.assertEqual(self.post_job(payload(recipients=[])).status_code, 400)
        body = payload()
        del body["recipients"]
        self.assertEqual(self.post_job(body).status_code, 400)

    def test_too_many_recipients_rejected(self):
        many = [{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(11)]
        res = self.post_job(payload(recipients=many))
        self.assertEqual(res.status_code, 400)

    def test_bad_issue_date_rejected(self):
        self.assertEqual(self.post_job(payload(issue_date="09/10/2026")).status_code, 400)

    def test_rejected_request_creates_no_job(self):
        self.post_job(payload(recipients=[]))
        self.assertEqual(self.client.get("/api/jobs").get_json()["jobs"], [])

    def test_invalid_recipients_are_failed_not_fatal(self):
        recipients = [
            {"name": "Good One", "email": "good@example.com"},
            {"name": "", "email": "noname@example.com"},
            {"name": "Bad Email", "email": "nope"},
            {"name": "Good One Again", "email": "GOOD@example.com"},  # duplicate (case-insensitive)
            "not-an-object",
            {"name": "Fine Person", "email": "fine@example.com"},
        ]
        res = self.post_job(payload(recipients=recipients))
        body = res.get_json()
        self.assertEqual(body["total"], 6)
        self.assertEqual(body["successful"], 2)
        self.assertEqual(body["failed"], 4)
        self.assertEqual(body["status"], "completed_with_errors")
        self.assertEqual([f["index"] for f in body["failures"]], [1, 2, 3, 4])
        self.assertIn("name", body["failures"][0]["error"])
        self.assertIn("email", body["failures"][1]["error"])
        self.assertIn("duplicate", body["failures"][2]["error"])

    def test_all_recipients_invalid_marks_job_failed(self):
        res = self.post_job(payload(recipients=[{"name": "", "email": "x"}]))
        self.assertEqual(res.get_json()["status"], "failed")


# --------------------------------------------------------------------------- 3. certificate generation
class GenerationTests(BaseTest):
    def test_pdf_is_created_on_disk(self):
        job_id = self.post_job().get_json()["id"]
        certs = self.client.get(f"/api/jobs/{job_id}/certificates").get_json()["certificates"]
        self.assertEqual(len(certs), 3)
        folder = os.path.join(self.config["STORAGE_DIR"], job_id)
        files = os.listdir(folder)
        self.assertEqual(len(files), 3)
        for name in files:
            with open(os.path.join(folder, name), "rb") as f:
                self.assertEqual(f.read(5), b"%PDF-")

    def test_generator_contains_recipient_name(self):
        path = os.path.join(self.tmp, "one.pdf")
        generator.generate_certificate(
            {"name": "Rahul Patil", "email": "r@x.com", "course": "Backend", "event_name": "Bootcamp",
             "title": "Certificate of Completion", "issuer": "Academy", "issue_date": "2026-10-09"}, path)
        with open(path, "rb") as f:
            self.assertTrue(f.read(5) == b"%PDF-")
        self.assertGreater(os.path.getsize(path), 500)

    def test_very_long_name_does_not_crash(self):
        long_name = "A" * 100
        res = self.post_job(payload(recipients=[{"name": long_name, "email": "long@example.com"}]))
        self.assertEqual(res.get_json()["status"], "completed")

    def test_unsupported_characters_fail_only_that_certificate(self):
        res = self.post_job(payload(recipients=[
            {"name": "राहुल पाटील", "email": "marathi@example.com"},
            {"name": "Rahul Patil", "email": "rahul@example.com"}]))
        body = res.get_json()
        self.assertEqual((body["successful"], body["failed"]), (1, 1))
        self.assertIn("CERT_FONT_PATH", body["failures"][0]["error"])

    def test_no_temp_files_left_behind(self):
        job_id = self.post_job().get_json()["id"]
        files = os.listdir(os.path.join(self.config["STORAGE_DIR"], job_id))
        self.assertFalse([f for f in files if f.endswith(".tmp")])


# --------------------------------------------------------------------------- 4. job status / progress
class StatusTests(BaseTest):
    def test_pending_job_shows_zero_progress_then_100_after_processing(self):
        conn = db.connect(self.config["DATABASE"])
        meta = {"event_name": "E", "title": "T", "issuer": "", "issue_date": "2026-01-01",
                "recipients": [{"name": "A One", "email": "a@x.com"}, {"name": "B Two", "email": "b@x.com"}]}
        job_id = service.create_job(conn, meta)
        conn.close()

        body = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["pending"], 2)
        self.assertEqual(body["progress_percent"], 0.0)

        service.process_job(self.config, job_id)

        body = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["progress_percent"], 100.0)
        self.assertIsNotNone(body["completed_at"])

    def test_partial_progress_is_reported(self):
        """Progress is committed per certificate, so a half-finished job reports e.g. 50%."""
        conn = db.connect(self.config["DATABASE"])
        meta = {"event_name": "E", "title": "T", "issuer": "", "issue_date": "2026-01-01",
                "recipients": [{"name": "A One", "email": "a@x.com"}, {"name": "B Two", "email": "b@x.com"}]}
        job_id = service.create_job(conn, meta)
        conn.execute("UPDATE certificates SET status='success' WHERE job_id=? AND idx=0", (job_id,))
        conn.commit()
        conn.close()
        body = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(body["progress_percent"], 50.0)

    def test_unknown_job_returns_404(self):
        self.assertEqual(self.client.get("/api/jobs/does-not-exist").status_code, 404)


# --------------------------------------------------------------------------- 5. single certificate failure
class FailureIsolationTests(BaseTest):
    def test_one_generation_error_does_not_stop_others(self):
        real = generator.generate_certificate

        def flaky(data, path, font_path=""):
            if data["name"] == "Sneha Joshi":
                raise RuntimeError("disk exploded")
            return real(data, path, font_path)

        with mock.patch.object(generator, "generate_certificate", side_effect=flaky):
            body = self.post_job().get_json()

        self.assertEqual(body["status"], "completed_with_errors")
        self.assertEqual(body["successful"], 2)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(body["failures"][0]["email"], "sneha@example.com")
        self.assertIn("disk exploded", body["failures"][0]["error"])

        failed = self.client.get(f"/api/jobs/{body['id']}/certificates?status=failed").get_json()["certificates"]
        ok = self.client.get(f"/api/jobs/{body['id']}/certificates?status=success").get_json()["certificates"]
        self.assertEqual((len(failed), len(ok)), (1, 2))

    def test_every_generation_failing_marks_job_failed(self):
        with mock.patch.object(generator, "generate_certificate", side_effect=RuntimeError("boom")):
            body = self.post_job().get_json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["failed"], 3)


# --------------------------------------------------------------------------- 6. retrieving certificates
class RetrievalTests(BaseTest):
    def setUp(self):
        super().setUp()
        recipients = [
            {"name": "Rahul Patil", "email": "rahul@example.com"},
            {"name": "", "email": "bad@example.com"},
        ]
        self.job = self.post_job(payload(recipients=recipients)).get_json()
        self.certs = self.client.get(f"/api/jobs/{self.job['id']}/certificates").get_json()["certificates"]

    def test_list_contains_success_and_failed(self):
        self.assertEqual([c["status"] for c in self.certs], ["success", "failed"])
        self.assertIn("download_url", self.certs[0])
        self.assertNotIn("download_url", self.certs[1])

    def test_download_single_certificate_pdf(self):
        res = self.client.get(self.certs[0]["download_url"])
        self.addCleanup(res.close)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "application/pdf")
        self.assertTrue(res.data.startswith(b"%PDF-"))

    def test_download_failed_certificate_returns_409(self):
        res = self.client.get(f"/api/certificates/{self.certs[1]['id']}/download")
        self.assertEqual(res.status_code, 409)

    def test_download_unknown_certificate_returns_404(self):
        self.assertEqual(self.client.get("/api/certificates/nope/download").status_code, 404)

    def test_download_all_as_zip_contains_only_successful(self):
        res = self.client.get(f"/api/jobs/{self.job['id']}/download")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "application/zip")
        with zipfile.ZipFile(io.BytesIO(res.data)) as zf:
            self.assertEqual(len(zf.namelist()), 1)
            self.assertTrue(zf.namelist()[0].endswith(".pdf"))

    def test_zip_not_available_when_nothing_generated(self):
        job = self.post_job(payload(recipients=[{"name": "", "email": "x"}])).get_json()
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/download").status_code, 409)

    def test_invalid_status_filter_rejected(self):
        res = self.client.get(f"/api/jobs/{self.job['id']}/certificates?status=weird")
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
