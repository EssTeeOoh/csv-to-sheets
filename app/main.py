import os
import re
import glob
import uuid
import json
import errno
import logging
import asyncio
import tempfile
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from app.sheets import (
    initialise_google_services,
    get_cached_services,
    get_google_auth_status,
    create_spreadsheet,
    make_sheet_public,
    upload_data_to_sheet
)
from app.utils import validate_csv_from_disk, stream_csv_from_disk

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APP INSTANCE
# ---------------------------------------------------------------------------
app = FastAPI(
    title="CSV to Google Sheets API",
    description="Upload a CSV file and get back a public Google Sheets URL.",
    version="1.0.0"
)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_MB = 200
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024
CELL_LIMIT = 10_000_000
CHUNK_SIZE = 1024 * 1024
TEMP_CSV_PREFIX = "csv_to_sheets_"

VALID_CSV_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
}

INVALID_TITLE_CHARS = re.compile(r'[\\/*?\[\]]')

# ---------------------------------------------------------------------------
# DISK-BASED JOB STORE
# ---------------------------------------------------------------------------
# Each job is a JSON file: JOBS_DIR/{job_id}.json
# Survives server restarts because disk persists between process restarts.
# Files older than JOB_EXPIRY_HOURS are deleted by the cleanup thread.
JOBS_DIR = os.path.join(tempfile.gettempdir(), "csv_jobs")
jobs_lock = threading.Lock()
JOB_EXPIRY_HOURS = 24

# How often the background cleanup thread runs.
# Every hour is enough -- job files are tiny, they won't fill disk in an hour.
CLEANUP_INTERVAL_SECONDS = 3600  # 1 hour


def _job_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def write_job(job_id: str, data: dict):
    """Creates or replaces a job's JSON file on disk."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    with jobs_lock:
        with open(_job_path(job_id), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def read_job(job_id: str) -> Optional[dict]:
    """Reads a job's JSON file. Returns None if missing or corrupted."""
    path = _job_path(job_id)
    with jobs_lock:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read job file {path}: {e}")
            return None


def update_job(job_id: str, **kwargs):
    """
    Updates specific fields of an existing job file.
    Reads current state, applies updates, writes back.
    Thread-safe via jobs_lock.
    """
    path = _job_path(job_id)
    with jobs_lock:
        if not os.path.exists(path):
            logger.warning(f"update_job called for unknown job {job_id}")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.update(kwargs)
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to update job {job_id}: {e}", exc_info=True)


def cleanup_old_jobs():
    """
    Deletes job files older than JOB_EXPIRY_HOURS.

    Called at startup AND by the background cleanup thread every hour.
    Running continuously means files are deleted promptly regardless of
    whether the server ever restarts -- fixing the accumulation problem
    that affected the startup-only approach.
    """
    if not os.path.exists(JOBS_DIR):
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=JOB_EXPIRY_HOURS)
    deleted = 0
    failed = 0

    for job_file in glob.glob(os.path.join(JOBS_DIR, "*.json")):
        try:
            mtime = os.path.getmtime(job_file)
            file_time = datetime.fromtimestamp(mtime, tz=timezone.utc)
            if file_time < cutoff:
                os.remove(job_file)
                deleted += 1
        except OSError as e:
            logger.warning(f"Could not clean up job file {job_file}: {e}")
            failed += 1

    if deleted > 0:
        logger.info(
            f"Cleanup: deleted {deleted} expired job file(s) "
            f"older than {JOB_EXPIRY_HOURS} hours."
        )
    if failed > 0:
        logger.warning(f"Cleanup: failed to delete {failed} job file(s).")


def run_cleanup_loop():
    """
    Background thread that runs cleanup_old_jobs every hour indefinitely.

    WHY A BACKGROUND THREAD INSTEAD OF STARTUP-ONLY CLEANUP:

    The old approach only cleaned up at server startup. If the server runs
    continuously for days without restarting (possible on paid Render tiers),
    job files from every upload accumulate on disk indefinitely.

    This thread runs independently of the server lifecycle. It wakes up
    every CLEANUP_INTERVAL_SECONDS, deletes expired files, then sleeps again.
    No restarts needed, no cron jobs needed, no external scheduler needed.

    WHY daemon=True:
    A daemon thread is automatically killed when the main process exits.
    Without daemon=True, this thread would keep Python running even after
    the server shuts down -- preventing clean process termination.
    """
    logger.info(
        f"Cleanup thread started. Will run every "
        f"{CLEANUP_INTERVAL_SECONDS // 3600} hour(s)."
    )
    while True:
        # Sleep first, then clean. Startup cleanup runs separately
        # so we don't duplicate work immediately on boot.
        time_module.sleep(CLEANUP_INTERVAL_SECONDS)
        logger.info("Cleanup thread: running scheduled job file cleanup...")
        try:
            cleanup_old_jobs()
        except Exception as e:
            # Never let an exception kill the cleanup thread
            logger.error(f"Cleanup thread error: {e}", exc_info=True)


# Import time here to avoid shadowing the 'time' name from sheets.py
import time as time_module

# ---------------------------------------------------------------------------
# CONCURRENCY LIMITERS
# ---------------------------------------------------------------------------
MAX_CONCURRENT_REQUESTS = 3
MAX_CONCURRENT_BACKGROUNDS = 3
upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
background_semaphore = threading.Semaphore(MAX_CONCURRENT_BACKGROUNDS)


# ---------------------------------------------------------------------------
# STARTUP EVENT
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """
    Runs once at server boot.
    1. Delete expired job files
    2. Delete orphaned CSV temp files
    3. Start the background cleanup thread
    4. Authenticate with Google
    """
    # 1. Clean up expired job files from previous runs
    logger.info("Cleaning up expired job files...")
    cleanup_old_jobs()

    # 2. Clean up orphaned CSV temp files from crashed uploads
    temp_dir = tempfile.gettempdir()
    leftover_files = glob.glob(
        os.path.join(temp_dir, f"{TEMP_CSV_PREFIX}*.csv")
    )
    if leftover_files:
        logger.warning(
            f"Found {len(leftover_files)} orphaned CSV temp file(s). Cleaning up..."
        )
        for path in leftover_files:
            try:
                os.remove(path)
                logger.info(f"Deleted orphaned temp file: {path}")
            except OSError as e:
                logger.warning(f"Could not delete {path}: {e}")
    else:
        logger.info("No orphaned CSV temp files found.")

    # 3. Start the background cleanup thread.
    # daemon=True so it dies automatically when the server process exits.
    cleanup_thread = threading.Thread(
        target=run_cleanup_loop,
        daemon=True,
        name="job-cleanup-thread"
    )
    cleanup_thread.start()
    logger.info("Background cleanup thread started.")

    # 4. Authenticate with Google
    logger.info("Initialising Google API services...")
    initialise_google_services()
    logger.info("Startup complete. Ready to accept requests.")


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def make_safe_sheet_title(filename: str) -> str:
    """Strips characters Google disallows in spreadsheet titles."""
    raw = filename.replace(".csv", "")
    sanitized = INVALID_TITLE_CHARS.sub("", raw).strip()
    return sanitized or "Uploaded CSV"


# ---------------------------------------------------------------------------
# BACKGROUND TASK
# ---------------------------------------------------------------------------

def background_upload(
    job_id: str,
    spreadsheet_id: str,
    temp_file_path: str,
    total_rows: int
):
    """
    Streams CSV from disk into Google Sheets with live progress reporting.

    PROGRESS ACCURACY:
    upload_data_to_sheet now returns the count of rows actually confirmed
    written to Google Sheets. We store this in the job file so that if
    the upload fails partway through, the UI shows:
        "Upload failed after 20,000 of 100,000 rows"
    instead of just "Upload failed" with no context.

    The on_progress callback fires every 1,000 rows (changed from 10,000)
    giving 10x more frequent UI updates with zero extra API calls.
    """
    background_semaphore.acquire()

    try:
        update_job(job_id, status="uploading", rows_uploaded=0)
        sheets_service, _ = get_cached_services()

        def on_progress(rows_uploaded: int, total: int):
            """
            Called every 1,000 rows by upload_data_to_sheet.
            Writes progress to the job file on disk.
            """
            percent = round((rows_uploaded / total) * 100, 1) if total > 0 else 0
            update_job(
                job_id,
                rows_uploaded=rows_uploaded,
                percent_complete=percent
            )
            
            

        rows_generator = stream_csv_from_disk(temp_file_path)

        # upload_data_to_sheet now returns the confirmed uploaded row count.
        # We store this so partial failure messages are accurate.
        rows_confirmed = upload_data_to_sheet(
            sheets_service,
            spreadsheet_id,
            rows_generator,
            total_rows=total_rows,
            on_progress=on_progress
        )

        # rows_confirmed may be less than total_rows if some batches failed.
        if rows_confirmed is not None and rows_confirmed < total_rows:
            # Partial success -- some rows made it, some didn't
            update_job(
                job_id,
                status="failed",
                rows_uploaded=rows_confirmed,
                percent_complete=round((rows_confirmed / total_rows) * 100, 1),
                error=(
                    f"Upload incomplete: {rows_confirmed:,} of "
                    f"{total_rows:,} rows were written. "
                    f"Some batches failed -- check server logs for details."
                )
            )
            logger.warning(
                f"Job {job_id}: partial upload -- "
                f"{rows_confirmed}/{total_rows} rows written."
            )
        else:
            update_job(
                job_id,
                status="complete",
                rows_uploaded=total_rows,
                percent_complete=100.0
            )
            logger.info(f"Job {job_id}: upload complete")

    except Exception as e:
        # Read current rows_uploaded before overwriting status
        # so we preserve how far we got before the exception.
        current = read_job(job_id) or {}
        rows_so_far = current.get("rows_uploaded", 0)
        update_job(
            job_id,
            status="failed",
            error=(
                f"Upload failed after {rows_so_far:,} rows: {str(e)}"
            )
        )
        logger.error(
            f"Job {job_id}: background upload failed: {e}",
            exc_info=True
        )

    finally:
        background_semaphore.release()
        try:
            os.remove(temp_file_path)
            logger.info(f"Job {job_id}: temp file deleted: {temp_file_path}")
        except OSError as e:
            logger.warning(
                f"Job {job_id}: could not delete temp file {temp_file_path}: {e}"
            )


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.get("/")
def root(request: Request):
    """Serves the HTML upload UI."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/privacy")
def privacy_policy(request: Request):
    """Serves the public privacy policy page used by Google OAuth."""
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/terms")
def terms_of_service(request: Request):
    """Serves the public terms of service page used by Google OAuth."""
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/health")
def health_check():
    """Health check -- returns 200 OK if the server is running."""
    return {"status": "ok"}


@app.get("/auth-status")
def auth_status():
    """Reports whether Google auth is ready and whether local reauth is needed."""
    status = get_google_auth_status()
    http_status = 200 if status.get("ready") else 503
    return JSONResponse(status_code=http_status, content=status)


@app.get("/status/{job_id}")
def get_status(job_id: str):
    """
    Returns current upload status for a job_id.

    Clients poll this endpoint every few seconds to track progress.
    Job files persist on disk so this works across server restarts.
    Files are deleted after JOB_EXPIRY_HOURS (24 hours).

    Status values: pending | uploading | complete | failed
    """
    job = read_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Job '{job_id}' not found. "
                f"Job files are kept for {JOB_EXPIRY_HOURS} hours. "
                f"Check your sheet URL directly to see if data is present."
            )
        )

    return JSONResponse(content=job)


@app.post("/upload")
async def upload_csv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    """
    Accepts a CSV, creates a Google Sheet, returns URL and job_id immediately.
    Data uploads in the background. Poll GET /status/{job_id} for progress.
    """

    # Step 1: validate file type
    content_type = file.content_type or ""
    filename = file.filename or ""

    is_valid_type = (
        content_type in VALID_CSV_CONTENT_TYPES or
        filename.lower().endswith(".csv")
    )

    if not is_valid_type:
        raise HTTPException(
            status_code=400,
            detail=f"Only CSV files accepted. Got: '{content_type}'"
        )

    async with upload_semaphore:

        # Step 2: create job file on disk immediately
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        write_job(job_id, {
            "job_id": job_id,
            "status": "pending",
            "spreadsheet_url": None,
            "filename": filename or "unknown",
            "total_rows": 0,
            "rows_uploaded": 0,
            "percent_complete": 0.0,
            "error": None,
            "created_at": now,
            "updated_at": now
        })

        logger.info(f"Job {job_id}: created for '{filename}'")

        # Step 3: write chunks to temp file on disk
        total_size = 0
        temp_file_path = None

        try:
            with tempfile.NamedTemporaryFile(
                delete=False, prefix=TEMP_CSV_PREFIX, suffix=".csv", mode="wb"
            ) as tmp:
                temp_file_path = tmp.name
                logger.info(f"Job {job_id}: writing to {temp_file_path}")

                while True:
                    chunk = await file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > MAX_FILE_SIZE:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"File too large. Maximum is {MAX_FILE_SIZE_MB}MB."
                            )
                        )
                    tmp.write(chunk)

        except HTTPException:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            update_job(job_id, status="failed", error="File too large.")
            raise

        except OSError as e:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            if e.errno == errno.ENOSPC:
                update_job(job_id, status="failed", error="Server storage full.")
                logger.error("Disk full.", exc_info=True)
                raise HTTPException(
                    status_code=507,
                    detail="Server storage is full. Please try again later."
                )
            update_job(job_id, status="failed", error=str(e))
            logger.error(f"OS error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to process upload.")

        except Exception as e:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            update_job(job_id, status="failed", error=str(e))
            logger.error(f"Unexpected error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to process upload.")

        if total_size == 0:
            os.remove(temp_file_path)
            update_job(job_id, status="failed", error="File was empty.")
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")

        # Step 4: stream-validate from disk
        is_valid, error, total_rows, total_cols = validate_csv_from_disk(
            temp_file_path
        )
        if not is_valid:
            os.remove(temp_file_path)
            update_job(job_id, status="failed", error=error)
            raise HTTPException(status_code=422, detail=error)

        update_job(job_id, total_rows=total_rows)

        # Step 5: check cell limit
        total_cells = total_rows * total_cols
        if total_cells > CELL_LIMIT:
            os.remove(temp_file_path)
            error_msg = (
                f"CSV too large: {total_rows:,} rows x {total_cols} cols "
                f"= {total_cells:,} cells. Limit is 10,000,000."
            )
            update_job(job_id, status="failed", error=error_msg)
            raise HTTPException(status_code=422, detail=error_msg)

        # Steps 6 & 7: create sheet and make it public
        try:
            sheets_service, drive_service = get_cached_services()
            sheet_title = make_safe_sheet_title(filename)

            spreadsheet_id = create_spreadsheet(
                sheets_service,
                title=sheet_title,
                rows=total_rows,
                cols=total_cols
            )
            make_sheet_public(drive_service, spreadsheet_id)
            logger.info(
                f"Job {job_id}: sheet '{sheet_title}' created ({spreadsheet_id})"
            )
        except Exception as e:
            os.remove(temp_file_path)
            update_job(job_id, status="failed", error=str(e))
            logger.error(f"Job {job_id}: failed to create sheet: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create Google Sheet: {str(e)}"
            )

        sheet_url = (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        )
        update_job(job_id, spreadsheet_url=sheet_url)

        # Step 8: schedule background upload
        background_tasks.add_task(
            background_upload,
            job_id,
            spreadsheet_id,
            temp_file_path,
            total_rows
        )

        return JSONResponse(
            status_code=202,
            content={
                "message": "Sheet created. Data is uploading in the background.",
                "spreadsheet_url": sheet_url,
                "job_id": job_id,
                "status_url": f"/status/{job_id}",
                "rows_queued": total_rows,
                "columns": total_cols,
                "total_cells": total_cells,
                "filename": filename or "unknown"
            }
        )
