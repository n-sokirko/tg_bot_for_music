FROM python:3.12-slim-bookworm

# ffmpeg converts audio to mp3.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to solve YouTube's challenges and accepts only Node >= 22
# (Deno >= 2.3), while Debian's own nodejs package is 18, so take Node from its official image.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY musicbot ./musicbot
RUN useradd --create-home bot
USER bot

CMD ["python", "-m", "musicbot"]
