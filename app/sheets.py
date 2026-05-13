import os
import json
import base64
import time
import logging
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from typing import Generator, Union, Callable, Optional

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

_sheets_service = None
_drive_service = None
_cached_creds = None
_auth_state = {
    "mode": "unknown",
    "ready": False,
    "expiry": None,
    "expired": None,
    "has_refresh_token": False,
    "reauth_required": False,
    "token_source": None,
    "last_error": None,
}


def _set_auth_state(**kwargs):
    """Updates the cached auth status exposed by the health endpoint."""
    _auth_state.update(kwargs)


def _serialise_expiry(creds: Optional[Credentials]) -> Optional[str]:
    """Returns the credential expiry as ISO text for logs and JSON responses."""
    if creds is None or creds.expiry is None:
        return None
    return creds.expiry.isoformat()


def _run_local_oauth_flow() -> Credentials:
    """Runs the browser-based OAuth flow and saves a fresh local token.json."""
    oauth_path = os.getenv("OAUTH_CREDENTIALS_PATH", "oauth_credentials.json")
    if not os.path.exists(oauth_path):
        raise FileNotFoundError(
            f"oauth_credentials.json not found at '{oauth_path}'."
        )

    logger.info("Opening browser for Google OAuth authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(oauth_path, SCOPES)
    creds = flow.run_local_server(port=8080)

    token_path = os.getenv("TOKEN_PATH", "token.json")
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    logger.info(f"Authorization complete. token.json saved. Expiry: {creds.expiry}")
    return creds


def initialise_google_services():
    """
    Authenticates with Google ONCE at server startup and caches the clients.
    LOCAL mode: reads token.json, opens browser on first run.
    HOSTED mode: reads TOKEN_JSON_B64 env var (base64-encoded token.json).
    """
    global _sheets_service, _drive_service, _cached_creds

    creds = None
    token_json_b64 = os.getenv("TOKEN_JSON_B64")
    _set_auth_state(
        ready=False,
        expiry=None,
        expired=None,
        has_refresh_token=False,
        reauth_required=False,
        token_source=None,
        last_error=None,
    )

    if token_json_b64:
        _set_auth_state(mode="hosted", token_source="TOKEN_JSON_B64")
        logger.info("Auth mode: hosted (TOKEN_JSON_B64 environment variable)")
        try:
            token_data = base64.b64decode(token_json_b64).decode()
            creds = Credentials.from_authorized_user_info(
                json.loads(token_data), SCOPES
            )
            logger.info(f"Hosted credentials loaded. Token expiry: {creds.expiry}")
        except Exception as e:
            _set_auth_state(
                ready=False,
                reauth_required=True,
                last_error=f"Failed to decode TOKEN_JSON_B64: {e}"
            )
            logger.error(
                f"Failed to decode TOKEN_JSON_B64: {e}.",
                exc_info=True
            )
            return
    else:
        token_path = os.getenv("TOKEN_PATH", "token.json")
        _set_auth_state(mode="local", token_source=token_path)
        if os.path.exists(token_path):
            logger.info(f"Local mode: loading credentials from {token_path}")
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            logger.info(f"Local credentials loaded. Token expiry: {creds.expiry}")
        else:
            _set_auth_state(
                ready=False,
                reauth_required=True,
                last_error=f"No token.json found at '{token_path}'."
            )
            logger.warning(f"No token.json found at '{token_path}'. Will open browser OAuth flow...")

    if creds:
        _set_auth_state(
            expiry=_serialise_expiry(creds),
            expired=creds.expired,
            has_refresh_token=bool(creds.refresh_token),
        )

    if creds and creds.expired and creds.refresh_token:
        logger.warning(f"Access token expired at {creds.expiry}. Refreshing...")
        try:
            creds.refresh(Request())
            _set_auth_state(
                expiry=_serialise_expiry(creds),
                expired=creds.expired,
                has_refresh_token=bool(creds.refresh_token),
                reauth_required=False,
                last_error=None,
            )
            logger.info(f"Token refreshed. New expiry: {creds.expiry}")
            if not token_json_b64:
                token_path = os.getenv("TOKEN_PATH", "token.json")
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
                logger.info(f"Refreshed token saved to {token_path}")
        except Exception as e:
            _set_auth_state(
                ready=False,
                reauth_required=True,
                last_error=str(e)
            )
            logger.error(
                f"Token refresh FAILED: {e}. "
                f"Fix: run locally, complete OAuth flow, re-encode token.json, "
                f"update TOKEN_JSON_B64 on server.",
                exc_info=True
            )
            if token_json_b64:
                raise RuntimeError(
                    "Hosted Google credentials could not be refreshed. "
                    "Regenerate token.json locally and update TOKEN_JSON_B64."
                ) from e

            logger.warning(
                "Local token refresh failed. Attempting interactive OAuth "
                "reauthorization to regenerate token.json."
            )
            creds = None

    if not creds or not creds.valid:
        if token_json_b64:
            _set_auth_state(
                ready=False,
                reauth_required=True,
                last_error="Hosted credentials invalid."
            )
            logger.error(
                "Hosted credentials invalid. Regenerate token.json locally "
                "and update TOKEN_JSON_B64."
            )
            raise RuntimeError(
                "Google credentials are invalid on the hosted server. "
                "Run locally to regenerate token.json, encode it with "
                "'base64 token.json', and update TOKEN_JSON_B64."
            )

        creds = _run_local_oauth_flow()
        _set_auth_state(
            expiry=_serialise_expiry(creds),
            expired=creds.expired,
            has_refresh_token=bool(creds.refresh_token),
            reauth_required=False,
            last_error=None,
        )

    _cached_creds = creds
    _sheets_service = build("sheets", "v4", credentials=creds)
    _drive_service = build("drive", "v3", credentials=creds)
    _set_auth_state(
        ready=True,
        expiry=_serialise_expiry(creds),
        expired=creds.expired,
        has_refresh_token=bool(creds.refresh_token),
        reauth_required=False,
        last_error=None,
    )
    logger.info("Google Sheets and Drive services initialised and cached.")
    logger.info(
        "Google auth ready. mode=%s expiry=%s refresh_token=%s",
        _auth_state["mode"],
        _auth_state["expiry"],
        _auth_state["has_refresh_token"]
    )


def get_cached_services():
    """Returns the cached API clients. Raises if auth failed at startup."""
    if _sheets_service is None or _drive_service is None:
        raise RuntimeError(
            "Google services failed to initialise at startup. "
            "Check server logs. Fix: regenerate token.json and update TOKEN_JSON_B64."
        )
    return _sheets_service, _drive_service


def get_google_auth_status() -> dict:
    """Returns the current Google auth health information for status checks."""
    status = dict(_auth_state)
    status["services_cached"] = (
        _sheets_service is not None and _drive_service is not None
    )
    status["credentials_cached"] = _cached_creds is not None
    return status


def create_spreadsheet(sheets_service, title: str, rows: int = 1000, cols: int = 26) -> str:
    """
    Creates a new Google Spreadsheet with exact grid dimensions.
    Creating with exact dimensions is one atomic API call -- no resize needed.
    """
    body = {
        "properties": {"title": title},
        "sheets": [{
            "properties": {
                "title": "Sheet1",
                "gridProperties": {"rowCount": rows, "columnCount": cols}
            }
        }]
    }
    result = sheets_service.spreadsheets().create(
        body=body,
        fields="spreadsheetId"
    ).execute()
    return result["spreadsheetId"]


def make_sheet_public(drive_service, spreadsheet_id: str):
    """
    Makes the spreadsheet viewable by anyone with the link.
    Uses Drive API because permissions are a filesystem concern, not a data concern.
    """
    drive_service.permissions().create(
        fileId=spreadsheet_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()
    logger.info(f"Sheet {spreadsheet_id} is now public (view only)")


def upload_batch_with_retry(sheets_service, spreadsheet_id: str, batch: list,
                             range_name: str, max_retries: int = 3):
    """
    Uploads one batch with exponential backoff retry.
    Waits 2s, 4s, 8s between attempts before giving up.
    """
    for attempt in range(max_retries):
        try:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="RAW",
                body={"values": batch}
            ).execute()
            return
        except (HttpError, Exception) as e:
            if attempt == max_retries - 1:
                raise RuntimeError(
                    f"Batch at {range_name} failed after {max_retries} attempts: {e}"
                )
            wait = 2 ** (attempt + 1)
            logger.warning(
                f"Batch {range_name} failed (attempt {attempt + 1}/{max_retries}), "
                f"retrying in {wait}s. Error: {e}"
            )
            time.sleep(wait)


def upload_data_to_sheet(
    sheets_service,
    spreadsheet_id: str,
    data: Union[Generator, list],
    total_rows: int = 0,
    on_progress: Optional[Callable[[int, int], None]] = None
):
    """
    Streams data into the spreadsheet in batches of 10,000 rows.

    PROGRESS REPORTING -- HOW IT WORKS:

    The old approach called on_progress only after a full batch of 10,000
    rows was uploaded. That meant the progress bar froze for potentially
    20+ seconds between updates -- the user had no feedback.

    The new approach separates two concerns:
        1. How often we REPORT progress  (every 1,000 rows -- PROGRESS_INTERVAL)
        2. How often we CALL the API     (every 10,000 rows -- BATCH_SIZE)

    We count every row as it comes off the generator. Every 1,000 rows
    we call on_progress to update the job store. Every 10,000 rows we
    upload the accumulated batch to Google Sheets.

    This gives 10x more frequent UI updates with zero extra API calls.

    on_progress is Optional -- if None, progress is only logged.
    This preserves backwards compatibility with any code that calls
    upload_data_to_sheet without the progress callback.

    PARTIAL FAILURE TRACKING:
    We track rows_successfully_uploaded separately from total_processed.
    If batches fail, the caller gets the accurate count of what actually
    made it into the sheet -- not just what was attempted.
    This lets the UI say "failed after 20,000 of 100,000 rows" accurately.
    """
    if data is None:
        return

    BATCH_SIZE = 10_000

    # Report progress every 1,000 rows without changing API call frequency.
    # 1,000 is enough for smooth UI updates. Lower would work too but
    # adds overhead to the inner loop with diminishing visual returns.
    PROGRESS_INTERVAL = 1_000

    batch = []
    sheet_row = 1
    total_processed = 0          # every row we've seen from the generator
    rows_successfully_uploaded = 0  # rows confirmed written to Sheets
    failed_batches = []

    for row in data:
        batch.append(row)
        total_processed += 1

        # Report progress every PROGRESS_INTERVAL rows.
        # We report total_processed here (not rows_successfully_uploaded)
        # because the rows are in the batch and will be uploaded imminently.
        # Reporting the confirmed count would mean 0% until the first batch
        # completes -- which is the stale progress problem we're fixing.
        
        # but only LOGS at 25% milestones (console stays readable)
        if on_progress and total_processed % PROGRESS_INTERVAL == 0:
            on_progress(total_processed, total_rows)

            # Only log at 0%, 25%, 50%, 75%, 100% milestones
            if total_rows > 0:
                percent = (total_processed / total_rows) * 100
                milestone = int(percent / 25) * 25
                last_milestone = int(((total_processed - PROGRESS_INTERVAL) / total_rows) * 100 / 25) * 25
                if milestone != last_milestone:
                    logger.info(
                        f"Upload to {spreadsheet_id}: "
                        f"{total_processed:,}/{total_rows:,} rows "
                        f"({milestone}%)"
                    )

        # When batch is full, upload it to Google Sheets.
        if len(batch) >= BATCH_SIZE:
            range_name = f"Sheet1!A{sheet_row}"
            try:
                upload_batch_with_retry(
                    sheets_service, spreadsheet_id, batch, range_name
                )
                rows_successfully_uploaded += len(batch)
                logger.info(
                    f"Uploaded rows {sheet_row} to {sheet_row + len(batch) - 1}"
                )
                sheet_row += len(batch)
            except RuntimeError as e:
                logger.error(f"Failed batch at row {sheet_row}: {e}", exc_info=True)
                failed_batches.append(sheet_row)
                sheet_row += len(batch)

            batch = []

    # Upload the final partial batch
    if batch:
        range_name = f"Sheet1!A{sheet_row}"
        try:
            upload_batch_with_retry(
                sheets_service, spreadsheet_id, batch, range_name
            )
            rows_successfully_uploaded += len(batch)
            logger.info(
                f"Uploaded final batch: rows {sheet_row} to "
                f"{sheet_row + len(batch) - 1}"
            )
            # Final progress report after the last batch
            if on_progress:
                on_progress(rows_successfully_uploaded, total_rows)
        except RuntimeError as e:
            logger.error(f"Failed final batch at row {sheet_row}: {e}", exc_info=True)
            failed_batches.append(sheet_row)

    if failed_batches:
        logger.error(
            f"Upload finished with {len(failed_batches)} failed batch(es) "
            f"at rows: {failed_batches}. "
            f"{rows_successfully_uploaded} of {total_processed} rows "
            f"uploaded successfully to {spreadsheet_id}."
        )
    else:
        logger.info(
            f"Upload complete -- {rows_successfully_uploaded} rows "
            f"written to {spreadsheet_id}."
        )

    # Return the confirmed upload count so the caller (background_upload
    # in main.py) can store the accurate number in the job status.
    # This is what enables the "failed after X of Y rows" message in the UI.
    return rows_successfully_uploaded
