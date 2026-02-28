import os
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from app.sheets import get_google_services, create_spreadsheet, make_sheet_public, upload_data_to_sheet
from app.utils import parse_csv, validate_csv

load_dotenv()

app = FastAPI(
    title="CSV to Google Sheets API",
    description="Upload a CSV file and get back a public Google Sheets URL.",
    version="1.0.0"
)

# Point FastAPI to the templates folder
templates = Jinja2Templates(directory="app/templates")

# Max file size: 200MB
# Render free tier has 512MB RAM. A CSV file briefly exists in memory
MAX_FILE_SIZE_MB = 200
MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024

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


@app.get("/")
def root(request: Request):
    """Serves the HTML upload UI from app/templates/index.html"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload")
async def upload_csv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    """
    Main endpoint. Accepts a CSV file, creates a Google Sheet,
    and returns the sheet URL immediately while uploading data in background.

    Flow:
    1. Validate file type
    2. Read file in chunks, reject the moment size limit is exceeded
    3. Parse CSV and validate contents
    4. Check cell count against Google's 10M limit
    5. Create sheet with exact dimensions
    6. Make sheet public
    7. Return URL immediately
    8. Upload data in background
    """

    # 1. Validate file type 
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted.")

    # 2. Read file in chunks with TRUE early rejection 
    #  read chunk → add to total_size → check if over limit → reject WITHOUT appending
    #  The moment we exceed the limit, we clear what we have and raise immediately.
    
    
    chunks = []
    total_size = 0
    CHUNK_SIZE = 1024 * 1024  # 1MB per chunk

    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break

        total_size += len(chunk)

        
        if total_size > MAX_FILE_SIZE:
            chunks.clear()  # free the chunks we already stored
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File too large. Maximum size is {MAX_FILE_SIZE_MB}MB. "
                    f"Consider splitting your CSV into smaller files."
                )
            )

        chunks.append(chunk)  

    file_bytes = b"".join(chunks)
    chunks.clear()  # free memory immediately after creating file_bytes

    if not file_bytes:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    # 3. Parse and validate CSV
    rows = parse_csv(file_bytes)
    del file_bytes  # free raw bytes immediately after parsing
                    

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
                f"CSV too large for Google Sheets: "
                f"{total_rows:,} rows x {total_cols} cols "
                f"= {total_cells:,} cells. "
                f"Google Sheets limit is 10,000,000 cells."
            )
        )

    # 5. Create Google Sheet with exact dimensions
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

    #6. Return URL immediately 
    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

    #7. Upload data in background
    background_tasks.add_task(background_upload, spreadsheet_id, rows)

    return JSONResponse(
        status_code=202,
        content={
            "message": "Sheet created successfully. Data is being uploaded in the background.",
            "spreadsheet_url": sheet_url,
            "rows_queued": total_rows - 1,  
            "columns": total_cols,
            "total_cells": total_cells,     
            "filename": file.filename
        }
    )