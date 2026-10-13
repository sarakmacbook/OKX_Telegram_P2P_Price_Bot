# P2P Merchant Price Bot — Docker
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Every module the bot imports at runtime — not just the ones it imports first:
# storage.py loads database_adapters.py when a database was selected, and
# runtime_config.py + telegram_store.py hold the settings (and the /database
# credentials) that let a container reconnect after a restart.  Missing one
# here is what makes /database fail inside the image with "No module named …".
COPY bot.py exchanges.py adlinks.py storage.py database_adapters.py \
     runtime_config.py telegram_store.py setup_cli.py ./

# config & data are mounted as volumes; bot creates them if missing.
# For non-interactive docker, pass env vars: BOT_TOKEN, ADMIN_IDS, ASSET, FIAT, INTERVAL
# Example: docker run -e BOT_TOKEN=123:ABC -e ADMIN_IDS=123456 ...
#
# State lives in data.json (mounted). To use a Redis/KV store instead
# (e.g. when several instances share one group), set
# KV_REST_API_URL + KV_REST_API_TOKEN — see storage.py.

VOLUME ["/app/data"]
# keep config.json & data.json in /app (or /app/data if you mount)

CMD ["python", "bot.py"]
