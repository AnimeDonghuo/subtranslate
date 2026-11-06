FROM python:3.11-slim-bookworm

# Use Debian's Chromium instead of downloading all of Playwright's browsers.
# Only one browser runs at a time, which is important on a 512 MB instance.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHROMIUM_PATH=/usr/bin/chromium \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=bot:bot subtranslate ./subtranslate
COPY THIRD_PARTY_NOTICES.md ./
USER bot
EXPOSE 8080
CMD ["python", "-m", "subtranslate"]
