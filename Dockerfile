FROM python:3.11-slim-bookworm

# Use Debian's Chromium instead of downloading all of Playwright's browsers.
# Only one browser runs at a time, which is important on a 512 MB instance.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHROMIUM_PATH=/usr/bin/chromium \
    PORT=8080

ARG TARGETARCH
# Pin and verify upstream Xray releases used for temporary VLESS bridges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates fonts-noto-core fonts-noto-cjk curl unzip \
    && case "${TARGETARCH:-amd64}" in \
        amd64) XRAY_ARCH=64; XRAY_SHA=e4bd9119f50ae7c41658b1abd0679ae06f98bd864a2907ff33f2f2cf7081b214 ;; \
        arm64) XRAY_ARCH=arm64-v8a; XRAY_SHA=1811c8f1da0c3b6dc707ebaf1c8390e987ba39859a8f0a7aadd8526157f2ec8d ;; \
        *) echo 'Unsupported Xray architecture' >&2; exit 1 ;; \
    esac \
    && curl -fsSL --retry 3 "https://github.com/XTLS/Xray-core/releases/download/v26.10.31/Xray-linux-${XRAY_ARCH}.zip" -o /tmp/xray.zip \
    && echo "${XRAY_SHA}  /tmp/xray.zip" | sha256sum -c - \
    && unzip -p /tmp/xray.zip xray > /usr/local/bin/xray \
    && chmod 755 /usr/local/bin/xray \
    && rm /tmp/xray.zip \
    && apt-get purge -y curl unzip \
    && apt-get autoremove -y \
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
