FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
# requirements.lock が存在すればそれを使用、なければ requirements.txt からインストール
COPY requirements.lock* ./
RUN if [ -f requirements.lock ]; then pip install --no-cache-dir -r requirements.lock; else pip install --no-cache-dir -r requirements.txt; fi
COPY . .
RUN useradd -m botuser \
    && mkdir -p /var/lib/ollama-bot logs \
    && chown -R botuser:botuser /app /var/lib/ollama-bot
USER botuser
CMD ["python", "-m", "front.discord.main"]
