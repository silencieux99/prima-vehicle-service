web: Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & gunicorn app:app --bind 0.0.0.0:$PORT --timeout 90 --workers 2 --threads 1 --worker-class sync --max-requests 500 --max-requests-jitter 50
