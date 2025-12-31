
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    vim \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /usr/src/app

# ✅ Copy requirements first for caching
COPY requirements.txt /usr/src/app/

# ✅ Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ✅ Copy the rest of the app
COPY . /usr/src/app

# ✅ Copy template config.yaml into /config
COPY config/config.yaml /config/config.yaml

# Create database directory
RUN mkdir /database
VOLUME ["/database"]

# ✅ Default to perpetual mode
ENTRYPOINT ["python", "./start.py"]
