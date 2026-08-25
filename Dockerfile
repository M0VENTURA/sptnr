FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Australia/Melbourne

# Default paths for the bundled Essentia-to-Metadata installation
ENV ESSENTIA_SCRIPT_PATH=/opt/Essentia-to-Metadata/tag_music.py
ENV ESSENTIA_MODELS_DIR=/opt/essentia_models

# System deps + tzdata + ffmpeg + git + wget for Essentia
# psycopg2-binary is pre-compiled so we no longer need build-essential/libpq-dev/gcc
# ca-certificates is REQUIRED — without it the container has no trusted root CA
# bundle, so every HTTPS call (MusicBrainz, Discogs, Last.fm, Cover Art Archive…)
# fails TLS verification with CERTIFICATE_VERIFY_FAILED.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ffmpeg \
    git \
    wget \
    ca-certificates \
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

# Install essentia-tensorflow and its dependencies.
# Only available for linux/amd64 — skipped on arm64 where no wheel exists.
RUN if [ "$(uname -m)" = "x86_64" ]; then \
        pip install --no-cache-dir essentia-tensorflow; \
    else \
        echo "⚠ Skipping essentia-tensorflow (no wheel for $(uname -m))"; \
    fi

# Essentia ML models are downloaded at runtime via the setup wizard or
# can be mounted at /opt/essentia_models from a pre-downloaded directory.
# Build-time downloads are skipped because essentia.upf.edu is often slow
# and the models are optional (the app handles missing models gracefully).
RUN mkdir -p /opt/essentia_models

# App files — .dockerignore prevents .git, documentation/, tests/ etc. from being copied
COPY . /app

# Sanitize line endings (Windows → Unix) and make entrypoint executable.
# Without this, git on Windows converts LF→CRLF which breaks bash on Linux.
RUN find /app -name "*.sh" -exec sed -i 's/\r$//' {} + \
    && chmod +x /app/entrypoint.sh

RUN mkdir -p /config /state

EXPOSE 5000
ENTRYPOINT ["./entrypoint.sh"]
