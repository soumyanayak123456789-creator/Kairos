# Clutch — Cloud Run container (build step 1).
FROM python:3.12-slim

# Keep Python output unbuffered so logs show up in Cloud Logging immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run injects the port to listen on via $PORT (defaults to 8080).
# Use the shell form so $PORT is expanded at runtime, not baked in at build.
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
