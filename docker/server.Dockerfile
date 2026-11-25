FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY harness ./harness
COPY workers ./workers
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils librsvg2-bin \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=secret,id=pip-ca \
    if [ -f /run/secrets/pip-ca ]; then export PIP_CERT=/run/secrets/pip-ca; fi \
    && pip install --no-cache-dir . \
    && useradd --system --uid 10001 harness \
    && install -d -m 0700 -o harness -g harness /data /skills

ENV PYTHONUNBUFFERED=1 HARNESS_DATA_DIR=/data
USER harness
CMD ["mini-harness", "serve", "--host", "0.0.0.0"]
