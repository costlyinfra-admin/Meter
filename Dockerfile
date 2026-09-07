# Meter — single production image: builds the web app and serves it from the
# FastAPI backend (one service, one domain). See docs/deploy.md.

# ---- Stage 1: build the React/Vite frontend ----
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build          # -> /web/dist

# ---- Stage 2: Python backend (also serves the built frontend) ----
FROM python:3.11-slim AS app

# psql is needed by the migration runner (deploy/release.sh).
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/ /app/backend/
RUN pip install --no-cache-dir -e /app/backend
COPY deploy/ /app/deploy/

# Bundle the built frontend and tell the API to serve it.
COPY --from=web /web/dist /app/web/dist
ENV METER_STATIC_DIR=/app/web/dist \
    METER_SECURE_COOKIES=true \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
EXPOSE 8000

# start.sh runs migrations + ensures the app-role password, then launches uvicorn
# (binding $PORT, which Render/most PaaS provide).
CMD ["/app/deploy/start.sh"]
