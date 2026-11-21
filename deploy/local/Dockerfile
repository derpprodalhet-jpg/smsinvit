# Local refresh using the previously verified arm64 runtime dependency image.
# Build from release.py's allowlisted context, with built frontends in static/.
ARG RUNTIME_BASE=smsinvit-python:v1.0.0-python-arm64-local-runtime-base
FROM ${RUNTIME_BASE}
LABEL org.opencontainers.image.title="SmsInvit Redeem" org.opencontainers.image.version="local-admin-native"
WORKDIR /app
COPY apps/api/smsinvit ./apps/api/smsinvit
COPY vendor/codex_invit ./vendor/codex_invit
COPY scripts ./scripts
COPY migrations ./migrations
COPY static ./static
ENV PYTHONPATH=/app/apps/api SMSINVIT_BIND_HOST=0.0.0.0 PORT=3005 \
    SMSINVIT_STATIC_DIR=/app/static SMSINVIT_INVITATION_DATA_DIR=/app/runtime/invitation \
    SMSINVIT_INVITATION_BINARY_ROOTS=/opt/codex,/opt/smsinvit-bin TZ=Asia/Shanghai
USER 10001:10001
EXPOSE 3005
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3005/health',timeout=3)" || exit 1
CMD ["python", "-m", "smsinvit"]
