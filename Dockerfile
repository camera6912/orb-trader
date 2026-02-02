FROM python:3.11-slim

# Keep logs + timestamps consistent with the trading session.
ENV TZ=America/New_York \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps:
# - tzdata: timezone database
# - curl: used by container healthcheck / debugging
RUN apt-get update \
  && apt-get install -y --no-install-recommends tzdata curl \
  && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY src ./src

# Config structure (NO secrets baked into image)
RUN mkdir -p config logs
COPY config/settings.yaml ./config/settings.yaml
COPY config/secrets.yaml.example ./config/secrets.yaml.example

# Default health port (can override via env)
ENV HEALTH_PORT=8000

# Optional container healthcheck (expects /health endpoint)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${HEALTH_PORT}/health" || exit 1

CMD ["python", "-m", "src.main"]
