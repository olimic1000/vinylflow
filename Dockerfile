FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    flac \
    && rm -rf /var/lib/apt/lists/*

# Verify ffmpeg installation at build time (fails fast if missing)
RUN ffmpeg -version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create output directory
RUN mkdir -p /app/output /app/temp_uploads

# Use Railway's PORT environment variable
CMD sh -c "python -m uvicorn backend.api:app --host 0.0.0.0 --port ${PORT:-8000}"
