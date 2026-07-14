FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Australia/Melbourne

# Default paths for the bundled Essentia-to-Metadata installation
ENV ESSENTIA_SCRIPT_PATH=/opt/Essentia-to-Metadata/tag_music.py
ENV ESSENTIA_MODELS_DIR=/opt/essentia_models

# System deps + tzdata + vim + gunicorn + ffmpeg + git + wget for Essentia
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    libpq-dev \
    gcc \
    vim \
    ffmpeg \
    git \
    wget \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt /app/

# Install Python deps including gunicorn, beets for music tagging, and pyyaml for config
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir flask beautifulsoup4 gunicorn pyyaml

# Install Essentia-to-Metadata (https://github.com/WB2024/Essentia-to-Metadata)
# Clone the repo so tag_music.py is available at the default ESSENTIA_SCRIPT_PATH
RUN git clone --depth=1 https://github.com/WB2024/Essentia-to-Metadata.git /opt/Essentia-to-Metadata

# Install essentia-tensorflow and its dependencies
RUN pip install --no-cache-dir essentia-tensorflow numpy

# Download Essentia ML models (~87 MB) to the default ESSENTIA_MODELS_DIR.
# The download is optional: if essentia.upf.edu is unreachable in the build
# environment (e.g. CI) the container still builds successfully.  Users can
# also mount a pre-downloaded model directory at runtime via ESSENTIA_MODELS_DIR.
RUN mkdir -p /opt/essentia_models && \
    wget -q --tries=3 --timeout=60 --retry-connrefused -P /opt/essentia_models \
        https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.pb || true && \
    wget -q --tries=3 --timeout=60 --retry-connrefused -P /opt/essentia_models \
        https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb || true && \
    wget -q --tries=3 --timeout=60 --retry-connrefused -P /opt/essentia_models \
        https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json || true && \
    wget -q --tries=3 --timeout=60 --retry-connrefused -P /opt/essentia_models \
        https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb || true && \
    wget -q --tries=3 --timeout=60 --retry-connrefused -P /opt/essentia_models \
        https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json || true

# App files - COPY ALL FILES INCLUDING STATIC FOLDER
COPY . /app

# Verify static folder and files are present (run AFTER COPY to catch issues)
RUN echo "=== STATIC FOLDER VERIFICATION ===" && \
    if [ -d /app/static ]; then \
        echo "✓ Static folder exists at /app/static"; \
        file_count=$(find /app/static -type f | wc -l); \
        echo "✓ Found $file_count files in static folder:"; \
        find /app/static -type f | head -10; \
    else \
        echo "✗ ERROR: Static folder missing at /app/static"; \
        mkdir -p /app/static; \
    fi && \
    echo "=== END VERIFICATION ==="

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /config /database

EXPOSE 5000
ENTRYPOINT ["./entrypoint.sh"]

