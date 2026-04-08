FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System packages required by whisper/torch runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Use runtime-only deps for cloud deploy (faster, smaller, fewer build failures).
COPY requirements.server.txt ./requirements.server.txt
RUN pip install --no-cache-dir --upgrade pip wheel setuptools && \
    pip install --no-cache-dir -r requirements.server.txt

# Copy app source.
COPY . .

# Render injects $PORT; keep 8181 for local docker runs.
CMD ["sh", "-c", "uvicorn whisperflow.fast_server:app --host 0.0.0.0 --port ${PORT:-8181}"]

