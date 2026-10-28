# syntax=docker/dockerfile:1
# Pin the base image by digest in production (docker buildx imagetools inspect python:3.12-slim).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY openapi.yaml schema.graphql ./

RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin appuser \
    && mkdir -p /data && chown appuser /data
USER 10001

ENV DB_PATH=/data/home-energy.sqlite3 \
    BIND=0.0.0.0:8080
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import os,urllib.request;h,_,p=os.environ['BIND'].rpartition(':');urllib.request.urlopen(f'http://127.0.0.1:{p}/readyz',timeout=2)"]

# uvicorn handles SIGTERM: stops accepting, drains for up to 10 s, then runs the shutdown hooks.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "app"]
