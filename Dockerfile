FROM python:3.11-slim

# System deps + vim (unchanged)
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    vim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt /app/

# Install Python deps including beets for music tagging
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir flask beautifulsoup4 beets

# App files
COPY . /app

RUN mkdir -p /config /database

EXPOSE 5000
CMD ["python", "app.py"]
