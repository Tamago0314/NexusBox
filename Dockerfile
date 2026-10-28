FROM python:3.12-slim

# ffmpeg: Discord へ流す PCM への変換 / libopus0: discord.py の音声エンコード
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY nexusbox/ ./nexusbox/

RUN mkdir -p /app/data

CMD ["python", "-m", "nexusbox"]
