FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY photo_organizer.py .

# Default configuration file
COPY config.json .

# Create volume mount points
RUN mkdir -p /data/photos /data/output

CMD ["python", "photo_organizer.py", "--scan-existing"]