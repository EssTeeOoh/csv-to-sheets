# CSV to Google Sheets API

A REST API that accepts a CSV file, creates a publicly accessible Google Spreadsheet, and returns the sheet URL instantly. Data is uploaded in the background so you get the URL without waiting.

## Live Demo

**Web UI:** https://csv-to-sheets.onrender.com

**API Endpoint:** `POST https://csv-to-sheets.onrender.com/upload`

**API Docs (Swagger):** https://csv-to-sheets.onrender.com/docs

---

## How It Works

```
Client uploads CSV
        ↓
Validate file (type, size, structure)
        ↓
Check Google Sheets cell limit (10M max)
        ↓
Create empty Google Sheet with exact dimensions
        ↓
Make sheet publicly accessible
        ↓
Return sheet URL immediately  ← client gets URL here
        ↓
Upload CSV data in background (batches of 10,000 rows)
```

The key architectural decision is returning the URL immediately after the sheet is created, then uploading data asynchronously in the background. This keeps response times fast regardless of file size.

---

## API Usage

### Endpoint

```
POST /upload
Content-Type: multipart/form-data
```

### Request

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | `.csv` file | Yes | The CSV file to upload (max 200MB) |

### Response `202 Accepted`

```json
{
  "message": "Sheet created successfully. Data is being uploaded in the background.",
  "spreadsheet_url": "https://docs.google.com/spreadsheets/d/SHEET_ID/edit",
  "rows_queued": 999,
  "columns": 9,
  "total_cells": 8991,
  "filename": "data.csv"
}
```

### Error Responses

| Status | Reason |
|--------|--------|
| `400` | File is not a CSV, is empty, or exceeds 200MB |
| `422` | CSV has no data rows, or exceeds Google's 10M cell limit |
| `500` | Failed to create Google Sheet (auth or API issue) |

---

## Code Examples

### Python

```python
import requests

with open("data.csv", "rb") as f:
    response = requests.post(
        "https://csv-to-sheets.onrender.com/upload",
        files={"file": ("data.csv", f, "text/csv")}
    )

data = response.json()
print(data["spreadsheet_url"])
```

### JavaScript / Fetch

```javascript
const formData = new FormData();
formData.append("file", csvFile); // csvFile is a File object

const response = await fetch("https://csv-to-sheets.onrender.com/upload", {
    method: "POST",
    body: formData
});

const data = await response.json();
console.log(data.spreadsheet_url);
```

### curl

```bash
curl -X POST "https://csv-to-sheets.onrender.com/upload" \
  -F "file=@/path/to/your/data.csv"
```

---

## Local Development Setup

### Prerequisites

- Python 3.11+
- A Google account
- Google Cloud project with Sheets and Drive APIs enabled

### 1. Clone the repository

```bash
git clone https://github.com/EssTeeOoh/csv-to-sheets.git
cd csv-to-sheets
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Mac/Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up Google Cloud credentials

**a. Create a Google Cloud project**
- Go to [console.cloud.google.com](https://console.cloud.google.com)
- Create a new project
- Enable **Google Sheets API** and **Google Drive API**

**b. Create OAuth2 credentials**
- Go to **APIs & Services → Credentials**
- Click **"+ Create Credentials" → "OAuth client ID"**
- Configure the consent screen if prompted (External, add your email as test user)
- Choose **"Desktop app"** → Create
- Download the JSON file and save it as `oauth_credentials.json` in the project root

### 5. Configure environment variables

Create a `.env` file in the project root:

```
OAUTH_CREDENTIALS_PATH=oauth_credentials.json
TOKEN_PATH=token.json
```

### 6. Authorize Google access (first time only)

```bash
uvicorn app.main:app --reload
```

On the first request, a browser window will open asking you to authorize access with your Google account. After authorizing, a `token.json` file is saved automatically, you won't need to authorize again.

### 7. Test the API

Visit `http://localhost:8000` for the web UI, or `http://localhost:8000/docs` for the Swagger API docs.

---

## Docker Setup

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running

### Run with Docker Compose

```bash
docker-compose up --build
```

The API will be available at `http://localhost:8000`.

> **Note:** Make sure `oauth_credentials.json` and `token.json` exist in the project root before running Docker. They are mounted into the container at runtime and are never baked into the image.

---

## Handling Large Files

- Files are read in **1MB chunks**, size limit is enforced before each chunk is stored, so RAM never exceeds the 200MB limit even if a larger file is uploaded
- Data is uploaded to Google Sheets in **batches of 10,000 rows**
- Each batch has **automatic retry with exponential backoff** (up to 3 attempts)
- The sheet is created with **exact dimensions** at creation time — no resizing needed
- Google's hard limit of **10 million cells** is checked before any API call



### Scaling to 100,000+ rows daily

For production-scale workloads, the recommended approach would be:

1. **Job queue** (e.g. Celery + Redis) — accept the upload, immediately queue a background job, return the URL. Workers process uploads independently from the web server.

2. **Streaming CSV parsing** — read and upload line by line instead of loading the entire file into memory. RAM usage stays constant at O(batch_size) regardless of file size.

3. **Multiple sheets** — for files exceeding 10M cells, automatically split data across multiple sheets (Sheet1, Sheet2, etc.) within the same spreadsheet.

4. **Status endpoint** — add a `GET /status/{job_id}` endpoint so clients can poll upload progress rather than relying on background task logs.

```
Current architecture:             Production architecture:

POST /upload                       POST /upload
    ↓                                  ↓
Parse CSV into RAM                 Save file to S3
    ↓                                  ↓
Create sheet                       Queue job (Celery)
    ↓                                  ↓
Return URL                         Return URL + job_id
    ↓                                  ↓
Background upload                  Worker streams CSV → Sheets
                                       ↓
                                   Update job status
```

---

## Project Structure

```
csv-to-sheets/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app and endpoints
│   ├── sheets.py        # Google Sheets/Drive integration
│   ├── utils.py         # CSV parsing and validation
│   └── templates/
│       └── index.html   # Web UI
├── .env                 # Local environment variables (never committed)
├── .gitignore
├── .dockerignore
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `OAUTH_CREDENTIALS_PATH` | Path to OAuth client credentials JSON | Yes (local) |
| `TOKEN_PATH` | Path to saved OAuth token | Yes (local) |
| `TOKEN_JSON_B64` | Base64-encoded token.json contents | Yes (hosted) |
| `PYTHONUNBUFFERED` | Set to `1` for real-time logs in Docker | Optional |

---

## Tech Stack

| Tool | Purpose |
|------|---------|
| **FastAPI** | Web framework and API |
| **Uvicorn** | ASGI server |
| **Google Sheets API v4** | Read/write spreadsheet data |
| **Google Drive API v3** | Create files, set permissions |
| **google-auth-oauthlib** | OAuth2 authentication flow |
| **Jinja2** | HTML template rendering |
| **Docker** | Containerization |
| **Render** | Free cloud hosting |