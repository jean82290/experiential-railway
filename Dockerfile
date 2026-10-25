# Experiential gateway on Railway
#
# Installs the official, pinned Experiential release from PyPI with every
# dependency locked by SHA-256 (requirements.txt), adds Caddy (official
# release, checked against a SHA-512 pinned in this file) as the public front
# door, and runs both behind an entrypoint that adapts them to Railway: $PORT,
# a volume at /data, configuration from variables, and graceful shutdown.
#
# To upgrade Experiential: the weekly GitHub Action in .github/workflows
# regenerates requirements.txt for the newest release and opens a pull request.

ARG CADDY_VERSION=2.11.7
ARG CADDY_SHA512=a7a433a1b133efc3c8d10eb0b99d52a24b5ef5c322dc77f5282182b1c0402139ab83f3a99f0c52409df77d20123fb0b523edad8a66d8f5e49136197bf61ef0e7

FROM debian:bookworm-slim AS fetch
ARG CADDY_VERSION
ARG CADDY_SHA512
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /fetch
RUN set -eu; \
    asset="caddy_${CADDY_VERSION}_linux_amd64.tar.gz"; \
    curl -fsSL --retry 3 -o "${asset}" \
      "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/${asset}"; \
    echo "${CADDY_SHA512}  ${asset}" | sha512sum -c -; \
    tar -xzf "${asset}" caddy LICENSE; \
    install -m 0755 caddy /caddy; \
    cp LICENSE /CADDY-LICENSE

FROM python:3.12-slim-bookworm
# tini: a real init as PID 1, so signals and zombie processes are handled.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /app /usr/share/doc/caddy /usr/share/doc/experiential-railway

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps \
      -r /app/requirements.txt \
    && exp --help > /dev/null \
    && python -c "import exp_gateway_native"

COPY --from=fetch /caddy /usr/local/bin/caddy
COPY --from=fetch /CADDY-LICENSE /usr/share/doc/caddy/LICENSE
COPY NOTICE /usr/share/doc/experiential-railway/NOTICE
COPY Caddyfile Caddyfile.setup bootstrap.py entrypoint.sh /app/
RUN chmod 0755 /app/entrypoint.sh

LABEL org.opencontainers.image.title="Experiential gateway for Railway" \
      org.opencontainers.image.description="Experiential AI gateway (OpenAI-compatible, per-key budgets), packaged for Railway" \
      org.opencontainers.image.licenses="Apache-2.0"

# EXP_TELEMETRY=0 turns off Experiential's anonymous usage statistics (PostHog),
# which are on by default upstream. Set it to 1 to send them.
ENV PORT=8080 \
    EXP_DATA_DIR=/data \
    EXP_TELEMETRY=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# `railway ssh` lands here, so `exp config gateway ...` finds the gateway state
# (/data/.exp) without --root.
WORKDIR /data
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
