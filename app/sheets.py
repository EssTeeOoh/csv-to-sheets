import os
import json
import base64
import time
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Scopes define what permissions we're requesting.
# Spreadsheets = read/write data, Drive = create files and set permissions.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def get_google_services():
    """
    Authenticates using OAuth2 and returns two API clients: Sheets and Drive.

    Supports two modes automatically:

    LOCAL (development):
      Reads token.json and oauth_credentials.json from disk.
      On first run, opens a browser for you to authorize access.

    HOSTED (Railway/any server):
      Reads TOKEN_JSON_B64 and OAUTH_CREDENTIALS_B64 environment variables.
      These are base64-encoded versions of the same JSON files.
      No browser needed — token was generated locally and uploaded as env var.
    """
    creds = None

    # Check if we're running hosted (env vars present) or local (files on disk)
    token_json_b64 = os.getenv("TOKEN_JSON_B64")

    if token_json_b64:
        # HOSTED MODE 
        # Decode the base64 env var back into a JSON string and load credentials
        print("Auth mode: hosted (using TOKEN_JSON_B64 environment variable)")
        token_data = base64.b64decode(token_json_b64).decode()
        creds = Credentials.from_authorized_user_info(
            json.loads(token_data), SCOPES
        )
    else:
        # LOCAL MODE 
        # Read token.json from disk as normal
        token_path = os.getenv("TOKEN_PATH", "token.json")
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # If token is expired, refresh it automatically (works in both modes)
    if creds and creds.expired and creds.refresh_token:
        print("Token expired, refreshing automatically...")
        creds.refresh(Request())

        # In local mode, save the refreshed token back to disk
        # In hosted mode, we can't persist files so we skip this
        if not token_json_b64:
            token_path = os.getenv("TOKEN_PATH", "token.json")
            with open(token_path, "w") as f:
                f.write(creds.to_json())

    # If still no valid creds, we need to run the OAuth browser flow
    if not creds or not creds.valid:
        if token_json_b64:
            # On a server we can't open a browser, tell the user to regenerate
            raise RuntimeError(
                "TOKEN_JSON_B64 is set but credentials are invalid or expired. "
                "Please regenerate token.json locally and update the Railway env var."
            )

        # Local only: open browser for first-time authorization
        oauth_credentials_path = os.getenv("OAUTH_CREDENTIALS_PATH", "oauth_credentials.json")
        flow = InstalledAppFlow.from_client_secrets_file(
            oauth_credentials_path, SCOPES
        )
        creds = flow.run_local_server(port=8080)

        # Save token for future local runs
        token_path = os.getenv("TOKEN_PATH", "token.json")
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    return sheets_service, drive_service


def create_spreadsheet(sheets_service, title: str, rows: int = 1000, cols: int = 26) -> str:
    """
    Creates a new Google Spreadsheet with exact grid dimensions.

    By specifying rowCount and columnCount at creation time, we avoid
    the need to resize after creation, safer for concurrent uploads.

    Args:
        sheets_service: authenticated Sheets API client
        title: name of the spreadsheet
        rows: exact number of rows needed
        cols: exact number of columns needed
    """
    spreadsheet_body = {
        "properties": {
            "title": title
        },
        "sheets": [{
            "properties": {
                "title": "Sheet1",
                "gridProperties": {
                    "rowCount": rows,
                    "columnCount": cols
                }
            }
        }]
    }

    spreadsheet = sheets_service.spreadsheets().create(
        body=spreadsheet_body,
        fields="spreadsheetId"
    ).execute()

    return spreadsheet["spreadsheetId"]


def make_sheet_public(drive_service, spreadsheet_id: str):
    """
    Sets the spreadsheet sharing permission to 'anyone with the link can view'.
    Without this, only the authenticated user can see the sheet.
    """
    permission = {
        "type": "anyone",
        "role": "reader"
    }

    drive_service.permissions().create(
        fileId=spreadsheet_id,
        body=permission
    ).execute()


def upload_batch_with_retry(sheets_service, spreadsheet_id: str, batch: list[list],
                             range_name: str, max_retries: int = 3):
    """
    Uploads a single batch with exponential backoff retry logic.

    If a batch fails due to a timeout or transient network error,
    we wait and retry up to max_retries times before giving up.

    Exponential backoff:
      Attempt 1 fails → wait 2s → retry
      Attempt 2 fails → wait 4s → retry
      Attempt 3 fails → wait 8s → raise error
    """
    for attempt in range(max_retries):
        try:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="RAW",
                body={"values": batch}
            ).execute()
            return  # success, exit retry loop

        except (HttpError, Exception) as e:
            is_last_attempt = attempt == max_retries - 1

            if is_last_attempt:
                raise RuntimeError(
                    f"Batch at {range_name} failed after {max_retries} attempts: {e}"
                )

            wait_seconds = 2 ** (attempt + 1)
            print(f"  ⚠ Batch {range_name} failed (attempt {attempt + 1}/{max_retries}), "
                  f"retrying in {wait_seconds}s... Error: {e}")
            time.sleep(wait_seconds)


def upload_data_to_sheet(sheets_service, spreadsheet_id: str, data: list[list]):
    """
    Uploads all rows to the spreadsheet in batches with retry logic.

    Google Sheets has a hard limit of 10 million cells per spreadsheet.
    Check this upfront, then upload in batches of 10,000 rows.
    Each batch is retried up to 3 times with exponential backoff if it fails.

    Batch size tradeoffs:
      Smaller (1,000): more API calls, slower, rarely timeout
      Larger (10,000): fewer API calls, faster, slight timeout risk on slow connections
    """
    if not data:
        return

    BATCH_SIZE = 10000
    total_rows = len(data)
    total_cols = max(len(row) for row in data)

    # Pre-flight: reject before hitting Google if data exceeds 10M cell limit
    total_cells = total_rows * total_cols
    CELL_LIMIT = 10_000_000
    if total_cells > CELL_LIMIT:
        raise ValueError(
            f"Data too large for Google Sheets: {total_rows} rows x {total_cols} cols "
            f"= {total_cells:,} cells, exceeds the 10,000,000 cell limit."
        )

    print(f"Uploading {total_rows} rows x {total_cols} cols in batches of {BATCH_SIZE}...")

    failed_batches = []

    for start_index in range(0, total_rows, BATCH_SIZE):
        batch = data[start_index: start_index + BATCH_SIZE]
        sheet_row = start_index + 1  # Sheets rows are 1-indexed
        range_name = f"Sheet1!A{sheet_row}"

        try:
            upload_batch_with_retry(sheets_service, spreadsheet_id, batch, range_name)
            print(f"  ✓ Uploaded rows {sheet_row} to {sheet_row + len(batch) - 1} of {total_rows}")

        except RuntimeError as e:
            # Log failure but continue with next batches, we don't want one failed batch to stop the entire upload
            print(f"  ✗ Failed batch at row {sheet_row}: {e}")
            failed_batches.append(sheet_row)

    if failed_batches:
        print(f"Upload finished with {len(failed_batches)} failed batches at rows: {failed_batches}")
    else:
        print(f"Upload complete — {total_rows} rows written successfully.")