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
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN uv pip install --no-cache-dir .
COPY app ./app

EXPOSE 8080
EXPOSE 3478/udp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
