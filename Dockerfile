# Use an official Python runtime as a parent image
FROM python:3.11-slim
# Updated for better compatibility with latest packages

# Install system dependencies for pip packages (especially for pylast if it uses SSL)
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /usr/src/app

# Copy project files
COPY . .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt