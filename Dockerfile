FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app ./app
COPY sql ./sql

# Run as a non-root user
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# All runtime config (DB DSN, base URL, etc.) comes from environment
# variables at container run time -- see .env.example / README.md
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
