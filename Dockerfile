FROM python:3.12-slim

WORKDIR /app

# System deps for aiosqlite / greenlet compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY dashboard/ ./dashboard/

# Ensure the data directory exists for SQLite volume mount
RUN mkdir -p /app/data

ENV DB_PATH=/app/data/trading.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
