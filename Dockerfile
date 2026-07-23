# Static FFmpeg binaries. Installing ffmpeg via apt pulls ~205 packages and
# ~466MB of X11/OpenGL/SDL dependencies we never use for audio-only playback;
# copying a static build instead cuts minutes off the build and shrinks the image.
FROM mwader/static-ffmpeg:7.1 AS ffmpeg

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# discord.py encodes voice audio with libopus, which is a separate shared library
# from the ffmpeg binaries below (ffmpeg decodes the source; libopus encodes what
# gets sent to Discord). Installing ffmpeg via apt used to pull this in implicitly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/

WORKDIR /app

# Dependencies are installed before the source is copied so this layer stays
# cached; editing the bot's code then rebuilds in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data
VOLUME ["/data"]

# Optional TaskBoard web dashboard (only serves traffic when WEB_ENABLED=1).
EXPOSE 8080

# Passes while main.py keeps refreshing its heartbeat file.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "healthcheck.py"]

ENTRYPOINT ["sh", "/app/entrypoint.sh"]
