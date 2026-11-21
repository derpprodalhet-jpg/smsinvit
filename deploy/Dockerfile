# Build with scripts/build_images.py so every base image is digest-pinned.
ARG NODE_IMAGE=node:24-trixie-slim
ARG NODE_BUILD_IMAGE=node:24-trixie-slim
ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE} AS dependencies
ARG CODEX_VERSION=0.160.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get -o Acquire::Retries=5 update \
    && (for attempt in 1 2 3 4 5; do \
        apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates libatomic1 && exit 0; \
        if [ "$attempt" = 5 ]; then exit 1; fi; sleep 2; \
    done) \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /opt/smsinvit-bin/node
COPY --from=node /usr/local/lib/node_modules/npm /opt/npm
ENV PATH=/opt/smsinvit-bin:/opt/codex/bin:$PATH
COPY apps/api/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock && rm /tmp/requirements.lock
RUN node /opt/npm/bin/npm-cli.js install --global --prefix /opt/codex --omit=dev --no-audit --no-fund @openai/codex@${CODEX_VERSION} \
    && rm -rf /opt/npm /root/.npm \
    && codex --version

FROM --platform=$BUILDPLATFORM ${NODE_BUILD_IMAGE} AS frontend
WORKDIR /src
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 NODE_USE_ENV_PROXY=1
RUN corepack enable && corepack prepare pnpm@10.24.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json ./
COPY apps/web/package.json ./apps/web/
COPY apps/admin/package.json ./apps/admin/
COPY packages ./packages
COPY patches ./patches
RUN --mount=type=cache,id=smsinvit-pnpm-store,target=/pnpm/store pnpm install --store-dir /pnpm/store --frozen-lockfile --fetch-retries=5 --network-concurrency=6
COPY apps/web ./apps/web
COPY apps/admin ./apps/admin
RUN pnpm --filter @smsinvit/errors build && pnpm --filter @smsinvit/web build && pnpm --filter @smsinvit/admin build

FROM dependencies AS runtime-base
WORKDIR /app
RUN groupadd --gid 10001 smsinvit && useradd --uid 10001 --gid 10001 --home-dir /app/runtime --no-create-home smsinvit \
    && mkdir -p /app/runtime/invitation /app/static/admin && chown -R 10001:10001 /app/runtime
# The runtime does not install packages, mount filesystems, manage users or run
# Perl/infocmp. Keep libraries required by Python/Node/Codex (including zsh).
# dpkg performs real package removal; the remaining package inventory is kept.
RUN apt-get purge -y --allow-remove-essential apt libapt-pkg7.0 mount login passwd util-linux bsdutils perl-base ncurses-bin \
    libudev1 liblastlog2-2 libsmartcols1 libmount1 libblkid1 \
    && rm -rf /var/lib/apt/lists/* \
    && find /usr/bin /usr/sbin /usr/lib -type f -perm /6000 -exec chmod a-s {} +
USER 10001:10001

FROM runtime-base AS runtime
ARG VERSION=v1.0.0-python
LABEL org.opencontainers.image.title="SmsInvit Redeem" org.opencontainers.image.version=${VERSION}
COPY apps/api/smsinvit ./apps/api/smsinvit
COPY vendor/codex_invit ./vendor/codex_invit
COPY scripts ./scripts
COPY migrations ./migrations
COPY --from=frontend /src/apps/web/dist ./static
COPY --from=frontend /src/apps/admin/dist ./static/admin
ENV PYTHONPATH=/app/apps/api SMSINVIT_BIND_HOST=0.0.0.0 PORT=3005 \
    SMSINVIT_STATIC_DIR=/app/static SMSINVIT_INVITATION_DATA_DIR=/app/runtime/invitation \
    SMSINVIT_INVITATION_BINARY_ROOTS=/opt/codex,/opt/smsinvit-bin \
    TZ=Asia/Shanghai
USER 10001:10001
EXPOSE 3005
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3005/health',timeout=3)" || exit 1
CMD ["python", "-m", "smsinvit"]
