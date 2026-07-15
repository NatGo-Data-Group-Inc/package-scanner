FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AWS_REGION=us-east-1 \
    CATALOG_PREFIX=evidence \
    WEBAPP_LOG_PATH=/tmp/package-scanner-webapp.log \
    GUNICORN_WORKERS=4 \
    GUNICORN_THREADS=8 \
    GUNICORN_TIMEOUT=120 \
    PORT=8000

WORKDIR /app

COPY requirements-webapp.txt /app/requirements-webapp.txt
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r /app/requirements-webapp.txt

COPY package_scanner /app/package_scanner
COPY webapp /app/webapp
COPY candidates /app/candidates
COPY scripts /app/scripts

EXPOSE 8000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers ${GUNICORN_WORKERS} --threads ${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} webapp.app:app"]
