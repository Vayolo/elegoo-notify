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
COPY slicer-profiles/ slicer-profiles/

# PrusaSlicer 2.8.1 (AGPL-3.0, vedi LICENSE-NOTICE) per lo slicing
# on-the-go: AppImage "newer-distros" estratta (no FUSE), symlink al launcher
# (lo script impostata LD_LIBRARY_PATH da solo). Testato: rc=0 su trixie.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libwebkit2gtk-4.1-0 libgl1 libegl1 libxkbcommon0 libx11-6 libxext6 \
        libxrender1 libdbus-1-3 libxdamage1 libxcomposite1 \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sL -o /tmp/prusa.AppImage \
        "https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1%2Blinux-x64-newer-distros-GTK3-202409181416.AppImage" \
    && chmod +x /tmp/prusa.AppImage \
    && /tmp/prusa.AppImage --appimage-extract >/dev/null \
    && mv squashfs-root /opt/prusa \
    && ln -s /opt/prusa/usr/bin/prusa-slicer /usr/local/bin/prusa-slicer \
    && rm /tmp/prusa.AppImage \
    && prusa-slicer --help >/dev/null

ENV PYTHONUNBUFFERED=1
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "app.main"]
