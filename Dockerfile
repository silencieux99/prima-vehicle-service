FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ARG CACHEBUST=20261028c
RUN echo cachebust=$CACHEBUST

RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
ENV DISPLAY=:99
ENV PRIMA_HEADLESS=true
EXPOSE 8080

CMD Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & gunicorn app:app --bind 0.0.0.0:$PORT --timeout 90 --workers 2 --threads 1 --worker-class sync --max-requests 500 --max-requests-jitter 50
