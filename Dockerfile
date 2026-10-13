FROM python:3.12-slim

# ffmpeg converts audio to mp3; nodejs runs YouTube's JS challenges for yt-dlp.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY musicbot ./musicbot
RUN useradd --create-home bot
USER bot

CMD ["python", "-m", "musicbot"]
