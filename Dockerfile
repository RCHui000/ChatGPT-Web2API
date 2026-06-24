FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg \
    && chmod a+r /etc/apt/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# Persistent Chrome profile (stores login session)
VOLUME /data/chrome-profile

# Cookie file mount point (for headless auth)
VOLUME /data/cookies

ENV W2A_HEADLESS=true
ENV W2A_HOST=0.0.0.0
ENV W2A_USER_DATA_DIR=/data/chrome-profile
ENV W2A_PORT=8080
ENV W2A_CHROME_EXTRA_ARGS="--no-sandbox --disable-dev-shm-usage"

# Only expose the API server. Chrome DevTools (9222) must stay loopback-only.
EXPOSE 8080

# Start script handles cookie injection
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
