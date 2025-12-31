
FROM python:3.11-slim

# Install system dependencies and vim
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    vim \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt /app/

# Install Python dependencies (Flask + your app dependencies)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install flask

# Copy app files
COPY . /app

# ✅ Create config and database directories
RUN mkdir -p /config /database

# Expose Flask port
EXPOSE 5000

# Run Flask app
CMD ["python", "app.py"]
