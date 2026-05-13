import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from glob import glob
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as app_main


class DummyThread:
    def __init__(self, *args, **kwargs):
        self.started = False

    def start(self):
        self.started = True


@contextmanager
def client_without_startup():
    startup_handlers = list(app_main.app.router.on_startup)
    try:
        app_main.app.router.on_startup.clear()
        with TestClient(app_main.app) as client:
            yield client
    finally:
        app_main.app.router.on_startup[:] = startup_handlers


class AuthStatusEndpointTests(unittest.TestCase):
    def test_auth_status_returns_200_when_ready(self):
        with patch.object(app_main, "get_google_auth_status", return_value={"ready": True, "mode": "local"}):
            with client_without_startup() as client:
                response = client.get("/auth-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "local")

    def test_auth_status_returns_503_when_not_ready(self):
        payload = {"ready": False, "reauth_required": True, "last_error": "bad token"}
        with patch.object(app_main, "get_google_auth_status", return_value=payload):
            with client_without_startup() as client:
                response = client.get("/auth-status")

        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["reauth_required"])


class StartupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_cleanup_only_deletes_app_owned_temp_csvs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            owned = os.path.join(temp_dir, f"{app_main.TEMP_CSV_PREFIX}owned.csv")
            foreign = os.path.join(temp_dir, "tmpforeign.csv")

            with open(owned, "w", encoding="utf-8") as f:
                f.write("owned")
            with open(foreign, "w", encoding="utf-8") as f:
                f.write("foreign")

            with patch.object(app_main, "cleanup_old_jobs", return_value=None), \
                 patch.object(app_main, "initialise_google_services", return_value=None), \
                 patch.object(app_main.tempfile, "gettempdir", return_value=temp_dir), \
                 patch.object(app_main.threading, "Thread", DummyThread):
                await app_main.startup_event()

            self.assertFalse(os.path.exists(owned))
            self.assertTrue(os.path.exists(foreign))


class UploadAccountingTests(unittest.TestCase):
    def test_upload_response_and_job_store_use_same_total_rows(self):
        csv_content = "name,age\nAda,35\nGrace,40\n"

        def cleanup_background_task(_job_id, _spreadsheet_id, temp_file_path, _total_rows):
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

        with tempfile.TemporaryDirectory() as jobs_dir:
            with patch.object(app_main, "JOBS_DIR", jobs_dir), \
                 patch.object(app_main, "get_cached_services", return_value=("sheets", "drive")), \
                 patch.object(app_main, "create_spreadsheet", return_value="sheet123"), \
                 patch.object(app_main, "make_sheet_public", return_value=None), \
                 patch.object(app_main, "background_upload", side_effect=cleanup_background_task):
                with client_without_startup() as client:
                    response = client.post(
                        "/upload",
                        files={"file": ("people.csv", csv_content, "text/csv")}
                    )

            self.assertEqual(response.status_code, 202)
            payload = response.json()
            self.assertEqual(payload["rows_queued"], 3)

            job_files = glob(os.path.join(jobs_dir, "*.json"))
            self.assertEqual(len(job_files), 1)
            job_path = job_files[0]
            with open(job_path, "r", encoding="utf-8") as f:
                job_data = json.load(f)

            self.assertEqual(job_data["job_id"], payload["job_id"])
            self.assertEqual(job_data["total_rows"], payload["rows_queued"])
            self.assertEqual(job_data["filename"], "people.csv")


if __name__ == "__main__":
    unittest.main()
