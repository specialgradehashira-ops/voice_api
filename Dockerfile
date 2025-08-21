FROM python:3.12-slim

# OS deps for audio/video
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg espeak-ng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000
CMD ["python", "app.py"]
