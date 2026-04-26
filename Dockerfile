FROM python:3.11-slim

WORKDIR /app

# Install system deps including ffmpeg for yt-dlp
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Update yt-dlp to latest (penting! YouTube sering update)
RUN yt-dlp -U || true

COPY . .

# ClawCloud pakai port 8000, bukan 7860 (itu HF Space)
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
