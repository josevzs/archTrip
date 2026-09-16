FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py .
COPY archtrip/ archtrip/
COPY static/ static/

ENV ARCHTRIP_DB=/app/data/archtrip.db
VOLUME ["/app/data"]
EXPOSE 8000

# One worker on purpose: the Nominatim rate limiter is an in-process lock.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]
