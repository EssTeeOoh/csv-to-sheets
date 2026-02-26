import os
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from app.sheets import get_google_services, create_spreadsheet, make_sheet_public, upload_data_to_sheet
from app.utils import parse_csv, validate_csv

load_dotenv()

app = FastAPI(
    title="CSV to Google Sheets API",
    description="Upload a CSV file and get back a public Google Sheets URL.",
    version="1.0.0"
)

# Max file size: 500MB 
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB in bytes

# Google Sheets hard cell limit
CELL_LIMIT = 10_000_000


def background_upload(spreadsheet_id: str, data: list[list]):
    """
    Runs after we've returned the URL to the client.
    Uploads all CSV data to the sheet in the background. 
    """
    try:
        sheets_service, _ = get_google_services()
        upload_data_to_sheet(sheets_service, spreadsheet_id, data)
    except Exception as e:
        print(f"Background upload failed for sheet {spreadsheet_id}: {e}")


@app.post("/upload")
async def upload_csv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    """
    Main endpoint. Accepts a CSV file, creates a Google Sheet,
    and returns the sheet URL immediately while uploading data in the background.

    Flow:
    1. Validate file type and size
    2. Parse CSV and validate contents
    3. Check cell count against Google's 10M limit
    4. Create sheet with exact dimensions (no resizing needed later)
    5. Make sheet public
    6. Return URL immediately
    7. Upload data in background
    """

    # 1. Validate file type
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")

    # 2. Read file in chunks to avoid memory spike on large files
    # Reading 1MB at a time lets us reject oversized files early
    # without loading the entire file into memory first.
    chunks = []
    total_size = 0
    CHUNK_SIZE = 1024 * 1024  

    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break

        total_size += len(chunk)

        if total_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)}MB."
            )

        chunks.append(chunk)

    file_bytes = b"".join(chunks)

    if not file_bytes:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    # 3. Parse and validate CSV
    rows = parse_csv(file_bytes)
    is_valid, error_message = validate_csv(rows)

    if not is_valid:
        raise HTTPException(status_code=422, detail=error_message)

    # 4. Check cell limit before doing anything with Google 
    total_rows = len(rows)
    total_cols = max(len(row) for row in rows)
    total_cells = total_rows * total_cols

    if total_cells > CELL_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"CSV too large for Google Sheets: {total_rows} rows x {total_cols} cols "
                f"= {total_cells:,} cells. Google Sheets limit is 10,000,000 cells."
            )
        )

    # 5. Create the Google Sheet with exact dimensions 
    # Passing exact row/col count at creation time avoids needing to
    # resize after the fact, which is cleaner and safer for concurrent uploads.
    try:
        sheets_service, drive_service = get_google_services()
        sheet_title = file.filename.replace(".csv", "").strip() or "Uploaded CSV"

        spreadsheet_id = create_spreadsheet(
            sheets_service,
            title=sheet_title,
            rows=total_rows,
            cols=total_cols
        )
        make_sheet_public(drive_service, spreadsheet_id)

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create Google Sheet: {str(e)}"
        )

    # 6. Return URL immediately 
    # We have the sheet ID so we can build the URL before uploading data. 
    # This way the client can start using the sheet right away, even if the data isn't fully uploaded yet. 
    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    # 7. Upload data in background 
    background_tasks.add_task(background_upload, spreadsheet_id, rows)

    return JSONResponse(
        status_code=202,
        content={
            "message": "Sheet created successfully. Data is being uploaded in the background.",
            "spreadsheet_url": sheet_url,
            "rows_queued": total_rows - 1, 
            "columns": total_cols,
            "filename": file.filename
        }
    )


@app.get("/")
def root():
    """Health check endpoint, confirms the server is running."""
    return {"status": "ok", "message": "CSV to Sheets API is running."}