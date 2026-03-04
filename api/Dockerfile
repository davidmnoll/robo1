FROM ghcr.io/astral-sh/uv:debian-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN uv python install 3.14
RUN uv venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN uv pip install --upgrade pip

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-dev \
    libffi-dev \
    libssl-dev \
    build-essential \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    libavfilter-dev \
    libpostproc-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN uv pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
