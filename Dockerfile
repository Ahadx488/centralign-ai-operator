FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8000 8001 8002 8003 8004

CMD ["python", "-m", "env.server"]
