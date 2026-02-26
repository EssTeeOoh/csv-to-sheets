# Use official Python 3.11 slim image as base.
# 'slim' means it's a minimal version, smaller file size, faster to download.
FROM python:3.11-slim

# Set the working directory inside the container.
# All subsequent commands run from this path.
WORKDIR /app

# Docker caches each step. If requirements.txt hasn't changed,
# Docker skips the pip install step on rebuilds (much faster).
COPY requirements.txt .

# Install dependencies.
# --no-cache-dir keeps the image size smaller by not storing pip's download cache.
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container.
# The .dockerignore file controls what gets excluded.
COPY . .

# Expose port 8000 so the outside world can reach the FastAPI server.
# This doesn't publish the port, it's documentation that says "this app uses 8000".
EXPOSE 8000

# The command that runs when the container starts.
# Use 0.0.0.0 so the server listens on all network interfaces inside the container
# Required for Docker port mapping to work.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
