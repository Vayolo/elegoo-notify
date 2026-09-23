FROM python:3.11-slim

# opencv-python-headless richiede la libreria di sistema glib
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY dashboard/ dashboard/
COPY models/ models/

ENV PYTHONUNBUFFERED=1
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "app.main"]
