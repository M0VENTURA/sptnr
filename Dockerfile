FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Australia/Melbourne

# Default paths for the bundled Essentia-to-Metadata installation
ENV ESSENTIA_SCRIPT_PATH=/opt/Essentia-to-Metadata/tag_music.py
ENV ESSENTIA_MODELS_DIR=/opt/essentia_models

# System deps + tzdata + ffmpeg + git + wget for Essentia
# psycopg2-binary is pre-compiled so we no longer need build-essential/libpq-dev/gcc
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ffmpeg \
    git \
    wget \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for Docker layer caching
COPY requirements.txt /app/

# Install all Python deps from requirements.txt (includes flask, gunicorn, psycopg2-binary, etc.)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install Essentia-to-Metadata (https://github.com/WB2024/Essentia-to-Metadata)
# Clone the repo so tag_music.py is available at the default ESSENTIA_SCRIPT_PATH
RUN git clone --depth=1 https://github.com/WB2024/Essentia-to-Metadata.git /opt/Essentia-to-Metadata

# Install essentia-tensorflow and its dependencies (numpy is pulled in automatically)
RUN pip install --no-cache-dir essentia-tensorflow

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

# App files — .dockerignore prevents .git, documentation/, tests/ etc. from being copied
COPY . /app

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /config /database

EXPOSE 5000
ENTRYPOINT ["./entrypoint.sh"]
