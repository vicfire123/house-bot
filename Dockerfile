FROM python:3.12-slim

# ffmpeg plays the audio; libopus is the voice codec discord.py needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY cogs ./cogs

CMD ["python", "-u", "bot.py"]
