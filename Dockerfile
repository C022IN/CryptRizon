FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Ensure your app file is named exactly 'app.py' at repo root
COPY app.py ./app.py

ENV PORT=8000
EXPOSE 8000

# Use shell form so $PORT expands; fallback to 8000 if not set
CMD ["python", "-u", "-c", "import os, uvicorn; uvicorn.run('app:app', host='0.0.0.0', port=int(os.getenv('PORT','8000')))" ]


