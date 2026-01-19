FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Australia/Melbourne

# System deps + tzdata + vim
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    libpq-dev \
    gcc \
    vim \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
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
