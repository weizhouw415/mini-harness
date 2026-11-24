FROM python:3.12-slim-bookworm AS wheels

WORKDIR /build
COPY pyproject.toml ./
COPY harness ./harness
COPY workers ./workers
RUN --mount=type=secret,id=pip-ca,target=/run/secrets/pip-ca \
    if [ -s /run/secrets/pip-ca ]; then export PIP_CERT=/run/secrets/pip-ca; fi \
    && pip wheel --no-cache-dir --wheel-dir /wheels '.[cloud]'

FROM python:3.12-slim-bookworm

RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    apt-get update \
    && apt-get install -y --no-install-recommends chromium-headless-shell fonts-liberation poppler-utils librsvg2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --no-index --find-links=/wheels 'mini-harness[cloud]' \
    && useradd --create-home --uid 10001 worker

ENV PYTHONUNBUFFERED=1 \
    MINI_WORKER_WORKSPACE_ROOT=/tmp/mini-harness-worker \
    MINI_CLOUD_BROWSER_EXECUTABLE=/usr/bin/chromium-headless-shell \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config
USER worker
CMD ["python", "-m", "workers.cloud"]
