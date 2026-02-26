import csv
import io


def parse_csv(file_bytes: bytes) -> list[list]:
    """
    Takes raw CSV bytes and returns a list of rows.
    Each row is a list of string values.

    Example output:
    [
        ["Name", "Age", "City"],
        ["Alice", "30", "Lagos"],
    ]
    """
    # Decode bytes to string, then wrap in StringIO so csv.reader can read it
    # like a file — csv.reader expects a file-like object, not raw text
    try:
        content = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Some CSV files use latin-1 encoding (common with Excel exports)
        content = file_bytes.decode("latin-1")

    reader = csv.reader(io.StringIO(content))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    # The 'if any(...)' filters out completely empty rows

    return rows


def validate_csv(rows: list[list]) -> tuple[bool, str]:
    """
    Basic validation before we try to upload anything to Google.
    Returns (is_valid, error_message).
    """
    if not rows:
        return False, "The CSV file is empty."

    if len(rows) < 2:
        return False, "The CSV file must have at least a header row and one data row."

    header = rows[0]
    if not any(cell.strip() for cell in header):
        return False, "The header row appears to be empty."

    # Check that all rows have the same number of columns as the header
    num_columns = len(header)
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != num_columns:
            # We don't hard-fail here, Google Sheets handles ragged rows fine.
            # But we log it as a warning. For now we just pass.
            pass

    return True, ""