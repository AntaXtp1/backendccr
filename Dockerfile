FROM python:3.11-slim

WORKDIR /app

# Install system deps: ffmpeg untuk yt-dlp + Chromium system deps buat Playwright
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Update yt-dlp to latest (penting! YouTube sering update)
RUN yt-dlp -U || true

# ── Playwright cookie warmer ──────────────────────────────────────────────────
# Chromium headless buat generate fresh YT cookies tiap startup + refresh 1 jam
RUN pip install playwright --no-cache-dir && \
    playwright install chromium
# ─────────────────────────────────────────────────────────────────────────────

COPY . .

# ClawCloud pakai port 8000
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
