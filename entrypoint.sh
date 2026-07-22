#!/bin/sh
set -e

# yt-dlp breaks whenever YouTube changes their player; always start on the
# latest release. Set YTDLP_AUTO_UPDATE=0 to pin the version baked into the image.
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
    pip install --no-cache-dir --upgrade yt-dlp bgutil-ytdlp-pot-provider \
        || echo "WARNING: yt-dlp upgrade failed; continuing with the installed version"
fi

exec python main.py
