FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY video_server.py drive_backend.py index.html photos.html ./

ENV STORAGE_MODE=drive
ENV PORT=8080
ENV MEDIA_PREWARM=0
ENV MEDIA_CACHE_DIR=/tmp/comfy-media-cache

EXPOSE 8080

CMD ["python3", "video_server.py"]
